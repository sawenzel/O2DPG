#!/usr/bin/env python3
"""Run the synthetic workflow under several backends at once and grade them.

Needs no ALICE software and no Monte Carlo, so it is what tells you a
backend is wrong before an hour of real MC does.  Each backend is graded
against the analytic truth the generator wrote down, and against a
reference backend when one is available.  Exit status is 0 only when
every candidate is at least SAFE against both.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

from filegraph_test_support import FILEGRAPH_DIR, REPO, filegraph  # noqa: F401

from compare_reports import compare  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))


def run(argv, **kw):
    print("+ " + " ".join(argv), flush=True)
    return subprocess.run(argv, **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", default=None,
                    help="scratch directory (default: a fresh one under $TMPDIR)")
    ap.add_argument("--backends", default=None,
                    help="comma-separated backends to activate "
                         f"(default: all but the reference; known: "
                         f"{', '.join(sorted(filegraph.BACKENDS))})")
    ap.add_argument("--reference", default="fanotify",
                    help="backend to compare the others against ('' to skip)")
    ap.add_argument("--ntf", type=int, default=4)
    ap.add_argument("--cpu-limit", type=int, default=4)
    ap.add_argument("--sleep", default="0.4")
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args(argv)

    runner = os.path.join(REPO, "MC", "new_runner2", "MC", "bin",
                          "o2dpg_workflow_runner.py")
    if not os.path.exists(runner):
        print(f"no runner at {runner}", file=sys.stderr)
        return 2

    ref = a.reference.strip()
    if a.backends is None:
        candidates = [b for b in sorted(filegraph.BACKENDS) if b != ref]
    else:
        candidates = [b.strip() for b in a.backends.split(",") if b.strip()]
    active = list(dict.fromkeys(([ref] if ref else []) + candidates))
    if not active:
        print("nothing to run", file=sys.stderr)
        return 2

    workdir = a.workdir or os.path.join(
        os.getenv("TMPDIR", "/tmp"), f"filegraph_equiv_{os.getpid()}")
    os.makedirs(workdir, exist_ok=True)
    print(f"workdir: {workdir}")

    run([a.python, os.path.join(_HERE, "make_synthetic_workflow.py"),
         "--ntf", str(a.ntf), "--sleep", a.sleep,
         "-o", os.path.join(workdir, "workflow.json"),
         "--emit-truth", os.path.join(workdir, "truth.json")], check=True)

    env = dict(os.environ, O2DPG_ROOT=REPO)
    t0 = time.time()
    r = run([a.python, runner, "-f", "workflow.json",
             "--cpu-limit", str(a.cpu_limit), "--mem-limit", "8000",
             "--filegraph-backends", ",".join(active)], cwd=workdir, env=env)
    if r.returncode != 0:
        print(f"runner failed with rc={r.returncode}", file=sys.stderr)
        return 2
    print(f"workflow finished in {time.time() - t0:.1f}s")

    def load(path):
        with open(os.path.join(workdir, path)) as f:
            return json.load(f)

    truth = load("truth.json")
    reports = {}
    for b in active:
        found = glob.glob(os.path.join(workdir, f"filegraph_{b}_*.json"))
        if found:
            reports[b] = load(found[0])

    failures = []
    for backend in active:
        role = "reference" if backend == ref else "candidate"
        if backend not in reports:
            print(f"\nNO REPORT from {backend}", file=sys.stderr)
            if role == "candidate":
                failures.append((backend, "no report"))
            continue

        print(f"\n{'=' * 72}\n== {backend} ({role}) vs analytic truth\n{'=' * 72}")
        text, summary = compare(truth, reports[backend], limit=10)
        print(text)
        print(f"OVERALL: {summary['verdict']}")
        if summary["verdict"] == "UNSAFE":
            if role == "reference":
                # fanotify resolves the process chain from /proc after the
                # event, so on a fast machine it loses accesses made by
                # processes that have already exited
                print(f"NOTE: the reference backend {backend} itself misses "
                      f"edges the truth has")
            else:
                failures.append((backend, "misses edges the truth has"))

        if role == "candidate" and ref in reports:
            print(f"\n{'=' * 72}\n== {backend} vs {ref} (reference)\n{'=' * 72}")
            text, summary = compare(reports[ref], reports[backend], limit=10)
            print(text)
            print(f"OVERALL: {summary['verdict']}")
            with open(os.path.join(workdir, f"diff_{backend}_vs_{ref}.json"), "w") as f:
                json.dump(summary, f, indent=2)
            if summary["verdict"] == "UNSAFE":
                failures.append((backend, f"misses edges {ref} has"))

    print(f"\n{'=' * 72}")
    if failures:
        for b, why in failures:
            print(f"FAIL  {b}: {why}")
        print(f"workdir kept at {workdir}")
        return 1
    print("PASS  every backend is at least SAFE against truth"
          + (f" and against {ref}" if ref else ""))
    print(f"workdir kept at {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
