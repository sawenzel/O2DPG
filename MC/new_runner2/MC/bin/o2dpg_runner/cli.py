"""Command-line entry point.

Translates argparse -> RunnerConfig, builds loggers, creates the
WorkflowExecutor, and invokes it. All semantics of the original script
are preserved; new flags are additive and default-compatible.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Optional, Tuple

import psutil

from .config import RunnerConfig
from .workflow import build_workflow, load_json
from .executor import WorkflowExecutor

_FORMATTER = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_IN_SLICE_ENV = "O2DPG_RUNNER_IN_SLICE"


def _setup_logger(name: str, logfile: str, level: int = logging.INFO) -> logging.Logger:
    handler = logging.FileHandler(logfile, mode="w")
    handler.setFormatter(_FORMATTER)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def build_parser() -> argparse.ArgumentParser:
    max_system_mem = psutil.virtual_memory().total
    default_mem = 0.9 * max_system_mem / 1024.0 / 1024.0

    p = argparse.ArgumentParser(
        description="Parallel execution of an O2-DPG data/job DAG under resource constraints.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-f", "--workflowfile", required=True)
    p.add_argument("-jmax", "--maxjobs", type=int, default=100)
    p.add_argument("-k", "--keep-going", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--visualize-workflow", action="store_true")
    p.add_argument("--target-labels", nargs="+", default=[])
    p.add_argument("-tt", "--target-tasks", nargs="+", default=["*"])
    p.add_argument("--produce-script", default=None)
    p.add_argument("--rerun-from", default=None)
    p.add_argument("--list-tasks", action="store_true")

    # Resources
    p.add_argument("--update-resources", dest="update_resources", default=None)
    p.add_argument("--dynamic-resources", dest="dynamic_resources", action="store_true")
    p.add_argument("--optimistic-resources", dest="optimistic_resources", action="store_true")
    p.add_argument("--n-backfill", dest="n_backfill", type=int, default=1)
    p.add_argument("--mem-limit", type=float, default=default_mem, help="in MB")
    p.add_argument("--cpu-limit", type=float, default=8)

    # systemd-run slice confinement (replaces the old --cgroup option)
    p.add_argument(
        "--systemd-run",
        dest="systemd_run_spec",
        default=None,
        metavar="SPEC",
        help=(
            "Relaunch the whole runner (and all child processes) inside a transient "
            "systemd scope unit with resource limits. SPEC is a slash-separated list "
            "of key:value pairs. Supported keys: ncpus (number of CPU cores, e.g. 8), "
            "mem (memory limit in systemd format, e.g. 16G or 16384M). "
            "Either key may be omitted. Examples: \"ncpus:8/mem:16G\", \"ncpus:4\", \"mem:32G\"."
        ),
    )

    # Scheduling (new)
    p.add_argument("--scheduler-policy", default="timeframe",
                   choices=["timeframe", "critical-path", "best-fit"])
    p.add_argument("--drop-should-break", action="store_true",
                   help="In timeframe policy, don't stop scanning on the first "
                        "non-fitting task (lets light tasks slip past heavy ones).")

    # Monitoring (new)
    p.add_argument("--monitor-interval-cpu", type=float, default=1.0)
    p.add_argument("--monitor-interval-mem", type=float, default=1.0)
    p.add_argument("--monitor-backend", default="psutil", choices=["psutil"])

    # Cache (new)
    p.add_argument("--cache-policy", default="off",
                   choices=["off", "lenient", "strict"])

    # Control
    p.add_argument("--stdout-on-failure", action="store_true")
    p.add_argument("--retry-on-failure", type=int, default=0)
    p.add_argument("--no-rootinit-speedup", action="store_true")
    p.add_argument("--remove-files-early", type=str, default="")

    # Accept-and-ignore for backward compatibility of call sites
    # that still pass these flags. They have no effect.
    p.add_argument("--webhook", default=None, help=argparse.SUPPRESS)
    p.add_argument("--checkpoint-on-failure", default=None, help=argparse.SUPPRESS)

    # Logging
    p.add_argument("--action-logfile", default=None)
    p.add_argument("--metric-logfile", default=None)
    p.add_argument("--production-mode", action="store_true")

    return p


def _args_to_config(ns: argparse.Namespace) -> RunnerConfig:
    target_tasks = [f.strip('"').strip("'") for f in ns.target_tasks]
    return RunnerConfig(
        workflowfile=ns.workflowfile,
        maxjobs=ns.maxjobs,
        mem_limit=ns.mem_limit,
        cpu_limit=ns.cpu_limit,
        n_backfill=ns.n_backfill,
        update_resources=ns.update_resources,
        dynamic_resources=ns.dynamic_resources,
        optimistic_resources=ns.optimistic_resources,
        in_systemd_slice=bool(os.environ.get(_IN_SLICE_ENV)),
        systemd_run_spec=ns.systemd_run_spec,
        scheduler_policy=ns.scheduler_policy,
        drop_should_break=ns.drop_should_break,
        monitor_interval_cpu=ns.monitor_interval_cpu,
        monitor_interval_mem=ns.monitor_interval_mem,
        monitor_backend=ns.monitor_backend,
        cache_policy=ns.cache_policy,
        target_tasks=target_tasks,
        target_labels=list(ns.target_labels),
        keep_going=ns.keep_going,
        dry_run=ns.dry_run,
        visualize_workflow=ns.visualize_workflow,
        produce_script=ns.produce_script,
        rerun_from=ns.rerun_from,
        list_tasks=ns.list_tasks,
        retry_on_failure=ns.retry_on_failure,
        no_rootinit_speedup=ns.no_rootinit_speedup,
        remove_files_early=ns.remove_files_early,
        stdout_on_failure=ns.stdout_on_failure,
        production_mode=ns.production_mode,
        action_logfile=ns.action_logfile,
        metric_logfile=ns.metric_logfile,
    )


def _parse_systemd_run_spec(spec: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse "ncpus:N/mem:M" into (cpu_quota_str, mem_str).

    ncpus is given as a number of cores and converted to systemd CPUQuota
    format (e.g. 8 cores → "800%").  mem is passed through as-is.
    Either part may be absent.
    """
    cpu_quota: Optional[str] = None
    mem: Optional[str] = None
    for part in spec.split("/"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"Expected key:value in --systemd-run spec, got: {part!r}")
        key, _, val = part.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "ncpus":
            try:
                cores = float(val)
            except ValueError:
                raise ValueError(f"ncpus must be a number, got: {val!r}")
            cpu_quota = f"{int(cores * 100)}%"
        elif key == "mem":
            mem = val
        else:
            raise ValueError(f"Unknown key in --systemd-run spec: {key!r}. "
                             f"Supported: ncpus, mem")
    return cpu_quota, mem


