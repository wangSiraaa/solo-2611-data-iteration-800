"""Terminology book tests: resolution rules, forbidden shapes, versioning."""

from __future__ import annotations

import os
import tempfile
import unittest

from delivery_system.errors import (
    ChapterCycle,
    ChapterExists,
    ChapterNotFound,
    ConceptExists,
    DanglingSegment,
    MarkerExists,
    SegmentNotFound,
    TerminologyVersionNotFound,
    UndefinedMarker,
)
from delivery_system.source_catalog import SourceCatalog
from delivery_system.terminology import PublishedTerminologyReader, TerminologyCatalog


class TerminologyCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "source.db")
        self.catalog = SourceCatalog(self.db)
        self.terms = TerminologyCatalog(self.db)
        self.catalog.create_document(
            "D",
            [
                {"segment_id": "S1", "text": "a"},
                {"segment_id": "S2", "text": "b"},
                {"segment_id": "S3", "text": "c"},
            ],
        )
        self.terms.add_concept("D", "P")
        self.terms.add_concept("D", "Q")
        self.terms.add_chapter("D", "ROOT")
        self.terms.add_chapter("D", "ELEC", parent_id="ROOT")
        self.terms.add_chapter("D", "SHIP", parent_id="ROOT")

    def tearDown(self):
        self._tmp.cleanup()

    def assign_all(self, chapter="ROOT"):
        for sid in ("S1", "S2", "S3"):
            self.terms.assign_segment("D", sid, chapter)


class ResolutionTests(TerminologyCase):
    def test_nearest_binding_upwards(self):
        # ROOT binds port->Q; ELEC overrides port->P; SHIP inherits ROOT's.
        self.terms.set_binding("D", "ROOT", "port", "Q")
        self.terms.set_binding("D", "ELEC", "port", "P")
        self.terms.assign_segment("D", "S1", "ELEC")
        self.terms.assign_segment("D", "S2", "SHIP")
        self.terms.assign_segment("D", "S3", "ROOT")
        self.terms.add_marker("D", "M1", "S1", 1, "port")
        self.terms.add_marker("D", "M2", "S2", 1, "port")
        self.terms.add_marker("D", "M3", "S3", 1, "port")
        version = self.terms.publish("D")
        self.assertEqual(
            version.marker_concepts(), {"M1": "P", "M2": "Q", "M3": "Q"}
        )
        by_id = {r.marker_id: r for r in version.resolutions}
        # M2's binding comes from ROOT, the nearest chapter upwards.
        self.assertEqual(by_id["M2"].binding_chapter_id, "ROOT")
        self.assertEqual(by_id["M2"].chapter_id, "SHIP")
        self.assertEqual(by_id["M1"].binding_chapter_id, "ELEC")

    def test_same_concept_different_spellings_per_chapter(self):
        # Concept P is "port" in ELEC but "interface" in SHIP.
        self.terms.set_binding("D", "ELEC", "port", "P")
        self.terms.set_binding("D", "SHIP", "interface", "P")
        self.terms.assign_segment("D", "S1", "ELEC")
        self.terms.assign_segment("D", "S2", "SHIP")
        self.terms.assign_segment("D", "S3", "ROOT")
        self.terms.set_binding("D", "ROOT", "port", "Q")  # S3 resolves too
        self.terms.add_marker("D", "M1", "S1", 1, "port")
        self.terms.add_marker("D", "M2", "S2", 1, "interface")
        self.terms.publish("D")
        version = self.terms.terminology_version("D", 1)
        self.assertEqual(version.marker_concepts()["M1"], "P")
        self.assertEqual(version.marker_concepts()["M2"], "P")

    def test_same_spelling_different_concepts_per_chapter(self):
        self.terms.set_binding("D", "ELEC", "port", "P")
        self.terms.set_binding("D", "SHIP", "port", "Q")
        self.terms.assign_segment("D", "S1", "ELEC")
        self.terms.assign_segment("D", "S2", "SHIP")
        self.terms.assign_segment("D", "S3", "ELEC")
        self.terms.add_marker("D", "M1", "S1", 1, "port")
        self.terms.add_marker("D", "M2", "S2", 1, "port")
        self.terms.publish("D")
        version = self.terms.terminology_version("D", 1)
        self.assertEqual(version.marker_concepts(), {"M1": "P", "M2": "Q"})

    def test_one_spelling_one_concept_per_chapter(self):
        self.terms.set_binding("D", "ELEC", "port", "P")
        # Re-binding the same spelling in the same chapter replaces, not stacks.
        self.terms.set_binding("D", "ELEC", "port", "Q")
        state = self.terms.current_state("D")
        bindings = [
            b for b in state["bindings"] if b["chapter_id"] == "ELEC" and b["spelling"] == "port"
        ]
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["concept_id"], "Q")


