#!/usr/bin/env python3
"""Analyse a fanotify file-access log correlated with an O2DPG pipeline
action log, producing a JSON file-task dependency report.

Replaces analyse_FileIO.py with support for both action-log formats:

  Old runner:  "... INFO Task <PID> <tid>:<name> finished with status 0"
  New runner:  "... INFO Task pid=<PID> tid=<TID> <name> finished rc=0"

Output JSON schema (same as v1, consumed by o2dpg_runner/cleanup.py):

  {
    "file_report": [{"file": str, "written_by": [str, ...], "read_by": [str, ...]}, ...],
    "file_template_report": [
      {"file": "./tfX/...", "written_by": ["task_X", ...],
       "read_by": ["task_X", ...], "source_timeframes": [int, ...]},
      ...
    ],
    "task_report": [{"task": str, "writes": [str, ...], "reads":  [str, ...]}, ...]
  }
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Dict, List, Set, Tuple


# ── action-log patterns ───────────────────────────────────────────────────────

# Old runner: "... INFO Task <PID> <tid>:<name> finished with status 0"
_PAT_OLD = re.compile(r'.*INFO Task (\d+)[^:]*:(\w+) finished with status 0')
# New runner: "... INFO Task pid=<PID> tid=<TID> <name> finished rc=0"
_PAT_NEW = re.compile(r'.*INFO Task pid=(\d+) tid=\d+ (\S+) finished rc=0')


def parse_action_log(path: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Parse task-PID associations from an action log.

    Returns (pid_to_task, task_to_pid).  Only successfully completed tasks
    are included (rc=0 / status 0) so failed retries don't pollute the map.
    """
    pid_to_task: Dict[str, str] = {}
    task_to_pid: Dict[str, str] = {}
    with open(path) as fh:
        for line in fh:
            m = _PAT_OLD.match(line) or _PAT_NEW.match(line)
            if m:
                pid, name = m.group(1), m.group(2)
                pid_to_task[pid] = name
                task_to_pid[name] = pid
    return pid_to_task, task_to_pid


# ── monitor-log parsing ───────────────────────────────────────────────────────

_RECORD_RE = re.compile(r'"?([^"]+)"?,(read|write),(.*)')
_EXCLUDE_RE = re.compile(r'(.*\.log.*|ccdb/log|.*dpl-config\.json)')
_TF_PATH_RE = re.compile(r'^\./tf(?P<tf>\d+)/')


def _task_template_for_timeframe(task_name: str, source_tf: int) -> str:
    suffix = f"_{source_tf}"
    if task_name.endswith(suffix):
        return f"{task_name[:-len(suffix)]}_X"
    return task_name