def _maybe_reexec_in_slice(ns: argparse.Namespace) -> None:
    """If --systemd-run is set and we are not already in the slice, re-exec.

    Uses os.execvp so the current process image is replaced by systemd-run,
    which creates a transient scope cgroup and then exec's the runner again.
    The child runner sees O2DPG_RUNNER_IN_SLICE=1 and skips this function.
    """
    spec = getattr(ns, "systemd_run_spec", None)
    if not spec:
        return
    if os.environ.get(_IN_SLICE_ENV):
        return  # already inside the slice

    if not shutil.which("systemd-run"):
        print(
            "Warning: --systemd-run requested but systemd-run not found on PATH; "
            "continuing without slice confinement.",
            file=sys.stderr,
        )
        return

    try:
        cpu_quota, mem = _parse_systemd_run_spec(spec)
    except ValueError as e:
        print(f"Error in --systemd-run spec: {e}", file=sys.stderr)
        sys.exit(1)

    unit_name = f"o2dpg-runner-{os.getpid()}"
    cmd = ["systemd-run", "--user", "--scope", f"--unit={unit_name}"]
    if cpu_quota:
        cmd.append(f"--property=CPUQuota={cpu_quota}")
    if mem:
        cmd.append(f"--property=MemoryMax={mem}")
    cmd += ["--", sys.executable] + sys.argv

    os.environ[_IN_SLICE_ENV] = "1"
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        # execvp only returns on failure
        del os.environ[_IN_SLICE_ENV]
        print(
            f"Warning: could not exec systemd-run ({e}); "
            "continuing without slice confinement.",
            file=sys.stderr,
        )


def _maybe_draw_workflow(raw_spec):
    try:
        from graphviz import Digraph
    except ImportError:
        print("graphviz not installed; cannot draw workflow")
        return
    dot = Digraph(comment="MC workflow")
    name_to_idx = {}
    for i, node in enumerate(raw_spec["stages"]):
        name_to_idx[node["name"]] = i
        dot.node(str(i), node["name"])
    for node in raw_spec["stages"]:
        to_i = name_to_idx[node["name"]]
        for r in node.get("needs", []):
            if r in name_to_idx:
                dot.edge(str(name_to_idx[r]), str(to_i))
    dot.render("workflow.gv")