class ForbiddenShapeTests(TerminologyCase):
    def test_chapter_cycle_rejected(self):
        self.terms.add_chapter("D", "SUB", parent_id="ELEC")
        with self.assertRaises(ChapterCycle):
            self.terms.set_chapter_parent("D", "ROOT", "SUB")
        with self.assertRaises(ChapterCycle):
            self.terms.set_chapter_parent("D", "ELEC", "SUB")
        # Self-parenting is a cycle too.
        with self.assertRaises(ChapterCycle):
            self.terms.add_chapter("D", "X", parent_id="X")

    def test_dangling_segment_rejected_at_publish(self):
        self.terms.set_binding("D", "ROOT", "port", "P")
        self.terms.assign_segment("D", "S1", "ROOT")
        self.terms.assign_segment("D", "S2", "ROOT")
        # S3 has no chapter: publishing must fail.
        with self.assertRaises(DanglingSegment):
            self.terms.publish("D")

    def test_assignment_requires_real_segment_and_chapter(self):
        with self.assertRaises(SegmentNotFound):
            self.terms.assign_segment("D", "S9", "ROOT")
        with self.assertRaises(ChapterNotFound):
            self.terms.assign_segment("D", "S1", "NOWHERE")

    def test_undefined_marker_rejected_at_publish(self):
        self.terms.set_binding("D", "ROOT", "port", "P")
        self.assign_all()
        self.terms.add_marker("D", "M1", "S1", 1, "port")
        self.terms.add_marker("D", "M2", "S2", 1, "flange")  # no binding anywhere
        with self.assertRaises(UndefinedMarker):
            self.terms.publish("D")

    def test_marker_revision_bounds(self):
        self.assign_all()
        with self.assertRaises(SegmentNotFound):
            self.terms.add_marker("D", "M1", "S1", 0, "port")
        with self.assertRaises(SegmentNotFound):
            self.terms.add_marker("D", "M2", "S1", 2, "port")  # S1 is at revision 1
        with self.assertRaises(SegmentNotFound):
            self.terms.add_marker("D", "M3", "S9", 1, "port")

    def test_marker_identity_conflict(self):
        self.assign_all()
        self.terms.add_marker("D", "M1", "S1", 1, "port")
        self.terms.add_marker("D", "M1", "S1", 1, "port")  # idempotent
        with self.assertRaises(MarkerExists):
            self.terms.add_marker("D", "M1", "S2", 1, "port")

    def test_concept_and_chapter_conflicts(self):
        with self.assertRaises(ConceptExists):
            self.terms.add_concept("D", "P", label="different")
        self.terms.add_concept("D", "P")  # idempotent
        with self.assertRaises(ChapterExists):
            self.terms.add_chapter("D", "ELEC", parent_id="SHIP")
        with self.assertRaises(ChapterNotFound):
            self.terms.add_chapter("D", "Y", parent_id="NOWHERE")


