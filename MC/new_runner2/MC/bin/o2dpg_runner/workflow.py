"""Workflow loading, filtering, and DAG construction.

The workflow JSON schema is unchanged from the original. Key fields per
stage:
  - name: str
  - needs: list[str]  (names of upstream stages)
  - cmd: str
  - cwd: str
  - timeframe: int (-1 for global stages)
  - labels: list[str]
  - resources: {cpu, mem, relative_cpu}
  - semaphore: str (optional)
  - retry_count: int (optional)
  - alternative_alienv_package: str (optional)
  - env: dict (optional)

One stage may be the synthetic ``__global_init_task__`` at index 0,
holding global env and an optional init cmd; it is stripped from the
DAG during loading.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .graph import build_adjacency

log = logging.getLogger(__name__)


@dataclass
class Workflow:
    """In-memory representation of a workflow after filtering.

    Tasks are stored as a list of dicts (the raw JSON objects) and indexed
    by integer ``tid``. The forward/reverse adjacency and derived quantities
    are computed once at construction time.
    """
    stages: List[Dict[str, Any]]
    global_env: Dict[str, str] = field(default_factory=dict)
    global_init_cmd: Optional[str] = None
    full_target_names: List[str] = field(default_factory=list)

    # Derived
    name_to_id: Dict[str, int] = field(default_factory=dict)
    id_to_name: List[str] = field(default_factory=list)
    forward_adj: List[List[int]] = field(default_factory=list)
    reverse_adj: List[List[int]] = field(default_factory=list)
    indegree: List[int] = field(default_factory=list)
    timeframes: Set[int] = field(default_factory=set)

    def __post_init__(self):
        self._rebuild_indices()

    def _rebuild_indices(self):
        self.name_to_id = {s["name"]: i for i, s in enumerate(self.stages)}
        self.id_to_name = [s["name"] for s in self.stages]
        edges: List[Tuple[int, int]] = []
        for i, s in enumerate(self.stages):
            for n in s.get("needs", []):
                if n in self.name_to_id:
                    edges.append((self.name_to_id[n], i))
        self.forward_adj, self.reverse_adj, self.indegree = build_adjacency(
            len(self.stages), edges
        )
        self.timeframes = {s.get("timeframe", -1) for s in self.stages}

    def tid(self, name: str) -> int:
        return self.name_to_id[name]

    def name(self, tid: int) -> str:
        return self.id_to_name[tid]

    def n_tasks(self) -> int:
        return len(self.stages)


def load_json(path: str) -> Dict[str, Any]:
    with open(path) as fp:
        return json.load(fp)


def extract_global_init(raw_spec: Dict[str, Any]) -> Tuple[Dict[str, str], Optional[str]]:
    """Pull out the synthetic __global_init_task__ if present.

    Mutates raw_spec['stages'] in place (removes the init stage).
    """
    env: Dict[str, str] = {}
    init_cmd: Optional[str] = None
    stages = raw_spec.get("stages", [])
    if stages and stages[0].get("name") == "__global_init_task__":
        init = stages[0]
        env_in = init.get("env")
        if env_in:
            env = {k: str(v) for k, v in env_in.items()}
        cmd = init.get("cmd")
        if cmd and cmd != "NO-COMMAND":
            init_cmd = cmd
        del stages[0]
    return env, init_cmd


def filter_workflow(
    raw_spec: Dict[str, Any],
    targets: List[str],
    target_labels: List[str],
) -> Tuple[Dict[str, Any], List[str]]:
    """Filter the raw spec down to tasks matching target selectors.

    Returns (new_spec, full_target_names). When no filter is requested,
    returns (raw_spec, []). The returned spec is always a fresh top-level
    dict (no aliasing bug like in the prototype), but the per-stage dicts
    are shared.
    """
    stages = raw_spec.get("stages", [])
    if not targets:
        return {**raw_spec, "stages": list(stages)}, []
    if not target_labels and len(targets) == 1 and targets[0] == "*":
        return {**raw_spec, "stages": list(stages)}, []

    name_to_idx = {t["name"]: i for i, t in enumerate(stages)}

    def task_matches(name: str) -> bool:
        for f in targets:
            if f == "*":
                return True
            if re.match(f, name) is not None:
                return True
        return False

    def task_matches_labels(t: Dict[str, Any]) -> bool:
        if not target_labels:
            return True
        for lbl in t.get("labels", []):
            if lbl in target_labels:
                return True
        return False

    # Memoized canBeDone using iterative traversal.
    ok_cache: Dict[str, bool] = {}

    def can_be_done(name: str) -> bool:
        if name in ok_cache:
            return ok_cache[name]
        idx = name_to_idx.get(name)
        if idx is None:
            ok_cache[name] = False
            return False
        # iterative post-order DFS
        order: List[str] = []
        seen: Set[str] = {name}
        stack: List[Tuple[str, int]] = [(name, 0)]
        while stack:
            cur, ci = stack[-1]
            needs = stages[name_to_idx[cur]].get("needs", []) if cur in name_to_idx else []
            if ci < len(needs):
                stack[-1] = (cur, ci + 1)
                child = needs[ci]
                if child not in seen and child not in ok_cache:
                    if child not in name_to_idx:
                        ok_cache[child] = False
                    else:
                        seen.add(child)
                        stack.append((child, 0))
            else:
                stack.pop()
                order.append(cur)
        for cur in order:
            if cur in ok_cache:
                continue
            idx2 = name_to_idx.get(cur)
            if idx2 is None:
                ok_cache[cur] = False
                continue
            ok = all(ok_cache.get(r, False) for r in stages[idx2].get("needs", []))
            ok_cache[cur] = ok
            if not ok:
                log.info("Disabling target %s due to unsatisfied requirements", cur)
        return ok_cache[name]

    full_target_list = [
        t for t in stages
        if task_matches(t["name"]) and task_matches_labels(t) and can_be_done(t["name"])
    ]
    full_target_names = [t["name"] for t in full_target_list]

    # Collect all upstream requirements (iterative, deduped).
    needed: Set[str] = set(full_target_names)
    stack2 = list(full_target_names)
    while stack2:
        cur = stack2.pop()
        idx = name_to_idx.get(cur)
        if idx is None:
            continue
        for r in stages[idx].get("needs", []):
            if r not in needed:
                needed.add(r)
                stack2.append(r)

    new_stages = [t for t in stages if t["name"] in needed]
    new_spec = {**raw_spec, "stages": new_stages}
    return new_spec, full_target_names


def build_workflow(
    raw_spec: Dict[str, Any],
    targets: List[str],
    target_labels: List[str],
) -> Workflow:
    """End-to-end: strip global-init, filter, build DAG."""
    # Operate on a shallow copy at top level so we don't mutate caller's dict.
    spec = {**raw_spec, "stages": list(raw_spec.get("stages", []))}
    # extract_global_init mutates spec['stages']
    env, init_cmd = extract_global_init(spec)
    filtered, target_names = filter_workflow(spec, targets, target_labels)
    wf = Workflow(
        stages=filtered["stages"],
        global_env=env,
        global_init_cmd=init_cmd,
        full_target_names=target_names,
    )
    return wf


def update_resource_estimates(workflow: Workflow, resource_json_path: str) -> None:
    """Apply learned resource estimates from a JSON file.

    The JSON is produced by o2dpg_sim_metrics.py json-stat and is keyed on
    the "global" task name (i.e. with the _<timeframe> suffix stripped).
    """
    with open(resource_json_path) as fp:
        resource_dict = json.load(fp)

    for task in workflow.stages:
        tf = task.get("timeframe", -1)
        name = task["name"]
        if tf >= 1:
            global_name = "_".join(name.split("_")[:-1])
        else:
            global_name = name

        if global_name not in resource_dict:
            continue
        new_res = resource_dict[global_name]

        new_mem = new_res.get("pss", {}).get("max")
        if new_mem is not None:
            old = task["resources"]["mem"]
            log.info("Updating MEM estimate for %s: %s -> %s", name, old, new_mem)
            task["resources"]["mem"] = new_mem

        new_cpu = new_res.get("cpu", {}).get("mean")
        if new_cpu is not None:
            old = task["resources"]["cpu"]
            rel = task["resources"].get("relative_cpu")
            if rel is not None:
                new_cpu *= rel
            log.info("Updating CPU estimate for %s: %s -> %s", name, old, new_cpu)
            task["resources"]["cpu"] = new_cpu
