"""Regional edition tests: the brief's worked example, solver rules, diagnosis."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

from delivery_system.delivery_repository import DeliveryRepository
from delivery_system.errors import (
    InvalidRegionalLock,
    InvalidRegionalQuote,
    NoRegionalEdition,
    PackageExistsImmutable,
    PackageNotFound,
    RegionalProblemTooLarge,
    RegionalQuoteExists,
)
from delivery_system.package_io import (
    validate_package,
    validate_regional_edition,
    write_package,
)
from delivery_system.regional import ChunkView, RegionalQuote, solve_regional
from delivery_system.source_catalog import PublishedSourceReader, SourceCatalog
from delivery_system.terminology import (
    MarkerResolution,
    PublishedTerminologyReader,
    TerminologyCatalog,
)

MATERIALS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "materials", "753"
)


def marker(marker_id, segment_id, concept, revision=1, spelling="port", chapter="ROOT"):
    return MarkerResolution(
        marker_id=marker_id,
        segment_id=segment_id,
        segment_revision=revision,
        spelling=spelling,
        chapter_id=chapter,
        binding_chapter_id=chapter,
        concept_id=concept,
    )


def quote(tid, draft_id, assignments, cost, text=None):
    return RegionalQuote(
        document_id="D",
        region="TW",
        translation_id=tid,
        draft_id=draft_id,
        text=text or f"text {draft_id}",
        assignments=tuple(sorted(assignments.items())),
        cost=cost,
    )


class PureSolverTests(unittest.TestCase):
    """The solver is pure: no database needed."""

    def test_brief_example_numbers(self):
        chunks = [
            ChunkView("T_A", ("S1",), "a"),
            ChunkView("T_B", ("S2",), "b"),
        ]
        markers = [marker("M1", "S1", "P"), marker("M2", "S2", "P")]
        quotes = [
            quote("T_A", "A1", {"M1": 1}, 0),
            quote("T_A", "A2", {"M1": 2}, 2),
            quote("T_B", "B1", {"M2": 1}, 5),
            quote("T_B", "B2", {"M2": 2}, 0),
        ]
        revisions = {"S1": 1, "S2": 1}
        unlocked = solve_regional(
            chunks=chunks, snapshot_revisions=revisions, markers=markers,
            quotes=quotes, locks={}, region="TW", terminology_version=1,
        )
        self.assertTrue(unlocked.ok)
        self.assertEqual(unlocked.total_cost, 2)
        self.assertEqual(dict(unlocked.concept_values), {"P": 2})
        locked = solve_regional(
            chunks=chunks, snapshot_revisions=revisions, markers=markers,
            quotes=quotes, locks={"P": 1}, region="TW", terminology_version=1,
        )
        self.assertTrue(locked.ok)
        self.assertEqual(locked.total_cost, 5)
        self.assertEqual(dict(locked.concept_values), {"P": 1})

    def test_consistency_forced_and_lexicographic_tiebreak(self):
        # Both consistent combinations cost 5; the smaller draft-id sequence wins.
        chunks = [ChunkView("T_A", ("S1",), "a"), ChunkView("T_B", ("S2",), "b")]
        markers = [marker("M1", "S1", "P"), marker("M2", "S2", "P")]
        quotes = [
            quote("T_A", "A1", {"M1": 1}, 0),
            quote("T_A", "A2", {"M1": 2}, 5),
            quote("T_B", "B1", {"M2": 1}, 5),
            quote("T_B", "B2", {"M2": 2}, 0),
        ]
        plan = solve_regional(
            chunks=chunks, snapshot_revisions={"S1": 1, "S2": 1}, markers=markers,
            quotes=quotes, locks={}, region="TW", terminology_version=1,
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.total_cost, 5)
        self.assertEqual([c.draft_id for c in plan.choices], ["A1", "B1"])
        self.assertEqual(dict(plan.concept_values), {"P": 1})

    def test_same_concept_twice_in_one_chunk_must_agree(self):
        chunks = [ChunkView("T_A", ("S1", "S2"), "a")]
        markers = [marker("M1", "S1", "P"), marker("M2", "S2", "P")]
        good = quote("T_A", "G", {"M1": 2, "M2": 2}, 3)
        bad = quote("T_A", "BAD", {"M1": 1, "M2": 2}, 0)  # cheaper but illegal
        plan = solve_regional(
            chunks=chunks, snapshot_revisions={"S1": 1, "S2": 1}, markers=markers,
            quotes=[good, bad], locks={}, region="TW", terminology_version=1,
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.total_cost, 3)
        self.assertEqual(plan.choices[0].draft_id, "G")

    def test_conflicting_draft_only_means_infeasible(self):
        chunks = [ChunkView("T_A", ("S1", "S2"), "a")]
        markers = [marker("M1", "S1", "P"), marker("M2", "S2", "P")]
        plan = solve_regional(
            chunks=chunks, snapshot_revisions={"S1": 1, "S2": 1}, markers=markers,
            quotes=[quote("T_A", "BAD", {"M1": 1, "M2": 2}, 0)],
            locks={}, region="TW", terminology_version=1,
        )
        self.assertFalse(plan.ok)
        self.assertEqual(plan.diagnosis.reason, "no_usable_draft")
        self.assertEqual(plan.diagnosis.rejected_drafts[0][1], "BAD")

    def test_marker_set_must_match_exactly(self):
        chunks = [ChunkView("T_A", ("S1", "S2"), "a")]
        markers = [marker("M1", "S1", "P"), marker("M2", "S2", "P")]
        quotes = [
            quote("T_A", "PARTIAL", {"M1": 1}, 0),          # missing M2
            quote("T_A", "EXTRA", {"M1": 1, "M2": 1, "M9": 1}, 0),  # unknown marker
            quote("T_A", "FULL", {"M1": 1, "M2": 1}, 4),
        ]
        plan = solve_regional(
            chunks=chunks, snapshot_revisions={"S1": 1, "S2": 1}, markers=markers,
            quotes=quotes, locks={}, region="TW", terminology_version=1,
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.choices[0].draft_id, "FULL")
        self.assertEqual(plan.total_cost, 4)

    def test_marker_revision_must_match_package_snapshot(self):
        chunks = [ChunkView("T_A", ("S1",), "a")]
        # Marker was indexed against S1@1, but the package froze S1@2.
        markers = [marker("M1", "S1", "P", revision=1)]
        plan = solve_regional(
            chunks=chunks, snapshot_revisions={"S1": 2}, markers=markers,
            quotes=[quote("T_A", "A1", {"M1": 1}, 0)],
            locks={}, region="TW", terminology_version=1,
        )
        self.assertTrue(plan.ok)
        self.assertIsNone(plan.choices[0].draft_id)  # original text kept
        self.assertEqual(plan.choices[0].text, "a")
        self.assertEqual(plan.total_cost, 0)

    def test_chunk_without_markers_keeps_original_text(self):
        chunks = [ChunkView("T_A", ("S1",), "keep me"), ChunkView("T_B", ("S2",), "b")]
        markers = [marker("M2", "S2", "P")]
        plan = solve_regional(
            chunks=chunks, snapshot_revisions={"S1": 1, "S2": 1}, markers=markers,
            quotes=[quote("T_B", "B1", {"M2": 1}, 1)],
            locks={}, region="TW", terminology_version=1,
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.choices[0].text, "keep me")
        self.assertIsNone(plan.choices[0].draft_id)
        self.assertEqual(plan.total_cost, 1)

    def test_lock_conflict_core_is_identified(self):
        chunks = [ChunkView("T_A", ("S1",), "a"), ChunkView("T_B", ("S2",), "b")]
        markers = [marker("M1", "S1", "P"), marker("M2", "S2", "P")]
        quotes = [
            quote("T_A", "A1", {"M1": 1}, 0),
            quote("T_B", "B1", {"M2": 1}, 0),
        ]
        plan = solve_regional(
            chunks=chunks, snapshot_revisions={"S1": 1, "S2": 1}, markers=markers,
            quotes=quotes, locks={"P": 2}, region="TW", terminology_version=1,
        )
        self.assertFalse(plan.ok)
        kinds = sorted(c["type"] for c in plan.diagnosis.conflicts)
        # The lock plus one chunk that cannot satisfy it is the irreducible core.
        self.assertEqual(kinds, ["chunk", "lock"])

    def test_lock_alone_in_core_when_no_chunk_offers_it(self):
        chunks = [ChunkView("T_A", ("S1",), "a")]
        markers = [marker("M1", "S1", "P")]
        plan = solve_regional(
            chunks=chunks, snapshot_revisions={"S1": 1}, markers=markers,
            quotes=[quote("T_A", "A1", {"M1": 1}, 0)],
            locks={"P": 2}, region="TW", terminology_version=1,
        )
        self.assertFalse(plan.ok)
        # Irreducible: the lock and the one chunk that cannot satisfy it.
        # Dropping either side leaves the remainder (vacuously) feasible.
        self.assertEqual(
            sorted((c["type"], c.get("concept"), c.get("translation_id"))
                   for c in plan.diagnosis.conflicts),
            [("chunk", None, "T_A"), ("lock", "P", None)],
        )

    def test_coupled_concepts_infeasible_core(self):
        chunks = [ChunkView("T_C", ("S1",), "c"), ChunkView("T_D", ("S2",), "d")]
        markers = [
            marker("M1", "S1", "P"), marker("M2", "S1", "R"),
            marker("M3", "S2", "P"), marker("M4", "S2", "R"),
        ]
        quotes = [
            quote("T_C", "C1", {"M1": 1, "M2": 1}, 0),
            quote("T_C", "C2", {"M1": 2, "M2": 2}, 0),
            quote("T_D", "D1", {"M3": 1, "M4": 2}, 0),
            quote("T_D", "D2", {"M3": 2, "M4": 1}, 0),
        ]
        plan = solve_regional(
            chunks=chunks, snapshot_revisions={"S1": 1, "S2": 1}, markers=markers,
            quotes=quotes, locks={}, region="TW", terminology_version=1,
        )
        self.assertFalse(plan.ok)
        self.assertEqual(plan.diagnosis.reason, "inconsistent_constraints")
        self.assertEqual(
            sorted(c["translation_id"] for c in plan.diagnosis.conflicts),
            ["T_C", "T_D"],
        )

    def test_coupled_concepts_feasible_when_aligned(self):
        chunks = [ChunkView("T_C", ("S1",), "c"), ChunkView("T_D", ("S2",), "d")]
        markers = [
            marker("M1", "S1", "P"), marker("M2", "S1", "R"),
            marker("M3", "S2", "P"), marker("M4", "S2", "R"),
        ]
        quotes = [
            quote("T_C", "C1", {"M1": 1, "M2": 1}, 0),
            quote("T_C", "C2", {"M1": 2, "M2": 2}, 0),
            quote("T_D", "D1", {"M3": 1, "M4": 1}, 0),
            quote("T_D", "D2", {"M3": 2, "M4": 2}, 0),
        ]
        plan = solve_regional(
            chunks=chunks, snapshot_revisions={"S1": 1, "S2": 1}, markers=markers,
            quotes=quotes, locks={}, region="TW", terminology_version=1,
        )
        self.assertTrue(plan.ok)
        self.assertEqual(dict(plan.concept_values), {"P": 1, "R": 1})

    def test_problem_size_limits(self):
        chunks = [ChunkView("T", tuple(f"S{i}" for i in range(13)), "x")]
        markers = [marker(f"M{i}", f"S{i}", f"C{i}") for i in range(13)]
        with self.assertRaises(RegionalProblemTooLarge):
            solve_regional(
                chunks=chunks,
                snapshot_revisions={f"S{i}": 1 for i in range(13)},
                markers=markers, quotes=[], locks={}, region="TW",
                terminology_version=1,
            )
        with self.assertRaises(InvalidRegionalLock):
            solve_regional(
                chunks=[ChunkView("T", ("S1",), "x")],
                snapshot_revisions={"S1": 1},
                markers=[marker("M1", "S1", "P")],
                quotes=[], locks={"P": 4}, region="TW", terminology_version=1,
            )


class RegionalRepoTests(unittest.TestCase):
    """End-to-end through both databases, exactly the brief's example."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.source_db = os.path.join(self.tmp, "source.db")
        self.delivery_db = os.path.join(self.tmp, "delivery.db")
        self.packages = os.path.join(self.tmp, "packages")
        os.makedirs(self.packages)
        self.catalog = SourceCatalog(self.source_db)
        self.terms = TerminologyCatalog(self.source_db)
        self.repo = DeliveryRepository(
            self.delivery_db,
            PublishedSourceReader(self.source_db),
            PublishedTerminologyReader(self.source_db),
        )
        self._build_port_doc()

    def tearDown(self):
        self._tmp.cleanup()

    def _build_port_doc(self):
        self.doc = "DEMO-PORT"
        self.catalog.create_document(
            self.doc,
            [
                {"segment_id": "S1", "revision": 1, "text": "The port on the panel is live."},
                {"segment_id": "S2", "revision": 1, "text": "The port is busy."},
            ],
        )
        self.terms.add_concept(self.doc, "P")
        self.terms.add_concept(self.doc, "Q")
        self.terms.add_chapter(self.doc, "ROOT")
        self.terms.add_chapter(self.doc, "CH_CIRCUIT", parent_id="ROOT")
        self.terms.add_chapter(self.doc, "CH_SHIPPING", parent_id="ROOT")
        self.terms.set_binding(self.doc, "ROOT", "port", "Q")
        self.terms.set_binding(self.doc, "CH_CIRCUIT", "port", "P")
        self.terms.assign_segment(self.doc, "S1", "CH_CIRCUIT")
        self.terms.assign_segment(self.doc, "S2", "CH_CIRCUIT")
        self.terms.add_marker(self.doc, "M1", "S1", 1, "port")
        self.terms.add_marker(self.doc, "M2", "S2", 1, "port")
        self.terms.publish(self.doc)
        self.repo.add_translation("T_A", self.doc, "ZH", "面板上的端口已通电。", [{"segment_id": "S1", "revision": 1}])
        self.repo.add_translation("T_B", self.doc, "ZH", "这个港口很繁忙。", [{"segment_id": "S2", "revision": 1}])
        self.pack = self.repo.publish_language(self.doc, "ZH", "PK_A")
        write_package(self.pack, os.path.join(self.packages, "PK_A.json"))
        self.repo.add_regional_quote(self.doc, "TW", "T_A", "A1", "面板上的端口已通电。", {"M1": 1}, 0)
        self.repo.add_regional_quote(self.doc, "TW", "T_A", "A2", "面板上的口岸已通电。", {"M1": 2}, 2)
        self.repo.add_regional_quote(self.doc, "TW", "T_B", "B1", "这个端口很繁忙。", {"M2": 1}, 5)
        self.repo.add_regional_quote(self.doc, "TW", "T_B", "B2", "这个口岸很繁忙。", {"M2": 2}, 0)

    def test_brief_example_end_to_end(self):
        # No lock: both blocks take 乙 (usage 2), total cost 2.
        plan = self.repo.solve_regional_edition("PK_A", "TW", 1)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.total_cost, 2)
        self.assertEqual(dict(plan.concept_values), {"P": 2})
        # Lock P=甲 (usage 1): total cost 5.
        self.repo.set_regional_lock(self.doc, "TW", "P", 1)
        plan = self.repo.solve_regional_edition("PK_A", "TW", 1)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.total_cost, 5)
        self.assertEqual(dict(plan.concept_values), {"P": 1})
        # Move S2's segment to the shipping chapter, publish terminology V2;
        # source text and translation bytes stay untouched.
        revision_before = self.catalog.published_product(self.doc).document_revision
        self.terms.assign_segment(self.doc, "S2", "CH_SHIPPING")
        self.terms.publish(self.doc)
        self.assertEqual(
            self.catalog.published_product(self.doc).document_revision, revision_before
        )
        # Same quotes, same lock: block 1 takes 甲, block 2 may take 乙, cost 0.
        plan = self.repo.solve_regional_edition("PK_A", "TW", 2)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.total_cost, 0)
        self.assertEqual(dict(plan.concept_values), {"P": 1, "Q": 2})
        self.assertEqual([c.draft_id for c in plan.choices], ["A1", "B2"])

    def test_edition_derives_only_from_published_package(self):
        with self.assertRaises(PackageNotFound):
            self.repo.solve_regional_edition("NO_SUCH_PACKAGE", "TW", 1)

    def test_base_package_still_valid_for_old_reader(self):
        self.assertEqual(validate_package(self.pack), [])
        proc = subprocess.run(
            [sys.executable, os.path.join(MATERIALS, "package_reader.py"),
             os.path.join(self.packages, "PK_A.json")],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("面板上的端口已通电。", proc.stdout)

    def test_publish_edition_freezes_and_validates(self):
        content = self.repo.publish_regional_edition("PK_A", "TW", "ED1", 1)
        self.assertEqual(validate_regional_edition(content), [])
        self.assertEqual(content["format"], "regional-edition/1")
        self.assertEqual(content["base_package_id"], "PK_A")
        self.assertEqual(content["terminology_version"], 1)
        self.assertEqual(content["total_cost"], 2)
        self.assertEqual(content["concept_values"], {"P": 2})
        self.assertEqual(content["chosen_drafts"], {"T_A": "A2", "T_B": "B2"})
        self.assertIn("口岸", content["rendered_text"])
        # Re-publishing identical content is idempotent.
        again = self.repo.publish_regional_edition("PK_A", "TW", "ED1", 1)
        self.assertEqual(again["content_sha256"], content["content_sha256"])

    def test_edition_immutable(self):
        self.repo.publish_regional_edition("PK_A", "TW", "ED1", 1)
        self.repo.set_regional_lock(self.doc, "TW", "P", 1)  # changes the solution
        with self.assertRaises(PackageExistsImmutable):
            self.repo.publish_regional_edition("PK_A", "TW", "ED1", 1)

    def test_reopen_reproduces_decision(self):
        content = self.repo.publish_regional_edition("PK_A", "TW", "ED1", 1)
        repo2 = DeliveryRepository(
            self.delivery_db,
            PublishedSourceReader(self.source_db),
            PublishedTerminologyReader(self.source_db),
        )
        plan2 = repo2.solve_regional_edition("PK_A", "TW", 1)
        self.assertTrue(plan2.ok)
        self.assertEqual(plan2.total_cost, 2)
        content2 = repo2.publish_regional_edition("PK_A", "TW", "ED1", 1)
        self.assertEqual(content2["content_sha256"], content["content_sha256"])

    def test_quote_validation(self):
        with self.assertRaises(InvalidRegionalQuote):
            self.repo.add_regional_quote(self.doc, "TW", "T_A", "X1", "x", {"M1": 1}, -1)
        with self.assertRaises(InvalidRegionalQuote):
            self.repo.add_regional_quote(self.doc, "TW", "T_A", "X2", "  ", {"M1": 1}, 0)
        with self.assertRaises(InvalidRegionalQuote):
            self.repo.add_regional_quote(self.doc, "TW", "T_A", "X3", "x", {"M1": 0}, 0)
        with self.assertRaises(InvalidRegionalQuote):
            self.repo.add_regional_quote(self.doc, "TW", "T_A", "X4", "x", {"M1": 4}, 0)
        with self.assertRaises(InvalidRegionalQuote):
            self.repo.add_regional_quote(self.doc, "TW", "T_A", "X5", "x", {}, 0)
        # Idempotent re-add, conflict on different content.
        self.repo.add_regional_quote(self.doc, "TW", "T_A", "A1", "面板上的端口已通电。", {"M1": 1}, 0)
        with self.assertRaises(RegionalQuoteExists):
            self.repo.add_regional_quote(self.doc, "TW", "T_A", "A1", "changed", {"M1": 1}, 0)

    def test_lock_validation(self):
        with self.assertRaises(InvalidRegionalLock):
            self.repo.set_regional_lock(self.doc, "TW", "P", 0)
        with self.assertRaises(InvalidRegionalLock):
            self.repo.set_regional_lock(self.doc, "TW", "P", 4)
        self.repo.set_regional_lock(self.doc, "TW", "P", 1)
        self.repo.set_regional_lock(self.doc, "TW", "P", 2)  # upsert
        self.assertEqual(self.repo.list_regional_locks(self.doc, "TW"), {"P": 2})
        self.repo.remove_regional_lock(self.doc, "TW", "P")
        self.assertEqual(self.repo.list_regional_locks(self.doc, "TW"), {})

    def test_infeasible_writes_nothing(self):
        self.repo.set_regional_lock(self.doc, "TW", "P", 3)  # no draft offers 3
        plan = self.repo.solve_regional_edition("PK_A", "TW", 1)
        self.assertFalse(plan.ok)
        kinds = sorted(c["type"] for c in plan.diagnosis.conflicts)
        # The lock plus one chunk that cannot satisfy it: irreducible core.
        self.assertEqual(kinds, ["chunk", "lock"])
        self.assertEqual(
            [c["concept"] for c in plan.diagnosis.conflicts if c["type"] == "lock"],
            ["P"],
        )
        with self.assertRaises(NoRegionalEdition):
            self.repo.publish_regional_edition("PK_A", "TW", "ED_X", 1)
        self.assertEqual(self.repo.list_regional_editions(self.doc), [])
        self.assertFalse(os.path.exists(os.path.join(self.packages, "ED_X.json")))

    def test_marker_revision_mismatch_keeps_original(self):
        # Source moves to revision 2, a fresh package freezes S1@2, but the
        # marker still speaks about S1@1: it does not apply to the new package.
        self.catalog.revise_segment(self.doc, "S1", "The panel port is live now.", revision=2)
        self.repo.add_translation(
            "T_A2", self.doc, "ZH", "面板上的端口现在已通电。", [{"segment_id": "S1", "revision": 2}]
        )
        pack2 = self.repo.publish_language(self.doc, "ZH", "PK_A2")
        self.assertIn({"segment_id": "S1", "revision": 2}, pack2["source_snapshot"])
        plan = self.repo.solve_regional_edition("PK_A2", "TW", 1)
        self.assertTrue(plan.ok)
        choice = {c.translation_id: c for c in plan.choices}["T_A2"]
        self.assertIsNone(choice.draft_id)
        self.assertEqual(choice.text, "面板上的端口现在已通电。")


if __name__ == "__main__":
    unittest.main()