class VersioningTests(TerminologyCase):
    def build_v1(self):
        self.terms.set_binding("D", "ROOT", "port", "Q")
        self.terms.set_binding("D", "ELEC", "port", "P")
        self.terms.assign_segment("D", "S1", "ELEC")
        self.terms.assign_segment("D", "S2", "ELEC")
        self.terms.assign_segment("D", "S3", "ROOT")
        self.terms.add_marker("D", "M1", "S1", 1, "port")
        self.terms.add_marker("D", "M2", "S2", 1, "port")
        return self.terms.publish("D")

    def test_move_segment_changes_concept_old_version_readable(self):
        v1 = self.build_v1()
        self.assertEqual(v1.version, 1)
        self.assertEqual(v1.marker_concepts(), {"M1": "P", "M2": "P"})
        # Move S2 to SHIP: same spelling now resolves to Q.
        self.terms.assign_segment("D", "S2", "SHIP")
        v2 = self.terms.publish("D")
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.marker_concepts(), {"M1": "P", "M2": "Q"})
        # Marker identity is stable across versions; only resolution changes.
        self.assertEqual([m.marker_id for m in v1.markers], [m.marker_id for m in v2.markers])
        # Old version is still readable and unchanged.
        reread = self.terms.terminology_version("D", 1)
        self.assertEqual(reread.marker_concepts(), {"M1": "P", "M2": "P"})
        self.assertEqual(self.terms.list_versions("D")[0]["version"], 1)
        self.assertEqual(self.terms.latest_version("D"), 2)
        with self.assertRaises(TerminologyVersionNotFound):
            self.terms.terminology_version("D", 99)

    def test_terminology_versions_independent_of_source_bytes(self):
        v1 = self.build_v1()
        # Source revision bumps do not disturb published terminology versions.
        self.catalog.revise_segment("D", "S1", "a revised")
        reread = self.terms.terminology_version("D", 1)
        self.assertEqual(reread.marker_concepts(), v1.marker_concepts())
        # And a new terminology version can still be published (markers pin
        # the revision they were indexed against; S1@1 stays historical).
        self.terms.assign_segment("D", "S2", "SHIP")
        v2 = self.terms.publish("D")
        self.assertEqual(v2.version, 2)

    def test_readonly_reader_contract_and_reopen_reproducibility(self):
        self.build_v1()
        self.terms.assign_segment("D", "S2", "SHIP")
        self.terms.publish("D")
        reader = PublishedTerminologyReader(self.db)
        v1 = reader.terminology_version("D", 1)
        v2 = reader.terminology_version("D", 2)
        self.assertEqual(v1.marker_concepts(), {"M1": "P", "M2": "P"})
        self.assertEqual(v2.marker_concepts(), {"M1": "P", "M2": "Q"})
        self.assertEqual(reader.latest_version("D"), 2)
        # The reader exposes no mutation API; the connection is mode=ro.
        self.assertFalse(hasattr(reader, "publish"))
        # Re-open the database entirely: resolution reproduces exactly.
        reader2 = PublishedTerminologyReader(self.db)
        again = reader2.terminology_version("D", 2)
        self.assertEqual(again.to_dict(), v2.to_dict())

    def test_snapshot_is_self_contained_for_review(self):
        self.build_v1()
        version = self.terms.terminology_version("D", 1)
        data = version.to_dict()
        # Chapter tree, segment ownership, marker segment revision and
        # spelling are all reviewable inside this one version.
        self.assertIn(("ELEC", "ROOT"), [(c["chapter_id"], c["parent_id"]) for c in data["chapters"]])
        self.assertIn(
            {"segment_id": "S1", "chapter_id": "ELEC"},
            [dict(s) for s in data["segment_chapters"]],
        )
        marker = next(m for m in data["markers"] if m["marker_id"] == "M1")
        self.assertEqual(marker["segment_revision"], 1)
        self.assertEqual(marker["spelling"], "port")


if __name__ == "__main__":
    unittest.main()
