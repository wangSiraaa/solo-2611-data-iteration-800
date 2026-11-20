"""Command-line interface for the source catalog + delivery repository.

The source catalog and the delivery repository each own a separate SQLite
database.  The delivery side reads source state through a read-only
:class:`PublishedSourceReader`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

from .delivery_repository import DeliveryRepository
from .errors import DeliveryError, NoCoverError, NoRegionalEdition
from .package_io import (
    read_package,
    validate_package,
    validate_regional_edition,
    write_package,
)
from .source_catalog import PublishedSourceReader, SourceCatalog
from .terminology import PublishedTerminologyReader, TerminologyCatalog


def _default_document(catalog: Optional[SourceCatalog]) -> Optional[str]:
    if catalog is None:
        return None
    docs = catalog.list_documents()
    return docs[0]["document_id"] if len(docs) == 1 else None


def _open(args: argparse.Namespace, *, readonly_source: bool = False):
    if not os.path.exists(args.source_db):
        raise DeliveryError(f"source database {args.source_db!r} does not exist")
    reader = PublishedSourceReader(args.source_db)
    terminology = PublishedTerminologyReader(args.source_db)
    repo = DeliveryRepository(args.delivery_db, reader, terminology)
    if readonly_source:
        return None, repo
    return SourceCatalog(args.source_db), repo


def _emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _document(args: argparse.Namespace, catalog: Optional[SourceCatalog]) -> str:
    doc = getattr(args, "document", None) or _default_document(catalog)
    if not doc:
        raise DeliveryError(
            "document id required (pass --document; the catalog has 0 or >1 documents)"
        )
    return doc


def _package_path(args: argparse.Namespace, package_id: str) -> str:
    return os.path.join(args.packages_dir, f"{package_id}.json")


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
def cmd_init_materials(args: argparse.Namespace) -> int:
    with open(args.source, encoding="utf-8") as fh:
        doc = json.load(fh)
    document_id = args.document or str(doc["document_id"])
    catalog = SourceCatalog(args.source_db)
    repo = DeliveryRepository(args.delivery_db, PublishedSourceReader(args.source_db))
    try:
        catalog.create_document(
            document_id,
            [
                {
                    "segment_id": seg["segment_id"],
                    "revision": int(seg.get("revision", 1)),
                    "text": seg["text"],
                }
                for seg in doc["segments"]
            ],
            document_revision=int(doc.get("revision", 1)),
        )
    except DeliveryError as exc:
        if "already exists" not in str(exc):
            raise
    loaded = 0
    with open(args.translations, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            repo.add_translation(
                translation_id=item["translation_id"],
                document_id=document_id,
                language=item["language"],
                text=item["text"],
                source_refs=item["source_refs"],
            )
            loaded += 1
    _emit(
        {
            "document_id": document_id,
            "segments": len(catalog.published_product(document_id).segments),
            "translations_loaded": loaded,
            "source_db": args.source_db,
            "delivery_db": args.delivery_db,
        }
    )
    return 0


def cmd_source_show(args: argparse.Namespace) -> int:
    catalog, _ = _open(args)
    product = catalog.published_product(_document(args, catalog))
    _emit(
        {
            "document_id": product.document_id,
            "document_revision": product.document_revision,
            "segments": [
                {
                    "position": seg.position,
                    "segment_id": seg.segment_id,
                    "revision": seg.revision,
                    "text": seg.text,
                }
                for seg in product.segments
            ],
        }
    )
    return 0


def cmd_source_revise(args: argparse.Namespace) -> int:
    catalog, _ = _open(args)
    product = catalog.revise_segment(
        _document(args, catalog), args.segment_id, args.text, revision=args.revision
    )
    _emit({"document_revision": product.document_revision, "snapshot": product.snapshot()})
    return 0


def cmd_source_add(args: argparse.Namespace) -> int:
    catalog, _ = _open(args)
    product = catalog.add_segment(
        _document(args, catalog),
        args.segment_id,
        args.text,
        position=args.position,
        revision=args.revision,
    )
    _emit({"document_revision": product.document_revision, "snapshot": product.snapshot()})
    return 0


def cmd_source_move(args: argparse.Namespace) -> int:
    catalog, _ = _open(args)
    product = catalog.move_segment(
        _document(args, catalog), args.segment_id, args.position
    )
    _emit({"document_revision": product.document_revision, "snapshot": product.snapshot()})
    return 0


def cmd_source_remove(args: argparse.Namespace) -> int:
    catalog, _ = _open(args)
    product = catalog.remove_segment(_document(args, catalog), args.segment_id)
    _emit({"document_revision": product.document_revision, "snapshot": product.snapshot()})
    return 0


def cmd_source_releases(args: argparse.Namespace) -> int:
    catalog, _ = _open(args)
    _emit(catalog.list_releases(_document(args, catalog)))
    return 0


def cmd_translation_add(args: argparse.Namespace) -> int:
    catalog, repo = _open(args)
    refs = json.loads(args.refs)
    entry = repo.add_translation(
        translation_id=args.translation_id,
        document_id=_document(args, catalog),
        language=args.language,
        text=args.text,
        source_refs=refs,
    )
    _emit(
        {
            "translation_id": entry.translation_id,
            "language": entry.language,
            "source_refs": [
                {"segment_id": r.segment_id, "revision": r.revision}
                for r in entry.source_refs
            ],
            "text": entry.text,
        }
    )
    return 0


def cmd_assess(args: argparse.Namespace) -> int:
    catalog, repo = _open(args)
    document_id = _document(args, catalog)
    result = repo.plan_cover(document_id, args.language)
    entries = repo.assess(document_id, args.language)
    _emit(
        {
            "document_id": document_id,
            "language": args.language,
            "entries": [
                {
                    "translation_id": item.entry.translation_id,
                    "state": item.state,
                    "issues": list(item.issues),
                    "interval": None
                    if item.start is None
                    else [item.start, item.end],
                }
                for item in entries
            ],
            "cover_ok": result.ok,
            "diagnosis": None if result.diagnosis is None else result.diagnosis.to_dict(),
        }
    )
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    catalog, repo = _open(args)
    document_id = _document(args, catalog)
    result = repo.plan_cover(document_id, args.language)
    payload: dict[str, Any] = {
        "document_id": document_id,
        "language": args.language,
        "cover_ok": result.ok,
    }
    if result.ok:
        payload["chunks"] = [
            {
                "translation_id": chunk.translation_id,
                "source_ids": list(chunk.source_ids),
                "source_revisions": list(chunk.source_revisions),
                "text": chunk.text,
            }
            for chunk in result.chunks
        ]
        payload["rendered_text"] = result.rendered_text
    else:
        payload["diagnosis"] = result.diagnosis.to_dict()
    _emit(payload)
    return 0 if result.ok else 3


def cmd_publish(args: argparse.Namespace) -> int:
    catalog, repo = _open(args)
    document_id = _document(args, catalog)
    try:
        content = repo.publish_language(document_id, args.language, args.package_id)
    except NoCoverError as exc:
        result = exc.result
        payload = {
            "published": False,
            "package_id": args.package_id,
            "language": args.language,
            "diagnosis": None
            if result is None
            else result.diagnosis.to_dict(),
        }
        _emit(payload)
        return 3
    os.makedirs(args.packages_dir, exist_ok=True)
    path = _package_path(args, args.package_id)
    write_package(content, path)
    problems = validate_package(content)
    _emit(
        {
            "published": True,
            "package_id": args.package_id,
            "path": path,
            "problems": problems,
            "content_sha256": content["content_sha256"],
        }
    )
    return 0 if not problems else 4


def cmd_package_show(args: argparse.Namespace) -> int:
    content = read_package(args.package)
    _emit({"package": content, "problems": validate_package(content)})
    return 0


def cmd_package_list(args: argparse.Namespace) -> int:
    _, repo = _open(args, readonly_source=True)
    _emit(
        repo.list_packages(
            document_id=getattr(args, "document", None), language=args.language
        )
    )
    return 0


def cmd_run_operations(args: argparse.Namespace) -> int:
    catalog = SourceCatalog(args.source_db)
    repo = DeliveryRepository(args.delivery_db, PublishedSourceReader(args.source_db))
    os.makedirs(args.packages_dir, exist_ok=True)
    with open(args.operations, encoding="utf-8") as fh:
        operations = [json.loads(line) for line in fh if line.strip()]

    if args.document:
        document_id = args.document
    else:
        docs = catalog.list_documents()
        document_id = docs[0]["document_id"] if docs else None
    if not document_id:
        raise DeliveryError("no document available for operations")

    log: list[dict[str, Any]] = []
    ok = True
    for index, op in enumerate(operations, 1):
        kind = op.get("operation")
        record: dict[str, Any] = {"step": index, "operation": kind}
        if kind == "publish_language":
            try:
                content = repo.publish_language(
                    document_id, op["language"], op["save_as"]
                )
            except NoCoverError as exc:
                record["published"] = False
                record["diagnosis"] = exc.result.diagnosis.to_dict()
                ok = False
                log.append(record)
                break
            path = _package_path(args, op["save_as"])
            write_package(content, path)
            problems = validate_package(content)
            record["published"] = True
            record["package_id"] = op["save_as"]
            record["path"] = path
            record["problems"] = problems
            record["source_snapshot"] = content["source_snapshot"]
            record["translation_ids"] = [
                c["translation_id"] for c in content["chunks"]
            ]
            if problems:
                ok = False
        elif kind == "try_publish_language":
            result = repo.plan_cover(document_id, op["language"])
            expected = op.get("expect")
            actual = "cover_ok" if result.ok else result.diagnosis.reason
            record["cover_ok"] = result.ok
            record["expected"] = expected
            record["actual"] = actual
            if result.ok:
                record["translation_ids"] = [
                    c.translation_id for c in result.chunks
                ]
            else:
                record["diagnosis"] = result.diagnosis.to_dict()
            if expected is not None and expected != actual:
                record["mismatch"] = True
                ok = False
        elif kind == "revise_source_segment":
            seg = op["segment"]
            product = catalog.revise_segment(
                document_id,
                seg["segment_id"],
                seg["text"],
                revision=seg.get("revision"),
            )
            record["document_revision"] = product.document_revision
            record["segment"] = {
                "segment_id": seg["segment_id"],
                "revision": catalog.get_segment(
                    document_id, seg["segment_id"]
                ).revision,
            }
        elif kind == "add_translation":
            item = op["translation"]
            repo.add_translation(
                translation_id=item["translation_id"],
                document_id=document_id,
                language=item["language"],
                text=item["text"],
                source_refs=item["source_refs"],
            )
            record["translation_id"] = item["translation_id"]
        elif kind == "add_source_segment":
            seg = op["segment"]
            product = catalog.add_segment(
                document_id,
                seg["segment_id"],
                seg["text"],
                position=seg.get("position"),
                revision=int(seg.get("revision", 1)),
            )
            record["document_revision"] = product.document_revision
        elif kind == "move_source_segment":
            seg = op["segment"]
            product = catalog.move_segment(
                document_id, seg["segment_id"], int(seg["position"])
            )
            record["document_revision"] = product.document_revision
        elif kind == "remove_source_segment":
            seg = op["segment"]
            product = catalog.remove_segment(document_id, seg["segment_id"])
            record["document_revision"] = product.document_revision
        elif kind == "read_old_package":
            path = _package_path(args, op["package"])
            content = read_package(path)
            problems = validate_package(content)
            record["package_id"] = op["package"]
            record["path"] = path
            record["source_snapshot"] = content["source_snapshot"]
            record["translation_ids"] = [c["translation_id"] for c in content["chunks"]]
            record["rendered_text"] = content["rendered_text"]
            record["problems"] = problems
            if problems:
                ok = False
        elif kind == "assess_language":
            entries = repo.assess(document_id, op["language"])
            record["states"] = {
                item.entry.translation_id: item.state for item in entries
            }
        else:
            record["error"] = f"unknown operation {kind!r}"
            ok = False
            log.append(record)
            break
        log.append(record)

    _emit({"ok": ok, "document_id": document_id, "log": log})
    return 0 if ok else 3


# ----------------------------------------------------------------------
# terminology book commands (source side)
# ----------------------------------------------------------------------
def _terminology_document(args: argparse.Namespace) -> str:
    catalog = SourceCatalog(args.source_db)
    return _document(args, catalog)


def cmd_term_concept_add(args: argparse.Namespace) -> int:
    terms = TerminologyCatalog(args.source_db)
    terms.add_concept(_terminology_document(args), args.concept_id, label=args.label)
    _emit({"concept_id": args.concept_id, "label": args.label})
    return 0


def cmd_term_chapter_add(args: argparse.Namespace) -> int:
    terms = TerminologyCatalog(args.source_db)
    terms.add_chapter(
        _terminology_document(args), args.chapter_id, parent_id=args.parent, name=args.name
    )
    _emit({"chapter_id": args.chapter_id, "parent_id": args.parent, "name": args.name})
    return 0


def cmd_term_bind(args: argparse.Namespace) -> int:
    terms = TerminologyCatalog(args.source_db)
    terms.set_binding(_terminology_document(args), args.chapter_id, args.spelling, args.concept_id)
    _emit(
        {
            "chapter_id": args.chapter_id,
            "spelling": args.spelling,
            "concept_id": args.concept_id,
        }
    )
    return 0


def cmd_term_assign(args: argparse.Namespace) -> int:
    terms = TerminologyCatalog(args.source_db)
    terms.assign_segment(_terminology_document(args), args.segment_id, args.chapter_id)
    _emit({"segment_id": args.segment_id, "chapter_id": args.chapter_id})
    return 0


def cmd_term_marker_add(args: argparse.Namespace) -> int:
    terms = TerminologyCatalog(args.source_db)
    terms.add_marker(
        _terminology_document(args),
        args.marker_id,
        args.segment_id,
        args.segment_revision,
        args.spelling,
    )
    _emit(
        {
            "marker_id": args.marker_id,
            "segment_id": args.segment_id,
            "segment_revision": args.segment_revision,
            "spelling": args.spelling,
        }
    )
    return 0


def cmd_term_marker_remove(args: argparse.Namespace) -> int:
    terms = TerminologyCatalog(args.source_db)
    terms.remove_marker(_terminology_document(args), args.marker_id)
    _emit({"removed": args.marker_id})
    return 0


def cmd_term_publish(args: argparse.Namespace) -> int:
    terms = TerminologyCatalog(args.source_db)
    version = terms.publish(_terminology_document(args))
    _emit(
        {
            "version": version.version,
            "document_id": version.document_id,
            "markers": len(version.markers),
            "resolutions": [
                {"marker_id": r.marker_id, "concept_id": r.concept_id}
                for r in version.resolutions
            ],
        }
    )
    return 0


def cmd_term_list(args: argparse.Namespace) -> int:
    terms = TerminologyCatalog(args.source_db)
    _emit(terms.list_versions(_terminology_document(args)))
    return 0


def cmd_term_show(args: argparse.Namespace) -> int:
    document_id = _terminology_document(args)
    reader = PublishedTerminologyReader(args.source_db)
    version = args.version
    if version is None:
        version = reader.latest_version(document_id)
        if version is None:
            raise DeliveryError(f"no terminology version published for {document_id!r}")
    _emit(reader.terminology_version(document_id, version).to_dict())
    return 0


# ----------------------------------------------------------------------
# regional edition commands (delivery side; source reached read-only)
# ----------------------------------------------------------------------
def cmd_regional_quote_add(args: argparse.Namespace) -> int:
    catalog, repo = _open(args)
    document_id = _document(args, catalog)
    assignments = json.loads(args.assignments)
    quote = repo.add_regional_quote(
        document_id=document_id,
        region=args.region,
        translation_id=args.translation_id,
        draft_id=args.draft_id,
        text=args.text,
        assignments={str(k): int(v) for k, v in assignments.items()},
        cost=args.cost,
    )
    _emit(
        {
            "region": quote.region,
            "translation_id": quote.translation_id,
            "draft_id": quote.draft_id,
            "cost": quote.cost,
            "assignments": dict(quote.assignments),
        }
    )
    return 0


def cmd_regional_lock_set(args: argparse.Namespace) -> int:
    catalog, repo = _open(args)
    document_id = _document(args, catalog)
    repo.set_regional_lock(document_id, args.region, args.concept_id, args.value)
    _emit({"region": args.region, "concept_id": args.concept_id, "value": args.value})
    return 0


def cmd_regional_lock_clear(args: argparse.Namespace) -> int:
    catalog, repo = _open(args)
    document_id = _document(args, catalog)
    repo.remove_regional_lock(document_id, args.region, args.concept_id)
    _emit({"region": args.region, "cleared": args.concept_id})
    return 0


def cmd_regional_locks(args: argparse.Namespace) -> int:
    catalog, repo = _open(args)
    document_id = _document(args, catalog)
    _emit(repo.list_regional_locks(document_id, args.region))
    return 0


def _plan_payload(plan) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": plan.ok,
        "region": plan.region,
        "terminology_version": plan.terminology_version,
    }
    if plan.ok:
        payload["total_cost"] = plan.total_cost
        payload["concept_values"] = dict(plan.concept_values)
        payload["choices"] = [
            {
                "translation_id": choice.translation_id,
                "draft_id": choice.draft_id,
                "text": choice.text,
                "assignments": dict(choice.assignments),
            }
            for choice in plan.choices
        ]
    else:
        payload["diagnosis"] = plan.diagnosis.to_dict()
    return payload


def cmd_regional_solve(args: argparse.Namespace) -> int:
    _, repo = _open(args, readonly_source=True)
    plan = repo.solve_regional_edition(args.package_id, args.region, args.terminology_version)
    payload = _plan_payload(plan)
    payload["package_id"] = args.package_id
    if plan.ok and args.save_as:
        content = repo.publish_regional_edition(
            args.package_id, args.region, args.save_as, args.terminology_version
        )
        os.makedirs(args.packages_dir, exist_ok=True)
        path = os.path.join(args.packages_dir, f"{args.save_as}.json")
        write_package(content, path)
        payload["saved_as"] = args.save_as
        payload["path"] = path
        payload["problems"] = validate_regional_edition(content)
    _emit(payload)
    return 0 if plan.ok else 3


def cmd_regional_publish(args: argparse.Namespace) -> int:
    _, repo = _open(args, readonly_source=True)
    try:
        content = repo.publish_regional_edition(
            args.package_id, args.region, args.edition_id, args.terminology_version
        )
    except NoRegionalEdition as exc:
        payload = {
            "published": False,
            "edition_id": args.edition_id,
            "package_id": args.package_id,
            "diagnosis": None if exc.plan is None else exc.plan.diagnosis.to_dict(),
        }
        _emit(payload)
        return 3
    os.makedirs(args.packages_dir, exist_ok=True)
    path = os.path.join(args.packages_dir, f"{args.edition_id}.json")
    write_package(content, path)
    problems = validate_regional_edition(content)
    _emit(
        {
            "published": True,
            "edition_id": args.edition_id,
            "path": path,
            "problems": problems,
            "total_cost": content["total_cost"],
            "content_sha256": content["content_sha256"],
        }
    )
    return 0 if not problems else 4


def cmd_regional_edition_show(args: argparse.Namespace) -> int:
    _, repo = _open(args, readonly_source=True)
    content = repo.get_regional_edition(args.edition_id)
    _emit({"edition": content, "problems": validate_regional_edition(content)})
    return 0


def cmd_regional_edition_list(args: argparse.Namespace) -> int:
    _, repo = _open(args, readonly_source=True)
    _emit(repo.list_regional_editions(region=args.region))
    return 0


def cmd_demo_regional(args: argparse.Namespace) -> int:
    """End-to-end regional-edition scenario in a throw-away temp directory."""

    import tempfile

    from .demo_regional import run_regional_demo

    with tempfile.TemporaryDirectory() as tmp:
        report = run_regional_demo(tmp)
    _emit(report)
    return 0 if report["ok"] else 3


def cmd_demo(args: argparse.Namespace) -> int:
    """End-to-end run over materials/753 using a throw-away temp directory."""

    import tempfile

    from .demo import run_demo

    with tempfile.TemporaryDirectory() as tmp:
        report = run_demo(tmp)
    _emit(report)
    return 0 if report["ok"] else 3


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="delivery",
        description="Source-segment catalog + multilingual delivery repository.",
    )
    p.add_argument("--source-db", default="data/source.db", help="source catalog SQLite")
    p.add_argument(
        "--delivery-db", default="data/delivery.db", help="delivery SQLite"
    )
    p.add_argument(
        "--packages-dir", default="data/packages", help="where frozen packages are written"
    )
    p.add_argument("--document", default=None, help="document id (when ambiguous)")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(func=func)
        return sp

    sp = add("init-materials", cmd_init_materials, "load source.json + translations.jsonl")
    sp.add_argument("--source", required=True)
    sp.add_argument("--translations", required=True)

    add("source-show", cmd_source_show, "show the published source product")

    sp = add("source-revise", cmd_source_revise, "publish a new segment revision")
    sp.add_argument("segment_id")
    sp.add_argument("text")
    sp.add_argument("--revision", type=int, default=None)

    sp = add("source-add", cmd_source_add, "add a segment")
    sp.add_argument("segment_id")
    sp.add_argument("text")
    sp.add_argument("--position", type=int, default=None)
    sp.add_argument("--revision", type=int, default=1)

    sp = add("source-move", cmd_source_move, "reorder a segment by identity")
    sp.add_argument("segment_id")
    sp.add_argument("position", type=int)

    sp = add("source-remove", cmd_source_remove, "remove a segment")
    sp.add_argument("segment_id")

    add("source-releases", cmd_source_releases, "list source releases")

    sp = add("translation-add", cmd_translation_add, "add a translation entry")
    sp.add_argument("translation_id")
    sp.add_argument("language")
    sp.add_argument("text")
    sp.add_argument("--refs", required=True, help="JSON list of {segment_id,revision}")

    sp = add("assess", cmd_assess, "assess per-entry states for a language")
    sp.add_argument("language")

    sp = add("plan", cmd_plan, "solve a cover without publishing")
    sp.add_argument("language")

    sp = add("publish", cmd_publish, "solve and freeze a package")
    sp.add_argument("language")
    sp.add_argument("package_id")

    sp = add("package-show", cmd_package_show, "read and validate a package file")
    sp.add_argument("package")

    sp = add("package-list", cmd_package_list, "list frozen packages")
    sp.add_argument("--language", default=None)

    sp = add("run-operations", cmd_run_operations, "replay an operations.jsonl")
    sp.add_argument("--operations", required=True)

    # terminology book (source side)
    sp = add("term-concept-add", cmd_term_concept_add, "declare a concept")
    sp.add_argument("concept_id")
    sp.add_argument("--label", default="")

    sp = add("term-chapter-add", cmd_term_chapter_add, "add a chapter to the tree")
    sp.add_argument("chapter_id")
    sp.add_argument("--parent", default=None)
    sp.add_argument("--name", default="")

    sp = add("term-bind", cmd_term_bind, "bind a spelling to a concept in a chapter")
    sp.add_argument("chapter_id")
    sp.add_argument("spelling")
    sp.add_argument("concept_id")

    sp = add("term-assign", cmd_term_assign, "assign a segment to a chapter")
    sp.add_argument("segment_id")
    sp.add_argument("chapter_id")

    sp = add("term-marker-add", cmd_term_marker_add, "hand-index one occurrence")
    sp.add_argument("marker_id")
    sp.add_argument("segment_id")
    sp.add_argument("segment_revision", type=int)
    sp.add_argument("spelling")

    sp = add("term-marker-remove", cmd_term_marker_remove, "drop a marker")
    sp.add_argument("marker_id")

    add("term-publish", cmd_term_publish, "freeze a new terminology version")

    add("term-list", cmd_term_list, "list terminology versions")

    sp = add("term-show", cmd_term_show, "show one terminology version (read-only)")
    sp.add_argument("--version", type=int, default=None)

    # regional editions (delivery side)
    sp = add("regional-quote-add", cmd_regional_quote_add, "offer a whole-sentence draft")
    sp.add_argument("region")
    sp.add_argument("translation_id")
    sp.add_argument("draft_id")
    sp.add_argument("--text", required=True)
    sp.add_argument("--assignments", required=True, help="JSON {marker_id: usage}")
    sp.add_argument("--cost", type=int, required=True)

    sp = add("regional-lock-set", cmd_regional_lock_set, "pre-lock a concept's usage")
    sp.add_argument("region")
    sp.add_argument("concept_id")
    sp.add_argument("value", type=int)

    sp = add("regional-lock-clear", cmd_regional_lock_clear, "remove a concept lock")
    sp.add_argument("region")
    sp.add_argument("concept_id")

    sp = add("regional-locks", cmd_regional_locks, "list a region's locks")
    sp.add_argument("region")

    sp = add("regional-solve", cmd_regional_solve, "solve an edition from a package")
    sp.add_argument("region")
    sp.add_argument("package_id")
    sp.add_argument("--terminology-version", type=int, default=None)
    sp.add_argument("--save-as", default=None, help="also freeze under this edition id")

    sp = add("regional-publish", cmd_regional_publish, "solve and freeze an edition")
    sp.add_argument("region")
    sp.add_argument("package_id")
    sp.add_argument("edition_id")
    sp.add_argument("--terminology-version", type=int, default=None)

    sp = add("regional-edition-show", cmd_regional_edition_show, "show a frozen edition")
    sp.add_argument("edition_id")

    sp = add("regional-edition-list", cmd_regional_edition_list, "list frozen editions")
    sp.add_argument("--region", default=None)

    add("demo", cmd_demo, "run the end-to-end materials/753 demo")
    add("demo-regional", cmd_demo_regional, "run the regional-edition demo")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except DeliveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
