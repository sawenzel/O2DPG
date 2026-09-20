#!/usr/bin/env python3
"""Tests for the runner's backend selection and task wrapping.

No monitor is started and no workflow runs; this is the contract between
the runner and the backends.
"""
from __future__ import annotations

import logging
import os
import tempfile
import unittest
from unittest import mock

from filegraph_test_support import filegraph

LOG = logging.getLogger("test")


class StubBackend(filegraph.FileGraphBackend):
    name = "stub"

    def wrap(self, argv, taskname, tid):
        return ["stub", taskname, str(tid), "--"] + argv


def manager(spec, action_log="action.log"):
    return filegraph.FileGraphManager.from_config(spec, "/w", 4242, action_log, LOG)


@mock.patch.dict(os.environ, {}, clear=False)
class TestSelection(unittest.TestCase):
    def setUp(self):
        for k in ("O2DPG_PRODUCE_FILEGRAPH", "O2DPG_FILEGRAPH_BACKENDS"):
            os.environ.pop(k, None)
        filegraph.BACKENDS["stub"] = StubBackend
        self.addCleanup(filegraph.BACKENDS.pop, "stub", None)

    def test_nothing_requested_means_no_backend(self):
        self.assertEqual(manager("").backends, [])

    def test_named_backends_are_built_in_order(self):
        self.assertEqual([b.name for b in manager("stub,fanotify,strace").backends],
                         ["stub", "fanotify", "strace"])

    def test_legacy_variable_selects_fanotify_with_that_exe(self):
        os.environ["O2DPG_PRODUCE_FILEGRAPH"] = "/opt/mon.exe"
        m = manager("")
        self.assertEqual([b.name for b in m.backends], ["fanotify"])
        self.assertEqual(m.backends[0].exe, "/opt/mon.exe")

    def test_legacy_exe_applies_when_fanotify_is_named_explicitly(self):
        os.environ["O2DPG_PRODUCE_FILEGRAPH"] = "/opt/mon.exe"
        self.assertEqual(manager("fanotify").backends[0].exe, "/opt/mon.exe")

    def test_unknown_backend_is_skipped_not_fatal(self):
        self.assertEqual([b.name for b in manager("stub,nonsense").backends],
                         ["stub"])

    def test_the_action_log_reaches_the_backend_that_needs_it(self):
        b = manager("fanotify", action_log="pipeline_action_7.log").backends[0]
        self.assertIn("pipeline_action_7.log", b.analyser_args())


class TestWrapping(unittest.TestCase):
    ARGV = ["/bin/bash", "-c", "echo hi"]

    def setUp(self):
        filegraph.BACKENDS["stub"] = StubBackend
        self.addCleanup(filegraph.BACKENDS.pop, "stub", None)

    def test_a_backend_that_does_not_wrap_leaves_the_command_alone(self):
        self.assertEqual(manager("fanotify").wrap(list(self.ARGV), "sgnsim_1", 3),
                         self.ARGV)

    def test_a_wrapping_backend_keeps_the_command_as_the_tail(self):
        argv = manager("stub").wrap(list(self.ARGV), "sgnsim_1", 3)
        self.assertEqual(argv[:4], ["stub", "sgnsim_1", "3", "--"])
        self.assertEqual(argv[4:], self.ARGV)

    def test_an_empty_manager_is_a_no_op(self):
        m = manager("")
        self.assertEqual(m.wrap(list(self.ARGV), "sgnsim_1", 3), self.ARGV)
        m.start()
        m.stop()
        self.assertEqual(m.analyse(), {})


class TestStraceWrapping(unittest.TestCase):
    ARGV = ["/bin/bash", "-c", "echo hi"]

    def _backend(self, exe="strace"):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        b = filegraph.StraceBackend(d.name, 4242, "action.log", LOG, exe=exe)
        b.start()
        return b

    def test_the_task_command_stays_the_tail(self):
        b = self._backend()
        if not b._argv:
            self.skipTest("strace not installed")
        argv = b.wrap(list(self.ARGV), "sgnsim_1", 3)
        self.assertEqual(argv[argv.index("--") + 1:], self.ARGV)
        self.assertIn("-f", argv)
        self.assertIn("-y", argv)
        # the trace file name is the whole attribution mechanism
        self.assertIn("trace_3_sgnsim_1.log", " ".join(argv))

    def test_a_missing_strace_leaves_the_command_alone(self):
        b = self._backend(exe="/nonexistent/strace")
        self.assertEqual(b.wrap(list(self.ARGV), "sgnsim_1", 3), self.ARGV)

    def test_traced_syscalls_are_the_ones_the_analyser_parses(self):
        import analyse_FileIO_strace as A
        traced = set(filegraph.StraceBackend.SYSCALLS.split(","))
        self.assertTrue(set(A.OPEN_CALLS) <= traced)
        self.assertTrue({"rename", "renameat", "renameat2"} <= traced)
        # nothing is traced that nothing reads
        self.assertEqual(traced - set(A.OPEN_CALLS)
                         - {"rename", "renameat", "renameat2"}, set())


class TestReportNames(unittest.TestCase):
    def test_fanotify_also_writes_the_legacy_name(self):
        self.assertTrue(filegraph.FanotifyBackend.legacy_report)
        self.assertFalse(filegraph.FileGraphBackend.legacy_report)


class TestFilegraphDir(unittest.TestCase):
    def test_resolves_without_o2dpg_root(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("O2DPG_ROOT", None)
            d = filegraph.filegraph_dir()
        self.assertTrue(os.path.isdir(d), d)
        self.assertTrue(os.path.exists(os.path.join(d, "analyse_FileIO_v2.py")))

    def test_o2dpg_root_overrides(self):
        with mock.patch.dict(os.environ, {"O2DPG_ROOT": "/opt/o2dpg"}):
            self.assertEqual(filegraph.filegraph_dir(),
                             "/opt/o2dpg/UTILS/FileIOGraph")


if __name__ == "__main__":
    unittest.main()
