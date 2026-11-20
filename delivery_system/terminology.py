"""Publishable terminology books with manual indexing (source side).

A terminology book belongs to one document and lives in the *same* SQLite
database as the source catalog: the source side gains concepts, a chapter
tree, per-chapter ``spelling -> concept`` bindings, segment->chapter
assignments and manually indexed markers.  Nothing here touches source text
or translation bytes.

Working state is mutable; ``publish`` freezes everything that matters for
review — chapter tree, segment ownership, and each marker's segment revision
and spelling — into one immutable, numbered *terminology version*.  Versions
are independent of source releases: re-assigning a segment to another
chapter yields a new terminology version while source bytes stay untouched,
and old versions remain readable forever.

Resolution rule: a marker's concept is found by walking from its segment's
chapter *upwards* and taking the nearest binding for its spelling.  One
chapter binds a given spelling to at most one concept; the same concept may
be bound to different spellings in different chapters.  Publishing rejects
chapter cycles, dangling segments and undefined markers.

The delivery side never opens this database for writing: it uses
:class:`PublishedTerminologyReader`, a read-only contract that returns each
marker with its resolved concept identity for a specified version.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional

from .errors import (
    ChapterCycle,
    ChapterExists,
    ChapterNotFound,
    ConceptExists,
    ConceptNotFound,
    DanglingSegment,
    DocumentNotFound,
    InvalidBinding,
    MarkerExists,
    MarkerNotFound,
    SegmentNotFound,
    TerminologyVersionNotFound,
    UndefinedMarker,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS term_concepts (
    document_id      TEXT NOT NULL,
    concept_id       TEXT NOT NULL,
    label            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    PRIMARY KEY (document_id, concept_id)
);
CREATE TABLE IF NOT EXISTS term_chapters (
    document_id      TEXT NOT NULL,
    chapter_id       TEXT NOT NULL,
    parent_id        TEXT,
    name             TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    PRIMARY KEY (document_id, chapter_id)
);
CREATE TABLE IF NOT EXISTS term_bindings (
    document_id      TEXT NOT NULL,
    chapter_id       TEXT NOT NULL,
    spelling         TEXT NOT NULL,
    concept_id       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (document_id, chapter_id, spelling)
);
CREATE TABLE IF NOT EXISTS term_segment_chapters (
    document_id      TEXT NOT NULL,
    segment_id       TEXT NOT NULL,
    chapter_id       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (document_id, segment_id)
);
CREATE TABLE IF NOT EXISTS term_markers (
    document_id      TEXT NOT NULL,
    marker_id        TEXT NOT NULL,
    segment_id       TEXT NOT NULL,
    segment_revision INTEGER NOT NULL,
    spelling         TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (document_id, marker_id)
);
CREATE TABLE IF NOT EXISTS term_releases (
    release_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id      TEXT NOT NULL,
    version          INTEGER NOT NULL,
    snapshot_json    TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (document_id, version)
);
CREATE INDEX IF NOT EXISTS idx_term_releases_doc
    ON term_releases (document_id, version);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(db_path: str, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{os.fspath(db_path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(os.fspath(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@dataclass(frozen=True)
class ChapterNode:
    chapter_id: str
    parent_id: Optional[str]
    name: str


@dataclass(frozen=True)
class Binding:
    chapter_id: str
    spelling: str
    concept_id: str


@dataclass(frozen=True)
class Marker:
    """One manually indexed occurrence; identity is stable across versions."""

    marker_id: str
    segment_id: str
    segment_revision: int
    spelling: str


@dataclass(frozen=True)
class MarkerResolution:
    """Where a marker's concept came from in one terminology version."""

    marker_id: str
    segment_id: str
    segment_revision: int
    spelling: str
    chapter_id: str  # chapter owning the segment in this version
    binding_chapter_id: str  # chapter whose binding supplied the concept
    concept_id: str


