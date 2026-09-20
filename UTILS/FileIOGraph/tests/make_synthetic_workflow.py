#!/usr/bin/env python3
"""Generate a small workflow whose file-IO graph is known by construction.

Exercises the shapes that make attribution hard: a global task consuming
every timeframe, a file with two readers, a subdirectory created at run
time, four different libc entry points, and enough concurrency that
several tasks are alive at once.

--emit-truth writes the expected graph in the schema the analysers produce.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from filegraph_report import build_report  # noqa: E402

SLEEP = "0.4"


RESOURCES = {"cpu": 1, "mem": 100, "relative_cpu": 1.0}


def build(ntf: int, sleep: str = SLEEP):
    stages: List[Dict] = []
    written: Dict[str, Set[str]] = {}
    read: Dict[str, Set[str]] = {}

    def w(f, t):
        written.setdefault(f, set()).add(t)

    def r(f, t):
        read.setdefault(f, set()).add(t)

    # a global producer everything depends on, like the background event
    stages.append({
        "name": "bkg", "cmd": f"sleep {sleep}; echo bkgdata > bkg.dat",
        "needs": [], "cwd": "./", "timeframe": -1, "labels": ["SIM"],
        "resources": RESOURCES,
    })
    w("./bkg.dat", "bkg")

    for i in range(1, ntf + 1):
        tf = f"tf{i}"

        # shell redirection and Python open() in one task
        stages.append({
            "name": f"sgnsim_{i}",
            "cmd": (f"sleep {sleep}; cat ../bkg.dat > sgn.dat; "
                    f"python3 -c \"open('kine.dat','w').write('kine{i}')\""),
            "needs": ["bkg"], "cwd": f"./{tf}", "timeframe": i,
            "labels": ["SIM"], "resources": RESOURCES,
        })
        r("./bkg.dat", f"sgnsim_{i}")
        w(f"./{tf}/sgn.dat", f"sgnsim_{i}")
        w(f"./{tf}/kine.dat", f"sgnsim_{i}")

        # cp, plus a subdirectory created while the workflow is running
        stages.append({
            "name": f"digi_{i}",
            "cmd": (f"sleep {sleep}; cp sgn.dat digi.dat; "
                    f"mkdir -p sub; cp digi.dat sub/extra.dat"),
            "needs": [f"sgnsim_{i}"], "cwd": f"./{tf}", "timeframe": i,
            "labels": ["DIGI"], "resources": RESOURCES,
        })
        r(f"./{tf}/sgn.dat", f"digi_{i}")
        w(f"./{tf}/digi.dat", f"digi_{i}")
        r(f"./{tf}/digi.dat", f"digi_{i}")
        w(f"./{tf}/sub/extra.dat", f"digi_{i}")

        # awk opens its output file itself, through fopen
        stages.append({
            "name": f"reco_{i}",
            "cmd": (f"sleep {sleep}; awk '{{print > \"reco.dat\"}}' digi.dat; "
                    f"cat kine.dat >> reco.dat"),
            "needs": [f"digi_{i}"], "cwd": f"./{tf}", "timeframe": i,
            "labels": ["RECO"], "resources": RESOURCES,
        })
        r(f"./{tf}/digi.dat", f"reco_{i}")
        r(f"./{tf}/kine.dat", f"reco_{i}")
        w(f"./{tf}/reco.dat", f"reco_{i}")

        stages.append({
            "name": f"aod_{i}",
            "cmd": (f"sleep {sleep}; cat reco.dat sub/extra.dat > AO2D.dat"),
            "needs": [f"reco_{i}"], "cwd": f"./{tf}", "timeframe": i,
            "labels": ["AOD"], "resources": RESOURCES,
        })
        r(f"./{tf}/reco.dat", f"aod_{i}")
        r(f"./{tf}/sub/extra.dat", f"aod_{i}")
        w(f"./{tf}/AO2D.dat", f"aod_{i}")

    # the global consumer of every timeframe's output
    inputs = " ".join(f"tf{i}/AO2D.dat" for i in range(1, ntf + 1))
    stages.append({
        "name": "aodmerge",
        "cmd": f"sleep {sleep}; cat {inputs} > AO2D_merged.dat",
        "needs": [f"aod_{i}" for i in range(1, ntf + 1)],
        "cwd": "./", "timeframe": -1, "labels": ["AOD"],
        "resources": RESOURCES,
    })
    for i in range(1, ntf + 1):
        r(f"./tf{i}/AO2D.dat", "aodmerge")
    w("./AO2D_merged.dat", "aodmerge")

    return {"stages": stages}, written, read


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ntf", type=int, default=4)
    ap.add_argument("--sleep", default=SLEEP)
    ap.add_argument("-o", "--output", default="workflow.json")
    ap.add_argument("--emit-truth", default=None,
                    help="also write the expected graph here")
    a = ap.parse_args(argv)

    wf, written, read = build(a.ntf, a.sleep)
    with open(a.output, "w") as f:
        json.dump(wf, f, indent=2)
    print(f"wrote {a.output}: {len(wf['stages'])} stages, {a.ntf} timeframe(s)")

    if a.emit_truth:
        doc = build_report(written, read,
                           [s["name"] for s in wf["stages"]])
        with open(a.emit_truth, "w") as f:
            json.dump(doc, f, indent=2)
        print(f"wrote {a.emit_truth}: expected graph "
              f"({len(doc['file_report'])} files)")


if __name__ == "__main__":
    main()
