"""End-to-end regional-edition scenario.

Two documents, all synthetic text:

* ``DEMO-PORT`` — the two-block example from the brief.  Chapters:
  ``ROOT`` binds spelling "port" to concept Q (harbour), its child
  ``CH_CIRCUIT`` overrides "port" to concept P (electrical).  Two chunks
  both carry concept P; quotes price P=甲 at 0/5 and P=乙 at 2/0.  No lock
  -> both 乙, cost 2.  Lock P=甲 -> cost 5.  Moving the second segment to
  ``CH_SHIPPING`` (which inherits "port"->Q from ROOT) and publishing
  terminology version 2 — without touching source text or translation
  bytes — lets the second chunk take 乙 under the same lock: cost 0.
* ``DEMO-COUPLE`` — one sentence constrains two concepts at once; every
  local quote costs 0, yet no combination is consistent, so the solver
  reports an irreducible conflict set and writes nothing.

Everything is recomputed after closing and reopening the databases to show
that terminology resolution and regional decisions reproduce from a
specified version.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from .delivery_repository import DeliveryRepository
from .errors import NoRegionalEdition
from .package_io import validate_package, validate_regional_edition, write_package
from .source_catalog import PublishedSourceReader, SourceCatalog
from .terminology import PublishedTerminologyReader, TerminologyCatalog

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE_READER = os.path.join(os.path.dirname(HERE), "materials", "753", "package_reader.py")


def _client_read(package_path: str) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, PACKAGE_READER, package_path],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def run_regional_demo(out_dir: str) -> dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    packages_dir = os.path.join(out_dir, "packages")
    os.makedirs(packages_dir, exist_ok=True)
    source_db = os.path.join(out_dir, "source.db")
    delivery_db = os.path.join(out_dir, "delivery.db")

    catalog = SourceCatalog(source_db)
    terms = TerminologyCatalog(source_db)
    repo = DeliveryRepository(
        delivery_db, PublishedSourceReader(source_db), PublishedTerminologyReader(source_db)
    )

    report: dict[str, Any] = {"ok": True, "steps": [], "checks": []}

    def step(name: str, payload: dict[str, Any]) -> None:
        report["steps"].append({"name": name, **payload})

    def check(name: str, condition: bool, detail: Any = None) -> None:
        report["checks"].append({"name": name, "ok": bool(condition), "detail": detail})
        if not condition:
            report["ok"] = False

    # ------------------------------------------------------------------
    # DOC_A: the two-block / one-concept example
    # ------------------------------------------------------------------
    doc_a = "DEMO-PORT"
    catalog.create_document(
        doc_a,
        [
            {"segment_id": "S1", "revision": 1, "text": "The port on the panel is live."},
            {"segment_id": "S2", "revision": 1, "text": "The port is busy."},
        ],
    )
    terms.add_concept(doc_a, "P", label="electrical port")
    terms.add_concept(doc_a, "Q", label="harbour port")
    terms.add_chapter(doc_a, "ROOT", name="whole book")
    terms.add_chapter(doc_a, "CH_CIRCUIT", parent_id="ROOT", name="circuits")
    terms.add_chapter(doc_a, "CH_SHIPPING", parent_id="ROOT", name="shipping")
    # Same spelling, different chapters, different concepts.  CH_SHIPPING has
    # no own "port" binding: it inherits ROOT's (nearest binding upwards).
    terms.set_binding(doc_a, "ROOT", "port", "Q")
    terms.set_binding(doc_a, "CH_CIRCUIT", "port", "P")
    terms.assign_segment(doc_a, "S1", "CH_CIRCUIT")
    terms.assign_segment(doc_a, "S2", "CH_CIRCUIT")
    terms.add_marker(doc_a, "M1", "S1", 1, "port")
    terms.add_marker(doc_a, "M2", "S2", 1, "port")
    v1 = terms.publish(doc_a)
    step(
        "terminology_v1",
        {"version": v1.version, "marker_concepts": v1.marker_concepts()},
    )
    check("V1_both_markers_P", v1.marker_concepts() == {"M1": "P", "M2": "P"})

    repo.add_translation("T_A", doc_a, "ZH", "面板上的端口已通电。", [{"segment_id": "S1", "revision": 1}])
    repo.add_translation("T_B", doc_a, "ZH", "这个港口很繁忙。", [{"segment_id": "S2", "revision": 1}])
    pack_a = repo.publish_language(doc_a, "ZH", "PK_A")
    pack_a_path = os.path.join(packages_dir, "PK_A.json")
    write_package(pack_a, pack_a_path)
    step("publish_base_package", {"package_id": "PK_A", "rendered_text": pack_a["rendered_text"]})
    check("PK_A_valid", not validate_package(pack_a), validate_package(pack_a))
    client = _client_read(pack_a_path)
    check("old_reader_reads_PK_A", client["returncode"] == 0, client)

    # Regional quotes: usage 1 = 端口 (甲), usage 2 = 口岸 (乙).
    repo.add_regional_quote(doc_a, "TW", "T_A", "A1", "面板上的端口已通电。", {"M1": 1}, 0)
    repo.add_regional_quote(doc_a, "TW", "T_A", "A2", "面板上的口岸已通电。", {"M1": 2}, 2)
    repo.add_regional_quote(doc_a, "TW", "T_B", "B1", "这个端口很繁忙。", {"M2": 1}, 5)
    repo.add_regional_quote(doc_a, "TW", "T_B", "B2", "这个口岸很繁忙。", {"M2": 2}, 0)

    plan1 = repo.solve_regional_edition("PK_A", "TW", 1)
    step(
        "solve_unlocked",
        {
            "ok": plan1.ok,
            "total_cost": plan1.total_cost,
            "concept_values": dict(plan1.concept_values),
            "drafts": {c.translation_id: c.draft_id for c in plan1.choices},
        },
    )
    check("unlocked_cost_2", plan1.ok and plan1.total_cost == 2, plan1.total_cost)
    check(
        "unlocked_both_yi",
        plan1.ok and dict(plan1.concept_values) == {"P": 2},
        dict(plan1.concept_values),
    )

    repo.set_regional_lock(doc_a, "TW", "P", 1)
    plan2 = repo.solve_regional_edition("PK_A", "TW", 1)
    step(
        "solve_locked_jia",
        {
            "ok": plan2.ok,
            "total_cost": plan2.total_cost,
            "concept_values": dict(plan2.concept_values),
        },
    )
    check("locked_cost_5", plan2.ok and plan2.total_cost == 5, plan2.total_cost)
    check("locked_P_jia", plan2.ok and dict(plan2.concept_values) == {"P": 1})

    # Move S2 to the shipping chapter and publish terminology version 2.
    # Source text and translation bytes are untouched.
    revision_before = catalog.published_product(doc_a).document_revision
    terms.assign_segment(doc_a, "S2", "CH_SHIPPING")
    v2 = terms.publish(doc_a)
    revision_after = catalog.published_product(doc_a).document_revision
    step(
        "terminology_v2_after_move",
        {
            "version": v2.version,
            "marker_concepts": v2.marker_concepts(),
            "document_revision_unchanged": revision_before == revision_after,
        },
    )
    check("V2_M2_now_Q", v2.marker_concepts() == {"M1": "P", "M2": "Q"})
    check(
        "source_bytes_untouched",
        revision_before == revision_after,
        {"before": revision_before, "after": revision_after},
    )
    # Old terminology version stays readable: M2 is still P in version 1.
    reread_v1 = terms.terminology_version(doc_a, 1)
    check("V1_still_readable", reread_v1.marker_concepts() == {"M1": "P", "M2": "P"})

    # Same quotes, same lock P=1, but M2 now resolves to Q: cost drops to 0.
    plan3 = repo.solve_regional_edition("PK_A", "TW", 2)
    step(
        "solve_after_move",
        {
            "ok": plan3.ok,
            "total_cost": plan3.total_cost,
            "concept_values": dict(plan3.concept_values),
            "drafts": {c.translation_id: c.draft_id for c in plan3.choices},
        },
    )
    check("after_move_cost_0", plan3.ok and plan3.total_cost == 0, plan3.total_cost)
    check(
        "after_move_values",
        plan3.ok and dict(plan3.concept_values) == {"P": 1, "Q": 2},
        dict(plan3.concept_values),
    )
    ed_a3 = repo.publish_regional_edition("PK_A", "TW", "ED_A3", 2)
    ed_a3_path = os.path.join(packages_dir, "ED_A3.json")
    write_package(ed_a3, ed_a3_path)
    check("ED_A3_valid", not validate_regional_edition(ed_a3), validate_regional_edition(ed_a3))
    check(
        "ED_A3_text",
        "口岸" in ed_a3["rendered_text"] and "面板上的端口" in ed_a3["rendered_text"],
        ed_a3["rendered_text"],
    )

    # ------------------------------------------------------------------
    # DOC_B: one sentence binds two concepts; local minima do not assemble
    # ------------------------------------------------------------------
    doc_b = "DEMO-COUPLE"
    catalog.create_document(
        doc_b,
        [
            {"segment_id": "S1", "revision": 1, "text": "The port and the berth."},
            {"segment_id": "S2", "revision": 1, "text": "Port and berth again."},
        ],
    )
    terms.add_concept(doc_b, "P", label="port")
    terms.add_concept(doc_b, "R", label="berth")
    terms.add_chapter(doc_b, "ROOT", name="whole book")
    terms.set_binding(doc_b, "ROOT", "port", "P")
    terms.set_binding(doc_b, "ROOT", "berth", "R")
    terms.assign_segment(doc_b, "S1", "ROOT")
    terms.assign_segment(doc_b, "S2", "ROOT")
    terms.add_marker(doc_b, "M1", "S1", 1, "port")
    terms.add_marker(doc_b, "M2", "S1", 1, "berth")
    terms.add_marker(doc_b, "M3", "S2", 1, "port")
    terms.add_marker(doc_b, "M4", "S2", 1, "berth")
    terms.publish(doc_b)

    repo.add_translation("T_C", doc_b, "ZH", "港口与泊位。", [{"segment_id": "S1", "revision": 1}])
    repo.add_translation("T_D", doc_b, "ZH", "又见港口与泊位。", [{"segment_id": "S2", "revision": 1}])
    pack_b = repo.publish_language(doc_b, "ZH", "PK_B")
    write_package(pack_b, os.path.join(packages_dir, "PK_B.json"))

    # Each draft of T_C pins P and R together; T_D's drafts pin the opposite
    # diagonal.  Every local quote costs 0, yet no pair is consistent.
    repo.add_regional_quote(doc_b, "TW", "T_C", "C1", "港与泊。", {"M1": 1, "M2": 1}, 0)
    repo.add_regional_quote(doc_b, "TW", "T_C", "C2", "口与位。", {"M1": 2, "M2": 2}, 0)
    repo.add_regional_quote(doc_b, "TW", "T_D", "D1", "港配位。", {"M3": 1, "M4": 2}, 0)
    repo.add_regional_quote(doc_b, "TW", "T_D", "D2", "口配泊。", {"M3": 2, "M4": 1}, 0)

    plan_b = repo.solve_regional_edition("PK_B", "TW", 1)
    step(
        "solve_coupled_infeasible",
        {
            "ok": plan_b.ok,
            "diagnosis": None if plan_b.ok else plan_b.diagnosis.to_dict(),
        },
    )
    check("coupled_infeasible", not plan_b.ok)
    if not plan_b.ok:
        conflict_chunks = sorted(
            c["translation_id"] for c in plan_b.diagnosis.conflicts if c["type"] == "chunk"
        )
        check(
            "conflict_core_is_TC_TD",
            conflict_chunks == ["T_C", "T_D"],
            plan_b.diagnosis.conflicts,
        )
        check(
            "no_locks_in_core",
            not any(c["type"] == "lock" for c in plan_b.diagnosis.conflicts),
        )
    blocked = False
    try:
        repo.publish_regional_edition("PK_B", "TW", "ED_B1", 1)
    except NoRegionalEdition:
        blocked = True
    check("infeasible_publish_blocked", blocked)
    check(
        "no_half_book_written",
        repo.list_regional_editions(document_id=doc_b) == []
        and not os.path.exists(os.path.join(packages_dir, "ED_B1.json")),
    )

    # ------------------------------------------------------------------
    # Reopen everything: resolution and decisions reproduce from a version
    # ------------------------------------------------------------------
    repo2 = DeliveryRepository(
        delivery_db, PublishedSourceReader(source_db), PublishedTerminologyReader(source_db)
    )
    reader2 = PublishedTerminologyReader(source_db)
    v1_again = reader2.terminology_version(doc_a, 1)
    v2_again = reader2.terminology_version(doc_a, 2)
    check(
        "resolution_reproduces_after_reopen",
        v1_again.marker_concepts() == {"M1": "P", "M2": "P"}
        and v2_again.marker_concepts() == {"M1": "P", "M2": "Q"},
    )
    plan3_again = repo2.solve_regional_edition("PK_A", "TW", 2)
    ed_a3_again = repo2.publish_regional_edition("PK_A", "TW", "ED_A3", 2)
    check(
        "decision_reproduces_after_reopen",
        plan3_again.ok
        and plan3_again.total_cost == 0
        and ed_a3_again["content_sha256"] == ed_a3["content_sha256"],
        {"first": ed_a3["content_sha256"], "again": ed_a3_again["content_sha256"]},
    )
    plan_b_again = repo2.solve_regional_edition("PK_B", "TW", 1)
    check(
        "infeasibility_reproduces_after_reopen",
        not plan_b_again.ok
        and plan_b_again.diagnosis.to_dict() == plan_b.diagnosis.to_dict(),
    )

    report["editions"] = {"ED_A3": ed_a3}
    return report