@dataclass(frozen=True)
class TerminologyVersion:
    """Immutable, self-contained snapshot of one terminology book version.

    The chapter tree, segment ownership and every marker's segment revision
    and spelling are all reviewable inside this single object; resolution is
    a pure function of the snapshot, so re-opening the database and asking
    for the same version always reproduces the same concepts.
    """

    document_id: str
    version: int
    concepts: tuple[tuple[str, str], ...]  # (concept_id, label), sorted
    chapters: tuple[ChapterNode, ...]  # sorted by chapter_id
    bindings: tuple[Binding, ...]  # sorted by (chapter_id, spelling)
    segment_chapters: tuple[tuple[str, str], ...]  # (segment_id, chapter_id), sorted
    markers: tuple[Marker, ...]  # sorted by marker_id
    created_at: str

    def __post_init__(self) -> None:
        parent: dict[str, Optional[str]] = {}
        for node in self.chapters:
            if node.chapter_id in parent:
                raise ChapterNotFound(
                    f"duplicate chapter {node.chapter_id!r} in terminology snapshot"
                )
            parent[node.chapter_id] = node.parent_id
        for node in self.chapters:
            if node.parent_id is not None and node.parent_id not in parent:
                raise ChapterNotFound(
                    f"chapter {node.chapter_id!r} has unknown parent {node.parent_id!r}"
                )
        # Cycle check: every upwards walk must terminate.
        for start in parent:
            seen: set[str] = set()
            cursor: Optional[str] = start
            while cursor is not None:
                if cursor in seen:
                    raise ChapterCycle(
                        f"chapter cycle detected at {cursor!r} in terminology snapshot"
                    )
                seen.add(cursor)
                cursor = parent[cursor]
        binding_map: dict[tuple[str, str], str] = {}
        for binding in self.bindings:
            if binding.chapter_id not in parent:
                raise ChapterNotFound(
                    f"binding references unknown chapter {binding.chapter_id!r}"
                )
            key = (binding.chapter_id, binding.spelling)
            if key in binding_map:
                raise InvalidBinding(
                    f"chapter {binding.chapter_id!r} binds spelling "
                    f"{binding.spelling!r} more than once"
                )
            binding_map[key] = binding.concept_id
        concept_ids = {concept_id for concept_id, _ in self.concepts}
        for binding in self.bindings:
            if binding.concept_id not in concept_ids:
                raise ConceptNotFound(
                    f"binding references unknown concept {binding.concept_id!r}"
                )
        owner = dict(self.segment_chapters)
        for _segment_id, chapter_id in self.segment_chapters:
            if chapter_id not in parent:
                raise ChapterNotFound(
                    f"segment {_segment_id!r} assigned to unknown chapter {chapter_id!r}"
                )
        object.__setattr__(self, "_parent", parent)
        object.__setattr__(self, "_binding_map", binding_map)
        object.__setattr__(self, "_owner", owner)
        resolutions = tuple(self._resolve(marker) for marker in self.markers)
        object.__setattr__(self, "_resolutions", resolutions)

    # ------------------------------------------------------------------
    def _resolve(self, marker: Marker) -> MarkerResolution:
        chapter_id = self._owner.get(marker.segment_id)
        if chapter_id is None:
            raise DanglingSegment(
                f"marker {marker.marker_id!r}: segment {marker.segment_id!r} "
                "has no chapter in this terminology version"
            )
        parent = self._parent
        binding_map = self._binding_map
        cursor: Optional[str] = chapter_id
        while cursor is not None:
            concept = binding_map.get((cursor, marker.spelling))
            if concept is not None:
                return MarkerResolution(
                    marker_id=marker.marker_id,
                    segment_id=marker.segment_id,
                    segment_revision=marker.segment_revision,
                    spelling=marker.spelling,
                    chapter_id=chapter_id,
                    binding_chapter_id=cursor,
                    concept_id=concept,
                )
            cursor = parent[cursor]
        raise UndefinedMarker(
            f"marker {marker.marker_id!r}: spelling {marker.spelling!r} has no "
            f"binding reachable from chapter {chapter_id!r} upwards"
        )

    # ------------------------------------------------------------------
    @property
    def resolutions(self) -> tuple[MarkerResolution, ...]:
        return self._resolutions

    def marker_concepts(self) -> dict[str, str]:
        """The read-only contract payload: marker identity -> concept identity."""

        return {r.marker_id: r.concept_id for r in self._resolutions}

    def chapter_of(self, segment_id: str) -> Optional[str]:
        return self._owner.get(segment_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "version": self.version,
            "concepts": [
                {"concept_id": cid, "label": label} for cid, label in self.concepts
            ],
            "chapters": [
                {
                    "chapter_id": node.chapter_id,
                    "parent_id": node.parent_id,
                    "name": node.name,
                }
                for node in self.chapters
            ],
            "bindings": [
                {
                    "chapter_id": b.chapter_id,
                    "spelling": b.spelling,
                    "concept_id": b.concept_id,
                }
                for b in self.bindings
            ],
            "segment_chapters": [
                {"segment_id": sid, "chapter_id": cid}
                for sid, cid in self.segment_chapters
            ],
            "markers": [
                {
                    "marker_id": m.marker_id,
                    "segment_id": m.segment_id,
                    "segment_revision": m.segment_revision,
                    "spelling": m.spelling,
                }
                for m in self.markers
            ],
            "resolutions": [
                {
                    "marker_id": r.marker_id,
                    "segment_id": r.segment_id,
                    "segment_revision": r.segment_revision,
                    "spelling": r.spelling,
                    "chapter_id": r.chapter_id,
                    "binding_chapter_id": r.binding_chapter_id,
                    "concept_id": r.concept_id,
                }
                for r in self._resolutions
            ],
            "created_at": self.created_at,
        }


