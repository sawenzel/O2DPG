#!/usr/bin/env python3
"""Offline tests: a fake CCDB whose validity intervals are known by construction."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.dirname(HERE))

from ccdb_run_coverage import (                                    # noqa: E402
    ABSENT, CATCH_ALL, COVERED, GAP, NOT_TIMESTAMP, RunBounds,
    ccdb_paths_from_filegraph, effective_domain, parse_run_span_file,
    scan_path,
)

#: the real bounds of run 553185; synthetic values below O2DPG's
#: 1514761200000 orbit-vs-ms threshold would be parsed as orbits
SOR, EOR = 1_718_945_276_649, 1_718_950_522_000
SPAN_LO, SPAN_HI = 1_718_946_000_000, 1_718_947_000_000   # as in the fixtures


def fixture(name):
    return os.path.join(FIXTURES, name)


class FakeCCDB:
    """Serves the interval containing t, like CCDB does -- half-open."""

    def __init__(self, intervals):
        self.intervals = intervals                      # path -> [(vf, vu)]

    def validity(self, path, timestamp):
        for vf, vu in self.intervals.get(path, []):
            if vf <= timestamp < vu:
                return vf, vu
        return None

    def any_version(self, path):
        iv = self.intervals.get(path)
        return iv[0] if iv else None


class TestFilegraphExtraction(unittest.TestCase):
    def test_paths_and_users_from_both_sections(self):
        report = {
            "file_report": [
                {"file": "./ccdb/GLO/Config/GRPMagField/snapshot.root",
                 "written_by": ["sgnsim_1"], "read_by": ["digi_1"]},
                {"file": "./tf1/AO2D.root", "written_by": ["aod_1"], "read_by": []},
            ],
            "file_template_report": [
                {"file": "./ccdb/ITS/Calib/NoiseMap/snapshot.root",
                 "written_by": [], "read_by": ["itsreco_X"]},
            ],
        }
        got = ccdb_paths_from_filegraph(report)
        self.assertEqual(set(got), {"GLO/Config/GRPMagField", "ITS/Calib/NoiseMap"})
        self.assertEqual(got["GLO/Config/GRPMagField"], {"sgnsim_1", "digi_1"})

    def test_non_snapshot_ccdb_files_are_ignored(self):
        report = {"file_report": [
            {"file": "./ccdb/RCT/Info/RunInformation/553185/header.json",
             "written_by": [], "read_by": ["x"]},
            {"file": "./ccdb/log", "written_by": ["y"], "read_by": []},
        ]}
        self.assertEqual(ccdb_paths_from_filegraph(report), {})


class TestScanVerdicts(unittest.TestCase):
    def setUp(self):
        self.domain = [[SOR, EOR]]

    def test_single_version_spanning_the_run(self):
        c = FakeCCDB({"A": [(SOR - 5000, EOR + 5000)]})
        self.assertEqual(scan_path(c, "A", self.domain).verdict, COVERED)

    def test_chained_versions_with_no_gap(self):
        c = FakeCCDB({"A": [(SOR - 1, SOR + 200_000),
                            (SOR + 200_000, SOR + 400_000),
                            (SOR + 400_000, EOR + 1)]})
        r = scan_path(c, "A", self.domain)
        self.assertEqual(r.verdict, COVERED)
        self.assertEqual(len(r.segments), 3)

    def test_run_scoped_object_is_not_a_gap(self):
        """Validity exactly [SOR, EOR) must pass -- the half-open case that
        made FT0/Calib/EventsPerBc look broken on run 553185."""
        c = FakeCCDB({"A": [(SOR, EOR)]})
        self.assertEqual(scan_path(c, "A", self.domain).verdict, COVERED)

    def test_real_gap_is_found_and_bracketed(self):
        c = FakeCCDB({"A": [(SOR - 1, SOR + 100_000),
                            (SOR + 400_000, EOR + 1)]})
        r = scan_path(c, "A", self.domain)
        self.assertEqual(r.verdict, GAP)
        self.assertTrue(r.gaps)
        lo, hi = r.gaps[0]
        self.assertGreaterEqual(lo, SOR + 100_000)
        self.assertLessEqual(hi, SOR + 400_000)

    def test_catch_all_is_distinguished_from_covered(self):
        c = FakeCCDB({"A": [(1, 99999999999999)]})
        self.assertEqual(scan_path(c, "A", self.domain).verdict, CATCH_ALL)

    def test_absent_everywhere(self):
        c = FakeCCDB({"A": []})
        self.assertEqual(scan_path(c, "A", self.domain).verdict, ABSENT)

    def test_non_timestamp_keyed_path_is_not_called_absent(self):
        """TPC/Config/FEE is keyed 12..13; a timestamp query cannot see it."""
        c = FakeCCDB({"TPC/Config/FEE": [(12, 13)]})
        self.assertEqual(scan_path(c, "TPC/Config/FEE", self.domain).verdict,
                         NOT_TIMESTAMP)

    def test_gap_inside_an_excluded_span_is_not_reported(self):
        """The whole point of folding in the run-time-span file."""
        c = FakeCCDB({"A": [(SOR - 1, SOR + 100_000), (SOR + 400_000, EOR + 1)]})
        self.assertEqual(scan_path(c, "A", self.domain).verdict, GAP)
        restricted = [[SOR, SOR + 100_000], [SOR + 400_000, EOR]]
        self.assertEqual(scan_path(c, "A", restricted).verdict, COVERED)


class TestRunSpanFile(unittest.TestCase):
    def test_only_lines_for_this_run_are_taken(self):
        spans = parse_run_span_file(fixture("span_ms.txt"), 553185)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0][:2], (SPAN_LO, SPAN_HI))

    def test_orbit_and_millisecond_spans_are_told_apart(self):
        spans = parse_run_span_file(fixture("span_mixed.txt"), 7)
        self.assertTrue(spans[0][2])          # in orbits
        self.assertFalse(spans[1][2])         # in ms

    def test_missing_file_is_no_exclusion(self):
        self.assertEqual(parse_run_span_file("/no/such/file", 7), [])
        self.assertEqual(parse_run_span_file("", 7), [])

    def test_exclusion_removes_the_interval_from_the_domain(self):
        spans = parse_run_span_file(fixture("span_one.txt"), 7)
        dom, warn = effective_domain(RunBounds(SOR, EOR), spans)
        self.assertEqual(dom, [[SOR, SPAN_LO], [SPAN_HI, EOR]])
        self.assertEqual(warn, [])

    def test_inverted_selection_keeps_only_the_listed_spans(self):
        spans = parse_run_span_file(fixture("span_one.txt"), 7)
        dom, _ = effective_domain(RunBounds(SOR, EOR), spans, invert=True)
        self.assertEqual(dom, [[SPAN_LO, SPAN_HI]])

    def test_orbit_span_without_first_orbit_warns_and_is_not_applied(self):
        """Not applying an exclusion can only make the scan stricter."""
        spans = parse_run_span_file(fixture("span_orbits.txt"), 7)
        dom, warn = effective_domain(RunBounds(SOR, EOR), spans)
        self.assertEqual(dom, [[SOR, EOR]])
        self.assertEqual(len(warn), 1)
        self.assertIn("NOT applied", warn[0])

    def test_orbit_span_is_applied_when_first_orbit_is_known(self):
        spans = parse_run_span_file(fixture("span_orbits.txt"), 7)
        bounds = RunBounds(SOR, EOR, first_orbit=5000, last_orbit=20000)
        dom, warn = effective_domain(bounds, spans)
        self.assertEqual(warn, [])
        self.assertEqual(len(dom), 2)
        self.assertEqual(dom[0][0], SOR)
        self.assertLess(dom[0][1], dom[1][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