def parse_monitor_log(
    path: str,
    pid_to_task: Dict[str, str],
    basedir: str,
    file_filters: List[re.Pattern],
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """Parse the fanotify raw log and map files to the tasks that touched them.

    Returns (file_written_by, file_read_by) where each value is a set of
    task names.  Only files inside *basedir* that pass *file_filters* and
    are not excluded by the built-in exclude pattern are included.

    A file access is attributed to a task when any PID in the process-chain
    column of the monitor log appears in *pid_to_task*.  This works for both
    direct children and children-of-children of the task process because the
    fanotify monitor records the full ancestor chain up to the root PID.
    """
    file_written: Dict[str, Set[str]] = {}
    file_read: Dict[str, Set[str]] = {}
    basedir_prefix = basedir.rstrip("/") + "/"

    with open(path) as fh:
        for line in fh:
            m = _RECORD_RE.match(line)
            if not m:
                continue
            fname, mode, chain = m.group(1), m.group(2), m.group(3)

            if not fname.startswith(basedir_prefix):
                continue
            rel = "./" + fname[len(basedir_prefix):]

            if _EXCLUDE_RE.match(rel):
                continue
            if not any(r.match(rel) for r in file_filters):
                continue

            for pid in chain.split(";"):
                task = pid_to_task.get(pid)
                if task is None:
                    continue
                if mode == "write":
                    file_written.setdefault(rel, set()).add(task)
                else:
                    file_read.setdefault(rel, set()).add(task)

    return file_written, file_read


# ── output ────────────────────────────────────────────────────────────────────

def write_json_report(
    path: str,
    file_written: Dict[str, Set[str]],
    file_read: Dict[str, Set[str]],
    task_to_pid: Dict[str, str],
) -> None:
    all_files = sorted(set(file_written) | set(file_read))
    file_report = [
        {
            "file": f,
            "written_by": sorted(file_written.get(f, set())),
            "read_by": sorted(file_read.get(f, set())),
        }
        for f in all_files
    ]

    template_report: Dict[str, Dict] = {}
    for entry in file_report:
        match = _TF_PATH_RE.match(entry["file"])
        if match is None:
            continue
        source_tf = int(match.group("tf"))
        template_file = _TF_PATH_RE.sub("./tfX/", entry["file"], count=1)
        merged = template_report.setdefault(
            template_file,
            {
                "file": template_file,
                "written_by": set(),
                "read_by": set(),
                "source_timeframes": set(),
            },
        )
        merged["source_timeframes"].add(source_tf)
        for task in entry["written_by"]:
            merged["written_by"].add(_task_template_for_timeframe(task, source_tf))
        for task in entry["read_by"]:
            merged["read_by"].add(_task_template_for_timeframe(task, source_tf))

    file_template_report = [
        {
            "file": f,
            "written_by": sorted(v["written_by"]),
            "read_by": sorted(v["read_by"]),
            "source_timeframes": sorted(v["source_timeframes"]),
        }
        for f, v in sorted(template_report.items())
    ]

    task_reads: Dict[str, Set[str]] = {}
    task_writes: Dict[str, Set[str]] = {}
    for f, tasks in file_read.items():
        for t in tasks:
            task_reads.setdefault(t, set()).add(f)
    for f, tasks in file_written.items():
        for t in tasks:
            task_writes.setdefault(t, set()).add(f)
    task_report = [
        {
            "task": t,
            "writes": sorted(task_writes.get(t, set())),
            "reads": sorted(task_reads.get(t, set())),
        }
        for t in sorted(task_to_pid)
    ]

    with open(path, "w") as fh:
        json.dump(
            {
                "file_report": file_report,
                "file_template_report": file_template_report,
                "task_report": task_report,
            },
            fh,
            indent=2,
        )
    print(
        f"Wrote {path}: {len(file_report)} file(s) referenced, "
        f"{len(file_template_report)} timeframe file template(s), "
        f"{len(task_report)} task(s) mapped"
    )


def draw_graph(
    filename: str,
    file_written: Dict[str, Set[str]],
    file_read: Dict[str, Set[str]],
    task_to_pid: Dict[str, str],
) -> None:
    try:
        from graphviz import Digraph
    except ImportError:
        print("graphviz not installed, skipping graph", file=sys.stderr)
        return

    ccdb_re = re.compile(r"ccdb(.*)/snapshot\.root")
    dot = Digraph(comment="O2DPG file-task network")
    idx: Dict[str, int] = {}
    counter = 0

    all_files = set(file_written) | set(file_read)
    ccdb = [(f, ccdb_re.match(f).group(1)) for f in all_files if ccdb_re.match(f)]
    normal = [f for f in all_files if not ccdb_re.match(f)]

    with dot.subgraph(name="CCDB") as sg:
        sg.attr(color="blue")
        for f, label in ccdb:
            idx[f] = counter
            sg.node(str(counter), label, color="blue")
            counter += 1

    with dot.subgraph(name="normal") as sg:
        sg.attr(color="black")
        for f in normal:
            idx[f] = counter
            sg.node(str(counter), f, color="red")
            counter += 1
        for t in task_to_pid:
            idx[t] = counter
            sg.node(str(counter), t, shape="box", color="green", style="filled")
            counter += 1

    for f, tasks in file_read.items():
        for t in tasks:
            dot.edge(str(idx[f]), str(idx[t]))
    for f, tasks in file_written.items():
        for t in tasks:
            dot.edge(str(idx[t]), str(idx[f]))

    dot.render(filename, format="pdf")
    dot.render(filename, format="gv")


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--actionFile", required=True,
                   help="O2DPG pipeline runner action log")
    p.add_argument("--monitorFile", required=True,
                   help="fanotify raw log from monitor_fileaccess_v2.exe")
    p.add_argument("--basedir", default="/",
                   help="Workflow working directory (default: /)")
    p.add_argument("--file-filters", nargs="+", default=[r".*"],
                   help="Regex filters to select file paths (default: all)")
    p.add_argument("--graphviz", default=None,
                   help="Produce graphviz plots with this base filename")
    p.add_argument("-o", "--output", required=True,
                   help="Output JSON report path")
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)

    file_filters = [re.compile(f) for f in args.file_filters]

    pid_to_task, task_to_pid = parse_action_log(args.actionFile)
    if not pid_to_task:
        print(
            f"WARNING: no task completions found in {args.actionFile}.\n"
            "Check that the action log is from a completed run and that\n"
            "its format matches either the old or new O2DPG runner.",
            file=sys.stderr,
        )
    else:
        print(f"Action log: {len(pid_to_task)} completed task(s) found")

    file_written, file_read = parse_monitor_log(
        args.monitorFile, pid_to_task, args.basedir, file_filters,
    )

    if args.graphviz:
        draw_graph(args.graphviz, file_written, file_read, task_to_pid)

    write_json_report(args.output, file_written, file_read, task_to_pid)


if __name__ == "__main__":
    main()
