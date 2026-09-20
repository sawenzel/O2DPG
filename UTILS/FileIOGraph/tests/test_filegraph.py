#!/usr/bin/env python3
"""Offline tests for the file-graph report machinery.

Plain unittest on purpose: this has to run on the bare interpreter of a
GRID worker or a CVMFS O2 environment, neither of which has pytest.

    python3 -m unittest discover -s UTILS/FileIOGraph/tests
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from filegraph_test_support import FILEGRAPH_DIR  # noqa: F401

from filegraph_report import (  # noqa: E402
    basedir_prefix, build_report, keep_file, relative_to_basedir,
)
from compare_reports import compare  # noqa: E402
import make_synthetic_workflow  # noqa: E402


ALL = [re.compile(r".*")]


class TestPathHelpers(unittest.TestCase):
    def test_relative_to_basedir(self):
        for base in ("/w", "/w/"):
            prefix = basedir_prefix(base)
            self.assertEqual(relative_to_basedir("/w/tf1/a.root", prefix),
                             "./tf1/a.root")
            self.assertIsNone(relative_to_basedir("/usr/lib/libc.so", prefix))

    def test_logs_and_dpl_config_are_noise(self):
        self.assertFalse(keep_file("./tf1/sgnsim_1.log", ALL))
        self.assertFalse(keep_file("./tf1/sgnsim_1.log_done", ALL))
        self.assertFalse(keep_file("./dpl-config.json", ALL))
        self.assertTrue(keep_file("./tf1/AO2D.root", ALL))

    def test_filters_apply(self):
        self.assertTrue(keep_file("./tf1/a.root", [re.compile(r".*\.root")]))
        self.assertFalse(keep_file("./tf1/a.dat", [re.compile(r".*\.root")]))


class TestBuildReport(unittest.TestCase):
    def _doc(self):
        written = {"./tf1/a.root": {"digi_1"}, "./tf2/a.root": {"digi_2"},
                   "./AO2D.root": {"aodmerge"}}
        read = {"./tf1/a.root": {"reco_1", "aodmerge"},
                "./tf2/a.root": {"reco_2", "aodmerge"}}
        return build_report(written, read, ["digi_1", "digi_2", "reco_1",
                                            "reco_2", "aodmerge"])

    def test_timeframe_tasks_become_templates(self):
        doc = self._doc()
        tpl = {e["file"]: e for e in doc["file_template_report"]}
        self.assertIn("./tfX/a.root", tpl)
        self.assertEqual(tpl["./tfX/a.root"]["written_by"], ["digi_X"])
        self.assertEqual(tpl["./tfX/a.root"]["source_timeframes"], [1, 2])

    def test_global_reader_is_not_templated(self):
        # aodmerge has no _N suffix, so it must survive verbatim -- this is
        # the case that a naive "strip the trailing _N" would corrupt
        tpl = {e["file"]: e for e in self._doc()["file_template_report"]}
        self.assertIn("aodmerge", tpl["./tfX/a.root"]["read_by"])
        self.assertIn("reco_X", tpl["./tfX/a.root"]["read_by"])

    def test_non_timeframe_file_absent_from_templates(self):
        tpl = {e["file"] for e in self._doc()["file_template_report"]}
        self.assertNotIn("./AO2D.root", tpl)

    def test_task_report_covers_every_task(self):
        doc = self._doc()
        self.assertEqual([t["task"] for t in doc["task_report"]],
                         ["aodmerge", "digi_1", "digi_2", "reco_1", "reco_2"])


class TestCompare(unittest.TestCase):
    def _pair(self, cand_written, cand_read):
        ref = build_report({"./tf1/a.root": {"digi_1"}},
                           {"./tf1/a.root": {"reco_1"}}, ["digi_1", "reco_1"])
        cand = build_report(cand_written, cand_read, ["digi_1", "reco_1"])
        _, summary = compare(ref, cand)
        return summary["verdict"], summary

    def test_identical_is_exact(self):
        v, _ = self._pair({"./tf1/a.root": {"digi_1"}},
                             {"./tf1/a.root": {"reco_1"}})
        self.assertEqual(v, "EXACT")

    def test_extra_reader_is_safe(self):
        v, s = self._pair({"./tf1/a.root": {"digi_1"}},
                             {"./tf1/a.root": {"reco_1", "qc_1"}})
        self.assertEqual(v, "SAFE")
        self.assertEqual(s["sections"]["file_report"]["missing_edges"], 0)

    def test_missing_reader_is_unsafe(self):
        v, s = self._pair({"./tf1/a.root": {"digi_1"}}, {})
        self.assertEqual(v, "UNSAFE")
        self.assertEqual(s["sections"]["file_report"]["missing_edges"], 1)

    def test_missing_file_is_unsafe(self):
        v, _ = self._pair({}, {})
        self.assertEqual(v, "UNSAFE")

    def test_recall_is_reported(self):
        _, s = self._pair({"./tf1/a.root": {"digi_1"}}, {})
        # 2 reference edges (one write, one read), one of them missing
        self.assertAlmostEqual(s["sections"]["file_report"]["recall"], 0.5)


class TestAnalyseFanotifyLog(unittest.TestCase):
    """End-to-end on the v2 analyser, so the shared module stays honest."""

    ACTION = (
        "2026-05-06 16:49:18,267 INFO Task pid=101 tid=0 bkg finished rc=0\n"
        "2026-05-06 16:49:19,267 INFO Task pid=102 tid=1 sgnsim_1 finished rc=0\n"
        "2026-05-06 16:49:20,267 INFO Task pid=103 tid=2 reco_1 finished rc=0\n"
    )

    def _monitor(self, base):
        return (
            f'"{base}/bkg.dat",write,101\n'
            f'"{base}/tf1/sgn.dat",write,201;102\n'
            f'"{base}/bkg.dat",read,201;102\n'
            f'"{base}/tf1/sgn.dat",read,103\n'
            f'"{base}/tf1/reco.dat",write,103\n'
            f'"{base}/tf1/reco_1.log",write,103\n'
            f'"/usr/lib/libc.so.6",read,103\n'
        )

    def test_report(self):
        with tempfile.TemporaryDirectory() as d:
            action = os.path.join(d, "action.log")
            monitor = os.path.join(d, "monitor.log")
            out = os.path.join(d, "report.json")
            with open(action, "w") as f:
                f.write(self.ACTION)
            with open(monitor, "w") as f:
                f.write(self._monitor(d))
            subprocess.run(
                [sys.executable, os.path.join(FILEGRAPH_DIR, "analyse_FileIO_v2.py"),
                 "--actionFile", action, "--monitorFile", monitor,
                 "--basedir", d, "-o", out],
                check=True, stdout=subprocess.DEVNULL)
            with open(out) as f:
                doc = json.load(f)

        got = {e["file"]: (e["written_by"], e["read_by"])
               for e in doc["file_report"]}
        self.assertEqual(got["./bkg.dat"], (["bkg"], ["sgnsim_1"]))
        self.assertEqual(got["./tf1/sgn.dat"], (["sgnsim_1"], ["reco_1"]))
        self.assertEqual(got["./tf1/reco.dat"], (["reco_1"], []))
        # a grandchild's access is attributed through the parent chain
        self.assertIn("sgnsim_1", got["./tf1/sgn.dat"][0])
        # noise is dropped
        self.assertNotIn("./tf1/reco_1.log", got)
        self.assertNotIn("/usr/lib/libc.so.6", got)


class TestSyntheticWorkflow(unittest.TestCase):
    def test_truth_matches_the_commands(self):
        wf, written, read = make_synthetic_workflow.build(2)
        # the global merge task reads every timeframe's AOD
        self.assertEqual(read["./tf1/AO2D.dat"], {"aodmerge"})
        self.assertEqual(read["./tf2/AO2D.dat"], {"aodmerge"})
        # a run-time subdirectory is part of the expected graph
        self.assertIn("./tf1/sub/extra.dat", written)

    def test_every_stage_has_a_cwd_and_resources(self):
        wf, _, _ = make_synthetic_workflow.build(3)
        for s in wf["stages"]:
            self.assertIn("cwd", s)
            self.assertIn("resources", s)
            self.assertIn("timeframe", s)


if __name__ == "__main__":
    unittest.main()