def _version_from_snapshot(
    document_id: str, version: int, snapshot: dict[str, object], created_at: str
) -> TerminologyVersion:
    return TerminologyVersion(
        document_id=document_id,
        version=version,
        concepts=tuple(
            (str(c["concept_id"]), str(c.get("label", "")))
            for c in snapshot.get("concepts", [])  # type: ignore[union-attr]
        ),
        chapters=tuple(
            ChapterNode(
                chapter_id=str(c["chapter_id"]),
                parent_id=None if c["parent_id"] is None else str(c["parent_id"]),
                name=str(c.get("name", "")),
            )
            for c in snapshot.get("chapters", [])  # type: ignore[union-attr]
        ),
        bindings=tuple(
            Binding(
                chapter_id=str(b["chapter_id"]),
                spelling=str(b["spelling"]),
                concept_id=str(b["concept_id"]),
            )
            for b in snapshot.get("bindings", [])  # type: ignore[union-attr]
        ),
        segment_chapters=tuple(
            (str(s["segment_id"]), str(s["chapter_id"]))
            for s in snapshot.get("segment_chapters", [])  # type: ignore[union-attr]
        ),
        markers=tuple(
            Marker(
                marker_id=str(m["marker_id"]),
                segment_id=str(m["segment_id"]),
                segment_revision=int(m["segment_revision"]),
                spelling=str(m["spelling"]),
            )
            for m in snapshot.get("markers", [])  # type: ignore[union-attr]
        ),
        created_at=created_at,
    )


