"""Multilingual delivery repository: translations, covers and immutable packages.

The repository stores translation entries and packages in its *own* SQLite
database.  Source identity, order and revisions are read through a
:class:`~delivery_system.source_catalog.PublishedSourceReader`, which opens the
catalog database read-only.  The delivery side never writes source state and
never treats a position number as identity.

Publishing freezes the current source snapshot and the selected translation
entries.  Packages are immutable: reusing an id with different frozen content
is rejected, and old packages keep the source text and ids they were built
with even after the source is revised.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional, Protocol

from .coverage import assess_entries, solve_cover
from .errors import (
    DocumentNotFound,
    InvalidRegionalLock,
    InvalidRegionalQuote,
    InvalidTranslation,
    NoCoverError,
    NoRegionalEdition,
    PackageExistsImmutable,
    PackageNotFound,
    RegionalError,
    RegionalQuoteExists,
    TranslationExists,
)
from .models import (
    CoverResult,
    SourceProduct,
    SourceRef,
    TranslationAssessment,
    TranslationEntry,
)
from .regional import (
    MAX_USAGES,
    ChunkView,
    RegionalPlan,
    RegionalQuote,
    solve_regional,
)
from .source_catalog import PublishedSourceReader
from .terminology import PublishedTerminologyReader, TerminologyVersion

PACKAGE_FORMAT = "translation-package/1"
REGIONAL_FORMAT = "regional-edition/1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    translation_id  TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    language        TEXT NOT NULL,
    text            TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_translations_doc_lang
    ON translations (document_id, language);
CREATE TABLE IF NOT EXISTS packages (
    package_id       TEXT PRIMARY KEY,
    document_id      TEXT NOT NULL,
    language         TEXT NOT NULL,
    content_json     TEXT NOT NULL,
    content_sha256   TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packages_doc_lang
    ON packages (document_id, language);
CREATE TABLE IF NOT EXISTS regional_quotes (
    document_id      TEXT NOT NULL,
    region           TEXT NOT NULL,
    translation_id   TEXT NOT NULL,
    draft_id         TEXT NOT NULL,
    text             TEXT NOT NULL,
    assignments_json TEXT NOT NULL,
    cost             INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (document_id, region, translation_id, draft_id)
);
CREATE TABLE IF NOT EXISTS regional_locks (
    document_id      TEXT NOT NULL,
    region           TEXT NOT NULL,
    concept_id       TEXT NOT NULL,
    value            INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (document_id, region, concept_id)
);
CREATE TABLE IF NOT EXISTS regional_editions (
    edition_id       TEXT PRIMARY KEY,
    document_id      TEXT NOT NULL,
    language         TEXT NOT NULL,
    region           TEXT NOT NULL,
    base_package_id  TEXT NOT NULL,
    terminology_version INTEGER NOT NULL,
    content_json     TEXT NOT NULL,
    content_sha256   TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regional_editions_doc
    ON regional_editions (document_id, region);
"""


class SourceProductReader(Protocol):
    def published_product(self, document_id: str) -> SourceProduct: ...

    def latest_release_id(self, document_id: str) -> Optional[int]: ...


class TerminologyReader(Protocol):
    """Read-only contract through which the delivery side learns concepts."""

    def terminology_version(self, document_id: str, version: int) -> TerminologyVersion: ...

    def latest_version(self, document_id: str) -> Optional[int]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _refs_to_json(refs: Iterable[SourceRef]) -> str:
    return json.dumps(
        [{"segment_id": r.segment_id, "revision": r.revision} for r in refs],
        ensure_ascii=False,
        sort_keys=True,
    )


def _refs_from_json(raw: str) -> tuple[SourceRef, ...]:
    return tuple(
        SourceRef(segment_id=str(r["segment_id"]), revision=int(r["revision"]))
        for r in json.loads(raw)
    )


def _entry_from_row(row: sqlite3.Row) -> TranslationEntry:
    return TranslationEntry(
        translation_id=row["translation_id"],
        document_id=row["document_id"],
        language=row["language"],
        text=row["text"],
        source_refs=_refs_from_json(row["source_refs_json"]),
    )