def _launch_fileaccess_sidecar(actionlogger_file: str):
    """Start the fanotify-based file-IO graph sidecar if requested."""
    exe = os.getenv("O2DPG_PRODUCE_FILEGRAPH")
    if not exe:
        return None, None, None
    env = os.environ.copy()
    env["FILEACCESS_MON_ROOTPATH"] = os.getcwd()
    env["MAXMOTHERPID"] = f"{os.getpid()}"
    log_file = f"pipeline_fileaccess_{os.getpid()}.log"
    fh = open(log_file, "w")
    proc = subprocess.Popen(
        [exe], stdout=fh, stderr=subprocess.STDOUT, env=env,
    )
    return proc, fh, log_file


def main(argv=None) -> int:
    ns = build_parser().parse_args(argv)
    _maybe_reexec_in_slice(ns)  # may replace this process; returns only if not re-execing
    cfg = _args_to_config(ns)

    # loggers
    action_log = cfg.action_logfile or f"pipeline_action_{os.getpid()}.log"
    metric_log = cfg.metric_logfile or f"pipeline_metric_{os.getpid()}.log"
    action_logger = _setup_logger("pipeline_action_logger", action_log, level=logging.DEBUG)
    metric_logger = _setup_logger("pipeline_metric_logger", metric_log)

    # also route the package-level log records to the action log
    pkg_log = logging.getLogger("o2dpg_runner")
    pkg_log.setLevel(logging.INFO)
    for h in list(pkg_log.handlers):
        pkg_log.removeHandler(h)
    pkg_log.propagate = False
    _h = logging.FileHandler(action_log, mode="a")
    _h.setFormatter(_FORMATTER)
    pkg_log.addHandler(_h)

    # record meta to the metric log (mirrors prototype)
    raw = load_json(cfg.workflowfile)
    meta = raw.get("meta", {}) if isinstance(raw, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    meta.update({
        "cpu_limit": cfg.cpu_limit,
        "mem_limit": cfg.mem_limit,
        "workflow_file": os.path.abspath(cfg.workflowfile),
        "target_task": cfg.target_tasks,
        "rerun_from": cfg.rerun_from,
        "target_labels": cfg.target_labels,
        "scheduler_policy": cfg.scheduler_policy,
        "drop_should_break": cfg.drop_should_break,
        "cache_policy": cfg.cache_policy,
        "systemd_run_spec": cfg.systemd_run_spec,
        "in_systemd_slice": cfg.in_systemd_slice,
    })
    metric_logger.info(meta)

    # visualize if asked (uses raw spec before filtering)
    if cfg.visualize_workflow:
        _maybe_draw_workflow(raw)

    # build workflow (filters, strips global init, builds DAG)
    wf = build_workflow(raw, cfg.target_tasks, cfg.target_labels)
    if not wf.stages:
        if cfg.target_tasks:
            print("Apparently some of the chosen target tasks are not in the workflow")
        else:
            print("Workflow is empty. Nothing to do")
        return 0

    # Apply global env (as the prototype did at construction time)
    for k, v in wf.global_env.items():
        os.environ.setdefault(k, str(v))

    # Optional file-access sidecar
    fileaccess_proc, fileaccess_fh, fileaccess_log_file = _launch_fileaccess_sidecar(action_log)

    rc = 0
    try:
        execer = WorkflowExecutor(cfg, wf, action_logger, metric_logger)
        rc = int(execer.execute())
    finally:
        if fileaccess_proc is not None:
            fileaccess_proc.terminate()
            try:
                fileaccess_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                fileaccess_proc.kill()
            if fileaccess_fh is not None:
                fileaccess_fh.close()
            o2dpg_root = os.getenv("O2DPG_ROOT")
            if o2dpg_root and fileaccess_log_file:
                analyse_cmd = [
                    sys.executable,
                    f"{o2dpg_root}/UTILS/FileIOGraph/analyse_FileIO.py",
                    "--actionFile", action_log,
                    "--monitorFile", fileaccess_log_file,
                    "-o", f"pipeline_fileaccess_report_{os.getpid()}.json",
                    "--basedir", os.getcwd(),
                ]
                print(f"Producing FileIOGraph with command {analyse_cmd}")
                try:
                    subprocess.run(analyse_cmd, check=True)
                except subprocess.CalledProcessError as e:
                    print(f"FileIOGraph analysis failed: {e}", file=sys.stderr)

    return rc


if __name__ == "__main__":
    sys.exit(main())