class TerminologyCatalog:
    """Read/write terminology book inside the source catalog's database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = os.fspath(db_path)
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = _connect(self.db_path)
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # working-state mutations
    # ------------------------------------------------------------------
    def add_concept(self, document_id: str, concept_id: str, label: str = "") -> None:
        if not concept_id:
            raise ConceptNotFound("concept_id must be non-empty")
        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                self._require_document(conn, document_id)
                existing = conn.execute(
                    "SELECT label FROM term_concepts WHERE document_id = ? AND concept_id = ?",
                    (document_id, concept_id),
                ).fetchone()
                if existing is not None:
                    if existing["label"] != label:
                        raise ConceptExists(
                            f"concept {concept_id!r} already exists with a different label"
                        )
                    return
                conn.execute(
                    "INSERT INTO term_concepts (document_id, concept_id, label, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (document_id, concept_id, label, _now()),
                )
        finally:
            conn.close()

    def add_chapter(
        self,
        document_id: str,
        chapter_id: str,
        *,
        parent_id: Optional[str] = None,
        name: str = "",
    ) -> None:
        if not chapter_id:
            raise ChapterNotFound("chapter_id must be non-empty")
        if parent_id == chapter_id:
            raise ChapterCycle(f"chapter {chapter_id!r} cannot be its own parent")
        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                self._require_document(conn, document_id)
                if parent_id is not None:
                    self._require_chapter(conn, document_id, parent_id)
                existing = conn.execute(
                    "SELECT parent_id, name FROM term_chapters"
                    " WHERE document_id = ? AND chapter_id = ?",
                    (document_id, chapter_id),
                ).fetchone()
                if existing is not None:
                    same = (existing["parent_id"] == parent_id) and existing["name"] == name
                    if not same:
                        raise ChapterExists(
                            f"chapter {chapter_id!r} already exists with different data"
                        )
                    return
                conn.execute(
                    "INSERT INTO term_chapters (document_id, chapter_id, parent_id, name,"
                    " created_at) VALUES (?, ?, ?, ?, ?)",
                    (document_id, chapter_id, parent_id, name, _now()),
                )
        finally:
            conn.close()

    def set_chapter_parent(
        self, document_id: str, chapter_id: str, parent_id: Optional[str]
    ) -> None:
        """Reparent a chapter; rejected immediately if it would create a cycle."""

        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                self._require_document(conn, document_id)
                self._require_chapter(conn, document_id, chapter_id)
                if parent_id is not None:
                    self._require_chapter(conn, document_id, parent_id)
                    # Walk up from the new parent: meeting chapter_id means a cycle.
                    cursor: Optional[str] = parent_id
                    seen: set[str] = set()
                    while cursor is not None:
                        if cursor == chapter_id:
                            raise ChapterCycle(
                                f"reparenting {chapter_id!r} under {parent_id!r}"
                                " would create a chapter cycle"
                            )
                        if cursor in seen:  # defensive: pre-existing cycle
                            raise ChapterCycle(
                                f"chapter cycle already present at {cursor!r}"
                            )
                        seen.add(cursor)
                        row = conn.execute(
                            "SELECT parent_id FROM term_chapters"
                            " WHERE document_id = ? AND chapter_id = ?",
                            (document_id, cursor),
                        ).fetchone()
                        cursor = row["parent_id"] if row else None
                conn.execute(
                    "UPDATE term_chapters SET parent_id = ?"
                    " WHERE document_id = ? AND chapter_id = ?",
                    (parent_id, document_id, chapter_id),
                )
        finally:
            conn.close()

    def set_binding(
        self, document_id: str, chapter_id: str, spelling: str, concept_id: str
    ) -> None:
        """Bind a spelling to a concept in one chapter (at most one concept).

        Re-binding the same spelling in the same chapter replaces the concept;
        a chapter can never hold two concepts for one spelling.
        """

        if not spelling:
            raise InvalidBinding("spelling must be non-empty")
        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                self._require_document(conn, document_id)
                self._require_chapter(conn, document_id, chapter_id)
                self._require_concept(conn, document_id, concept_id)
                conn.execute(
                    "INSERT INTO term_bindings (document_id, chapter_id, spelling,"
                    " concept_id, created_at) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT (document_id, chapter_id, spelling)"
                    " DO UPDATE SET concept_id = excluded.concept_id",
                    (document_id, chapter_id, spelling, concept_id, _now()),
                )
        finally:
            conn.close()

    def assign_segment(self, document_id: str, segment_id: str, chapter_id: str) -> None:
        """Assign a segment to a chapter (upsert).  Moving a segment to another
        chapter may change which concept its spellings resolve to; the change
        only becomes visible to readers after the next ``publish``."""

        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                self._require_document(conn, document_id)
                self._require_chapter(conn, document_id, chapter_id)
                self._require_segment(conn, document_id, segment_id)
                conn.execute(
                    "INSERT INTO term_segment_chapters (document_id, segment_id,"
                    " chapter_id, created_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (document_id, segment_id)"
                    " DO UPDATE SET chapter_id = excluded.chapter_id",
                    (document_id, segment_id, chapter_id, _now()),
                )
        finally:
            conn.close()

    def add_marker(
        self,
        document_id: str,
        marker_id: str,
        segment_id: str,
        segment_revision: int,
        spelling: str,
    ) -> None:
        """Index one occurrence by hand.  The marker keeps this identity across
        every future terminology version; target-side drafts refer to it."""

        if not marker_id:
            raise MarkerNotFound("marker_id must be non-empty")
        if not spelling:
            raise UndefinedMarker("marker spelling must be non-empty")
        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                self._require_document(conn, document_id)
                revision = self._segment_revision(conn, document_id, segment_id)
                if segment_revision < 1 or segment_revision > revision:
                    raise SegmentNotFound(
                        f"segment {segment_id!r} has revision {revision}; cannot index "
                        f"revision {segment_revision}"
                    )
                existing = conn.execute(
                    "SELECT segment_id, segment_revision, spelling FROM term_markers"
                    " WHERE document_id = ? AND marker_id = ?",
                    (document_id, marker_id),
                ).fetchone()
                if existing is not None:
                    same = (
                        existing["segment_id"] == segment_id
                        and int(existing["segment_revision"]) == segment_revision
                        and existing["spelling"] == spelling
                    )
                    if not same:
                        raise MarkerExists(
                            f"marker {marker_id!r} already exists with different content"
                        )
                    return
                conn.execute(
                    "INSERT INTO term_markers (document_id, marker_id, segment_id,"
                    " segment_revision, spelling, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (document_id, marker_id, segment_id, segment_revision, spelling, _now()),
                )
        finally:
            conn.close()

    def remove_marker(self, document_id: str, marker_id: str) -> None:
        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                self._require_document(conn, document_id)
                cur = conn.execute(
                    "DELETE FROM term_markers WHERE document_id = ? AND marker_id = ?",
                    (document_id, marker_id),
                )
                if cur.rowcount == 0:
                    raise MarkerNotFound(f"marker {marker_id!r} not found")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # publishing immutable versions
    # ------------------------------------------------------------------
    def publish(self, document_id: str) -> TerminologyVersion:
        """Freeze the working state as the next terminology version.

        Validation rejects chapter cycles, dangling segments (every document
        segment must own a chapter) and undefined markers (a spelling with no
        binding reachable upwards).  Source text and translation bytes are
        not involved and never change here.
        """

        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                self._require_document(conn, document_id)
                snapshot = self._working_snapshot(conn, document_id)
                self._validate_snapshot(conn, document_id, snapshot)
                row = conn.execute(
                    "SELECT MAX(version) AS v FROM term_releases WHERE document_id = ?",
                    (document_id,),
                ).fetchone()
                version = 1 if row["v"] is None else int(row["v"]) + 1
                created = _now()
                conn.execute(
                    "INSERT INTO term_releases (document_id, version, snapshot_json,"
                    " created_at) VALUES (?, ?, ?, ?)",
                    (
                        document_id,
                        version,
                        json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                        created,
                    ),
                )
        finally:
            conn.close()
        return _version_from_snapshot(document_id, version, snapshot, created)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def terminology_version(self, document_id: str, version: int) -> TerminologyVersion:
        conn = _connect(self.db_path, readonly=True)
        try:
            return self._read_version(conn, document_id, version)
        finally:
            conn.close()

    def latest_version(self, document_id: str) -> Optional[int]:
        conn = _connect(self.db_path, readonly=True)
        try:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM term_releases WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            return None if row is None or row["v"] is None else int(row["v"])
        finally:
            conn.close()

    def list_versions(self, document_id: str) -> list[dict[str, object]]:
        conn = _connect(self.db_path, readonly=True)
        try:
            rows = conn.execute(
                "SELECT version, created_at FROM term_releases WHERE document_id = ?"
                " ORDER BY version",
                (document_id,),
            ).fetchall()
            return [
                {"version": int(r["version"]), "created_at": r["created_at"]} for r in rows
            ]
        finally:
            conn.close()

    def current_state(self, document_id: str) -> dict[str, object]:
        conn = _connect(self.db_path, readonly=True)
        try:
            self._require_document(conn, document_id)
            return self._working_snapshot(conn, document_id)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _read_version(
        conn: sqlite3.Connection, document_id: str, version: int
    ) -> TerminologyVersion:
        row = conn.execute(
            "SELECT snapshot_json, created_at FROM term_releases"
            " WHERE document_id = ? AND version = ?",
            (document_id, version),
        ).fetchone()
        if row is None:
            raise TerminologyVersionNotFound(
                f"terminology version {version} not found for {document_id!r}"
            )
        snapshot = json.loads(row["snapshot_json"])
        return _version_from_snapshot(document_id, version, snapshot, row["created_at"])

    @staticmethod
    def _require_document(conn: sqlite3.Connection, document_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise DocumentNotFound(f"document {document_id!r} not found")

    @staticmethod
    def _require_chapter(
        conn: sqlite3.Connection, document_id: str, chapter_id: str
    ) -> None:
        row = conn.execute(
            "SELECT 1 FROM term_chapters WHERE document_id = ? AND chapter_id = ?",
            (document_id, chapter_id),
        ).fetchone()
        if row is None:
            raise ChapterNotFound(f"chapter {chapter_id!r} not found in {document_id!r}")

    @staticmethod
    def _require_concept(
        conn: sqlite3.Connection, document_id: str, concept_id: str
    ) -> None:
        row = conn.execute(
            "SELECT 1 FROM term_concepts WHERE document_id = ? AND concept_id = ?",
            (document_id, concept_id),
        ).fetchone()
        if row is None:
            raise ConceptNotFound(f"concept {concept_id!r} not found in {document_id!r}")

    @staticmethod
    def _segment_revision(
        conn: sqlite3.Connection, document_id: str, segment_id: str
    ) -> int:
        row = conn.execute(
            "SELECT revision FROM segments WHERE document_id = ? AND segment_id = ?",
            (document_id, segment_id),
        ).fetchone()
        if row is None:
            raise SegmentNotFound(
                f"segment {segment_id!r} not found in document {document_id!r}"
            )
        return int(row["revision"])

    @classmethod
    def _require_segment(
        cls, conn: sqlite3.Connection, document_id: str, segment_id: str
    ) -> None:
        cls._segment_revision(conn, document_id, segment_id)

    @staticmethod
    def _working_snapshot(conn: sqlite3.Connection, document_id: str) -> dict[str, object]:
        concepts = [
            {"concept_id": r["concept_id"], "label": r["label"]}
            for r in conn.execute(
                "SELECT concept_id, label FROM term_concepts WHERE document_id = ?"
                " ORDER BY concept_id",
                (document_id,),
            )
        ]
        chapters = [
            {"chapter_id": r["chapter_id"], "parent_id": r["parent_id"], "name": r["name"]}
            for r in conn.execute(
                "SELECT chapter_id, parent_id, name FROM term_chapters"
                " WHERE document_id = ? ORDER BY chapter_id",
                (document_id,),
            )
        ]
        bindings = [
            {
                "chapter_id": r["chapter_id"],
                "spelling": r["spelling"],
                "concept_id": r["concept_id"],
            }
            for r in conn.execute(
                "SELECT chapter_id, spelling, concept_id FROM term_bindings"
                " WHERE document_id = ? ORDER BY chapter_id, spelling",
                (document_id,),
            )
        ]
        segment_chapters = [
            {"segment_id": r["segment_id"], "chapter_id": r["chapter_id"]}
            for r in conn.execute(
                "SELECT segment_id, chapter_id FROM term_segment_chapters"
                " WHERE document_id = ? ORDER BY segment_id",
                (document_id,),
            )
        ]
        markers = [
            {
                "marker_id": r["marker_id"],
                "segment_id": r["segment_id"],
                "segment_revision": int(r["segment_revision"]),
                "spelling": r["spelling"],
            }
            for r in conn.execute(
                "SELECT marker_id, segment_id, segment_revision, spelling"
                " FROM term_markers WHERE document_id = ? ORDER BY marker_id",
                (document_id,),
            )
        ]
        return {
            "concepts": concepts,
            "chapters": chapters,
            "bindings": bindings,
            "segment_chapters": segment_chapters,
            "markers": markers,
        }

    @classmethod
    def _validate_snapshot(
        cls, conn: sqlite3.Connection, document_id: str, snapshot: dict[str, object]
    ) -> None:
        chapters = snapshot["chapters"]
        bindings = snapshot["bindings"]
        segment_chapters = snapshot["segment_chapters"]
        markers = snapshot["markers"]
        concepts = snapshot["concepts"]

        parent: dict[str, Optional[str]] = {}
        for node in chapters:  # type: ignore[union-attr]
            cid = str(node["chapter_id"])
            parent[cid] = node["parent_id"]
        for node in chapters:  # type: ignore[union-attr]
            pid = node["parent_id"]
            if pid is not None and pid not in parent:
                raise ChapterNotFound(
                    f"chapter {node['chapter_id']!r} has unknown parent {pid!r}"
                )
        for start in parent:
            seen: set[str] = set()
            cursor: Optional[str] = start
            while cursor is not None:
                if cursor in seen:
                    raise ChapterCycle(f"chapter cycle detected at {cursor!r}")
                seen.add(cursor)
                cursor = parent[cursor]

        concept_ids = {str(c["concept_id"]) for c in concepts}  # type: ignore[union-attr]
        seen_binding: set[tuple[str, str]] = set()
        for binding in bindings:  # type: ignore[union-attr]
            cid = str(binding["chapter_id"])
            if cid not in parent:
                raise ChapterNotFound(f"binding references unknown chapter {cid!r}")
            if str(binding["concept_id"]) not in concept_ids:
                raise ConceptNotFound(
                    f"binding references unknown concept {binding['concept_id']!r}"
                )
            key = (cid, str(binding["spelling"]))
            if key in seen_binding:
                raise InvalidBinding(
                    f"chapter {cid!r} binds spelling {binding['spelling']!r} more than once"
                )
            seen_binding.add(key)

        # Dangling segments: every document segment must own a chapter, and
        # every assignment must point at a real segment and a real chapter.
        doc_segments = {
            r["segment_id"]: int(r["revision"])
            for r in conn.execute(
                "SELECT segment_id, revision FROM segments WHERE document_id = ?",
                (document_id,),
            )
        }
        owner: dict[str, str] = {}
        for item in segment_chapters:  # type: ignore[union-attr]
            sid = str(item["segment_id"])
            cid = str(item["chapter_id"])
            if sid not in doc_segments:
                raise DanglingSegment(
                    f"assignment references unknown segment {sid!r}"
                )
            if cid not in parent:
                raise ChapterNotFound(
                    f"segment {sid!r} assigned to unknown chapter {cid!r}"
                )
            owner[sid] = cid
        unassigned = sorted(sid for sid in doc_segments if sid not in owner)
        if unassigned:
            raise DanglingSegment(
                "document segment(s) without a chapter: " + ", ".join(unassigned)
            )

        binding_map = {
            (str(b["chapter_id"]), str(b["spelling"])): str(b["concept_id"])
            for b in bindings  # type: ignore[union-attr]
        }
        for marker in markers:  # type: ignore[union-attr]
            mid = str(marker["marker_id"])
            sid = str(marker["segment_id"])
            revision = int(marker["segment_revision"])
            if sid not in doc_segments:
                raise DanglingSegment(
                    f"marker {mid!r} references unknown segment {sid!r}"
                )
            if revision < 1 or revision > doc_segments[sid]:
                raise SegmentNotFound(
                    f"marker {mid!r}: segment {sid!r} has revision "
                    f"{doc_segments[sid]}, not {revision}"
                )
            chapter_id = owner[sid]
            cursor = chapter_id
            found = False
            while cursor is not None:
                if (cursor, str(marker["spelling"])) in binding_map:
                    found = True
                    break
                cursor = parent[cursor]
            if not found:
                raise UndefinedMarker(
                    f"marker {mid!r}: spelling {marker['spelling']!r} has no binding "
                    f"reachable from chapter {chapter_id!r} upwards"
                )


class PublishedTerminologyReader:
    """Read-only contract used by the delivery side.

    Opens the source database with ``mode=ro`` on every call and returns the
    requested immutable terminology version, including each marker's resolved
    concept identity.  Regional editors never read or write the source
    database directly; this reader is the only way across.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = os.fspath(db_path)
        if not os.path.exists(self.db_path):
            raise DocumentNotFound(f"source database {self.db_path!r} does not exist")

    def terminology_version(self, document_id: str, version: int) -> TerminologyVersion:
        conn = _connect(self.db_path, readonly=True)
        try:
            return TerminologyCatalog._read_version(conn, document_id, version)
        finally:
            conn.close()

    def latest_version(self, document_id: str) -> Optional[int]:
        conn = _connect(self.db_path, readonly=True)
        try:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM term_releases WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            return None if row is None or row["v"] is None else int(row["v"])
        finally:
            conn.close()

    def list_versions(self, document_id: str) -> list[dict[str, object]]:
        conn = _connect(self.db_path, readonly=True)
        try:
            rows = conn.execute(
                "SELECT version, created_at FROM term_releases WHERE document_id = ?"
                " ORDER BY version",
                (document_id,),
            ).fetchall()
            return [
                {"version": int(r["version"]), "created_at": r["created_at"]} for r in rows
            ]
        finally:
            conn.close()