class DeliveryRepository:
    def __init__(
        self,
        db_path: str,
        source: SourceProductReader,
        terminology: Optional[TerminologyReader] = None,
    ) -> None:
        self.db_path = os.fspath(db_path)
        self.source = source
        self.terminology = terminology
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _transaction(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # translations
    # ------------------------------------------------------------------
    def add_translation(
        self,
        translation_id: str,
        document_id: str,
        language: str,
        text: str,
        source_refs: Iterable[dict[str, object] | SourceRef],
    ) -> TranslationEntry:
        if not translation_id:
            raise InvalidTranslation("translation_id must be non-empty")
        if not language:
            raise InvalidTranslation("language must be non-empty")
        refs: list[SourceRef] = []
        for ref in source_refs:
            if isinstance(ref, SourceRef):
                refs.append(ref)
            else:
                refs.append(
                    SourceRef(
                        segment_id=str(ref["segment_id"]),
                        revision=int(ref["revision"]),
                    )
                )
        if not refs:
            raise InvalidTranslation("source_refs must not be empty")

        entry = TranslationEntry(
            translation_id=translation_id,
            document_id=document_id,
            language=language,
            text=text,
            source_refs=tuple(refs),
        )
        conn = self._connect()
        try:
            with self._transaction(conn):
                existing = conn.execute(
                    "SELECT language, text, source_refs_json FROM translations"
                    " WHERE translation_id = ?",
                    (translation_id,),
                ).fetchone()
                if existing is not None:
                    same = (
                        existing["language"] == language
                        and existing["text"] == text
                        and existing["source_refs_json"] == _refs_to_json(refs)
                    )
                    if not same:
                        raise TranslationExists(
                            f"translation {translation_id!r} already exists with different content"
                        )
                    # Idempotent re-add of an identical entry.
                    return entry
                # Make sure the document exists on the source side so we never
                # accumulate translations for unknown documents.
                try:
                    self.source.published_product(document_id)
                except DocumentNotFound:
                    raise InvalidTranslation(
                        f"document {document_id!r} does not exist in the source catalog"
                    )
                conn.execute(
                    "INSERT INTO translations (translation_id, document_id, language, text,"
                    " source_refs_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        translation_id,
                        document_id,
                        language,
                        text,
                        _refs_to_json(refs),
                        _now(),
                    ),
                )
        finally:
            conn.close()
        return entry

    def get_translation(self, translation_id: str) -> TranslationEntry:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM translations WHERE translation_id = ?",
                (translation_id,),
            ).fetchone()
            if row is None:
                raise InvalidTranslation(f"translation {translation_id!r} not found")
            return _entry_from_row(row)
        finally:
            conn.close()

    def list_translations(
        self, document_id: Optional[str] = None, language: Optional[str] = None
    ) -> list[TranslationEntry]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if language is not None:
            clauses.append("language = ?")
            params.append(language)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM translations" + where + " ORDER BY translation_id",
                params,
            ).fetchall()
            return [_entry_from_row(row) for row in rows]
        finally:
            conn.close()

    def languages(self, document_id: str) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT language FROM translations WHERE document_id = ?"
                " ORDER BY language",
                (document_id,),
            ).fetchall()
            return [row["language"] for row in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # assessment / cover
    # ------------------------------------------------------------------
    def assess(
        self, document_id: str, language: str
    ) -> list[TranslationAssessment]:
        product = self.source.published_product(document_id)
        entries = self.list_translations(document_id, language)
        return assess_entries(entries, product, language)

    def plan_cover(self, document_id: str, language: str) -> CoverResult:
        product = self.source.published_product(document_id)
        entries = self.list_translations(document_id, language)
        return solve_cover(product, language, entries)

    # ------------------------------------------------------------------
    # packages
    # ------------------------------------------------------------------
    def publish_language(
        self, document_id: str, language: str, package_id: str
    ) -> dict[str, object]:
        result = self.plan_cover(document_id, language)
        if not result.ok:
            raise NoCoverError(
                f"cannot publish {language!r} for {document_id!r}: "
                f"{result.diagnosis.detail}",
                result=result,
            )
        try:
            source_release = self.source.latest_release_id(document_id)
        except Exception:  # pragma: no cover - optional reader capability
            source_release = None
        return self._freeze_package(document_id, language, package_id, result, source_release)

    def _build_content(
        self,
        document_id: str,
        language: str,
        package_id: str,
        result: CoverResult,
        source_release: Optional[int],
    ) -> dict[str, object]:
        product = result.product
        source_snapshot = product.snapshot()
        chunks = [chunk.to_package_chunk() for chunk in result.chunks]
        identity = json.dumps(
            {
                "document_id": document_id,
                "language": language,
                "source_snapshot": source_snapshot,
                "chunks": chunks,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        content_sha = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        content: dict[str, object] = {
            "format": PACKAGE_FORMAT,
            "package_id": package_id,
            "document_id": document_id,
            "language": language,
            "source_revision": product.document_revision,
            "source_release": source_release,
            "source_snapshot": source_snapshot,
            "chunks": chunks,
            "rendered_text": result.rendered_text,
            "content_sha256": content_sha,
            "created_at": _now(),
        }
        return content

    def _freeze_package(
        self,
        document_id: str,
        language: str,
        package_id: str,
        result: CoverResult,
        source_release: Optional[int],
    ) -> dict[str, object]:
        content = self._build_content(
            document_id, language, package_id, result, source_release
        )
        sha = str(content["content_sha256"])
        conn = self._connect()
        try:
            with self._transaction(conn):
                existing = conn.execute(
                    "SELECT content_sha256, content_json FROM packages WHERE package_id = ?",
                    (package_id,),
                ).fetchone()
                if existing is not None:
                    if existing["content_sha256"] != sha:
                        raise PackageExistsImmutable(
                            f"package {package_id!r} already exists with different frozen content"
                        )
                    return json.loads(existing["content_json"])
                conn.execute(
                    "INSERT INTO packages (package_id, document_id, language, content_json,"
                    " content_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        package_id,
                        document_id,
                        language,
                        json.dumps(content, ensure_ascii=False, sort_keys=True),
                        sha,
                        str(content["created_at"]),
                    ),
                )
        finally:
            conn.close()
        return content

    def get_package(self, package_id: str) -> dict[str, object]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT content_json FROM packages WHERE package_id = ?",
                (package_id,),
            ).fetchone()
            if row is None:
                raise PackageNotFound(f"package {package_id!r} not found")
            return json.loads(row["content_json"])
        finally:
            conn.close()

    def list_packages(
        self, document_id: Optional[str] = None, language: Optional[str] = None
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if language is not None:
            clauses.append("language = ?")
            params.append(language)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT package_id, document_id, language, content_sha256, created_at"
                " FROM packages" + where + " ORDER BY created_at, package_id",
                params,
            ).fetchall()
            return [
                {
                    "package_id": row["package_id"],
                    "document_id": row["document_id"],
                    "language": row["language"],
                    "content_sha256": row["content_sha256"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # regional quotes and locks
    # ------------------------------------------------------------------
    def add_regional_quote(
        self,
        document_id: str,
        region: str,
        translation_id: str,
        draft_id: str,
        text: str,
        assignments: dict[str, int],
        cost: int,
    ) -> RegionalQuote:
        """Offer one whole-sentence replacement draft for a selected chunk.

        The draft declares usages by stable marker identity.  Cost must be
        non-negative and the text must be the complete replacement sentence.
        """

        if not region:
            raise InvalidRegionalQuote("region must be non-empty")
        if not draft_id:
            raise InvalidRegionalQuote("draft_id must be non-empty")
        if not isinstance(text, str) or not text.strip():
            raise InvalidRegionalQuote("draft text must be the complete sentence")
        if not isinstance(cost, int) or cost < 0:
            raise InvalidRegionalQuote("cost must be a non-negative integer")
        if not assignments:
            raise InvalidRegionalQuote("assignments must not be empty")
        normalized: dict[str, int] = {}
        for marker_id, value in assignments.items():
            if not marker_id:
                raise InvalidRegionalQuote("marker_id must be non-empty")
            if not isinstance(value, int) or value < 1 or value > MAX_USAGES:
                raise InvalidRegionalQuote(
                    f"usage for marker {marker_id!r} must be in 1..{MAX_USAGES}"
                )
            normalized[str(marker_id)] = int(value)
        # Quotes address real chunks of real packages of the same document.
        entry = self.get_translation(translation_id)
        if entry.document_id != document_id:
            raise InvalidRegionalQuote(
                f"translation {translation_id!r} belongs to document "
                f"{entry.document_id!r}, not {document_id!r}"
            )

        quote = RegionalQuote(
            document_id=document_id,
            region=region,
            translation_id=translation_id,
            draft_id=draft_id,
            text=text,
            assignments=tuple(sorted(normalized.items())),
            cost=cost,
        )
        assignments_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        conn = self._connect()
        try:
            with self._transaction(conn):
                existing = conn.execute(
                    "SELECT text, assignments_json, cost FROM regional_quotes"
                    " WHERE document_id = ? AND region = ? AND translation_id = ?"
                    " AND draft_id = ?",
                    (document_id, region, translation_id, draft_id),
                ).fetchone()
                if existing is not None:
                    same = (
                        existing["text"] == text
                        and existing["assignments_json"] == assignments_json
                        and int(existing["cost"]) == cost
                    )
                    if not same:
                        raise RegionalQuoteExists(
                            f"quote {draft_id!r} for {translation_id!r} already exists "
                            "with different content"
                        )
                    return quote
                conn.execute(
                    "INSERT INTO regional_quotes (document_id, region, translation_id,"
                    " draft_id, text, assignments_json, cost, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        document_id,
                        region,
                        translation_id,
                        draft_id,
                        text,
                        assignments_json,
                        cost,
                        _now(),
                    ),
                )
        finally:
            conn.close()
        return quote

    def list_regional_quotes(self, document_id: str, region: str) -> list[RegionalQuote]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM regional_quotes WHERE document_id = ? AND region = ?"
                " ORDER BY translation_id, draft_id",
                (document_id, region),
            ).fetchall()
            return [
                RegionalQuote(
                    document_id=row["document_id"],
                    region=row["region"],
                    translation_id=row["translation_id"],
                    draft_id=row["draft_id"],
                    text=row["text"],
                    assignments=tuple(
                        sorted(
                            (str(k), int(v))
                            for k, v in json.loads(row["assignments_json"]).items()
                        )
                    ),
                    cost=int(row["cost"]),
                )
                for row in rows
            ]
        finally:
            conn.close()

    def set_regional_lock(
        self, document_id: str, region: str, concept_id: str, value: int
    ) -> None:
        """Pre-lock one concept's usage for a region (upsert)."""

        if not concept_id:
            raise InvalidRegionalLock("concept_id must be non-empty")
        if not isinstance(value, int) or value < 1 or value > MAX_USAGES:
            raise InvalidRegionalLock(
                f"lock value for concept {concept_id!r} must be in 1..{MAX_USAGES}"
            )
        conn = self._connect()
        try:
            with self._transaction(conn):
                conn.execute(
                    "INSERT INTO regional_locks (document_id, region, concept_id, value,"
                    " created_at) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT (document_id, region, concept_id)"
                    " DO UPDATE SET value = excluded.value",
                    (document_id, region, concept_id, value, _now()),
                )
        finally:
            conn.close()

    def remove_regional_lock(self, document_id: str, region: str, concept_id: str) -> None:
        conn = self._connect()
        try:
            with self._transaction(conn):
                conn.execute(
                    "DELETE FROM regional_locks WHERE document_id = ? AND region = ?"
                    " AND concept_id = ?",
                    (document_id, region, concept_id),
                )
        finally:
            conn.close()

    def list_regional_locks(self, document_id: str, region: str) -> dict[str, int]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT concept_id, value FROM regional_locks"
                " WHERE document_id = ? AND region = ? ORDER BY concept_id",
                (document_id, region),
            ).fetchall()
            return {row["concept_id"]: int(row["value"]) for row in rows}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # regional editions
    # ------------------------------------------------------------------
    def _terminology_reader(self) -> TerminologyReader:
        if self.terminology is None:
            raise RegionalError(
                "no terminology reader configured; regional editions need the "
                "read-only terminology contract"
            )
        return self.terminology

    def solve_regional_edition(
        self,
        package_id: str,
        region: str,
        terminology_version: Optional[int] = None,
    ) -> RegionalPlan:
        """Solve one regional edition from a published package; writes nothing."""

        reader = self._terminology_reader()
        package = self.get_package(package_id)  # raises if unknown
        document_id = str(package["document_id"])
        if terminology_version is None:
            terminology_version = reader.latest_version(document_id)
            if terminology_version is None:
                raise RegionalError(
                    f"no terminology version published for {document_id!r}"
                )
        version = reader.terminology_version(document_id, terminology_version)
        snapshot_revisions = {
            str(item["segment_id"]): int(item["revision"])
            for item in package["source_snapshot"]
        }
        chunks = [
            ChunkView(
                translation_id=str(chunk["translation_id"]),
                source_ids=tuple(str(s) for s in chunk["source_ids"]),
                text=str(chunk["text"]),
            )
            for chunk in package["chunks"]
        ]
        quotes = self.list_regional_quotes(document_id, region)
        locks = self.list_regional_locks(document_id, region)
        return solve_regional(
            chunks=chunks,
            snapshot_revisions=snapshot_revisions,
            markers=version.resolutions,
            quotes=quotes,
            locks=locks,
            region=region,
            terminology_version=terminology_version,
        )

    def publish_regional_edition(
        self,
        package_id: str,
        region: str,
        edition_id: str,
        terminology_version: Optional[int] = None,
    ) -> dict[str, object]:
        """Freeze a solved regional edition.  Infeasible -> nothing written."""

        plan = self.solve_regional_edition(package_id, region, terminology_version)
        if not plan.ok:
            raise NoRegionalEdition(
                f"cannot build regional edition for {package_id!r} region {region!r}: "
                f"{plan.diagnosis.detail}",
                plan=plan,
            )
        package = self.get_package(package_id)
        document_id = str(package["document_id"])
        snapshot_revisions = {
            str(item["segment_id"]): int(item["revision"])
            for item in package["source_snapshot"]
        }
        base_chunks = {str(c["translation_id"]): c for c in package["chunks"]}
        chunks: list[dict[str, object]] = []
        for choice in plan.choices:
            base = base_chunks[choice.translation_id]
            source_ids = [str(s) for s in base["source_ids"]]
            chunks.append(
                {
                    "translation_id": choice.translation_id,
                    "source_ids": source_ids,
                    "source_revisions": [snapshot_revisions[s] for s in source_ids],
                    "text": choice.text,
                    "draft_id": choice.draft_id,
                    "assignments": dict(choice.assignments),
                }
            )
        concept_values = dict(plan.concept_values)
        rendered = "\n".join(choice.text for choice in plan.choices)
        identity = json.dumps(
            {
                "base_package_id": package_id,
                "document_id": document_id,
                "language": package["language"],
                "region": region,
                "terminology_version": plan.terminology_version,
                "source_snapshot": package["source_snapshot"],
                "chunks": chunks,
                "concept_values": concept_values,
                "total_cost": plan.total_cost,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        content_sha = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        content: dict[str, object] = {
            "format": REGIONAL_FORMAT,
            "edition_id": edition_id,
            "base_package_id": package_id,
            "document_id": document_id,
            "language": package["language"],
            "region": region,
            "terminology_version": plan.terminology_version,
            "source_snapshot": package["source_snapshot"],
            "chunks": chunks,
            "chosen_drafts": {
                choice.translation_id: choice.draft_id
                for choice in plan.choices
                if choice.draft_id is not None
            },
            "concept_values": concept_values,
            "total_cost": plan.total_cost,
            "rendered_text": rendered,
            "content_sha256": content_sha,
            "created_at": _now(),
        }
        conn = self._connect()
        try:
            with self._transaction(conn):
                existing = conn.execute(
                    "SELECT content_sha256, content_json FROM regional_editions"
                    " WHERE edition_id = ?",
                    (edition_id,),
                ).fetchone()
                if existing is not None:
                    if existing["content_sha256"] != content_sha:
                        raise PackageExistsImmutable(
                            f"regional edition {edition_id!r} already exists with "
                            "different frozen content"
                        )
                    return json.loads(existing["content_json"])
                conn.execute(
                    "INSERT INTO regional_editions (edition_id, document_id, language,"
                    " region, base_package_id, terminology_version, content_json,"
                    " content_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        edition_id,
                        document_id,
                        str(package["language"]),
                        region,
                        package_id,
                        plan.terminology_version,
                        json.dumps(content, ensure_ascii=False, sort_keys=True),
                        content_sha,
                        str(content["created_at"]),
                    ),
                )
        finally:
            conn.close()
        return content

    def get_regional_edition(self, edition_id: str) -> dict[str, object]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT content_json FROM regional_editions WHERE edition_id = ?",
                (edition_id,),
            ).fetchone()
            if row is None:
                raise PackageNotFound(f"regional edition {edition_id!r} not found")
            return json.loads(row["content_json"])
        finally:
            conn.close()

    def list_regional_editions(
        self, document_id: Optional[str] = None, region: Optional[str] = None
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if region is not None:
            clauses.append("region = ?")
            params.append(region)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT edition_id, document_id, language, region, base_package_id,"
                " terminology_version, content_sha256, created_at"
                " FROM regional_editions" + where + " ORDER BY created_at, edition_id",
                params,
            ).fetchall()
            return [
                {
                    "edition_id": row["edition_id"],
                    "document_id": row["document_id"],
                    "language": row["language"],
                    "region": row["region"],
                    "base_package_id": row["base_package_id"],
                    "terminology_version": int(row["terminology_version"]),
                    "content_sha256": row["content_sha256"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()


def open_repository(
    delivery_db: str, source_db: str
) -> tuple[DeliveryRepository, PublishedSourceReader]:
    source_reader = PublishedSourceReader(source_db)
    terminology_reader = PublishedTerminologyReader(source_db)
    repo = DeliveryRepository(delivery_db, source_reader, terminology_reader)
    return repo, source_reader
