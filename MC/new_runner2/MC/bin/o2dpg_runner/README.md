# o2dpg_runner — modular rewrite of the O2DPG workflow runner

This package replaces the monolithic `MC/bin/o2dpg_workflow_runner.py`
prototype (~2000 lines, module-global state) with a modular package.

The entry-point script keeps its original name and location, so every
existing call site continues to work:

```bash
$O2DPG_ROOT/MC/bin/o2dpg_workflow_runner.py -f workflow.json --cpu-limit 8
```

All previously documented flags still work with the same semantics.

## Install

Drop the package and the shim script in `MC/bin/`:

```
MC/bin/
    o2dpg_workflow_runner.py            # 10-line shim (renamed from prototype)
    o2dpg_runner/                       # new package
        __init__.py
        cli.py                          # argparse -> RunnerConfig -> Executor
        config.py                       # RunnerConfig dataclass
        workflow.py                     # load / filter / build DAG
        graph.py                        # Kahn topo sort, memoized descendants
        resources.py                    # TaskResources, ResourceManager
        monitoring.py                   # threaded psutil monitor
        scheduler/
            base.py                     # SchedulerPolicy ABC
            timeframe.py                # legacy policy (default)
            critical_path.py
            best_fit.py
        executor.py                     # main control loop
        cleanup.py                      # early file removal, log archival
        alienv.py                       # alienv env resolution
        cache.py                        # _done + _done.json fingerprint cache
        tests/
            ...
```

No new required dependencies. `psutil` and optionally `graphviz` as before.

## What changed behaviorally

The default invocation should reproduce prototype behavior bit-for-bit
(same scheduling decisions, same output files, same log formats).
Everything new is opt-in.

### New CLI flags (all default to current behavior)

| Flag                          | Default       | Effect                                                                         |
| ----------------------------- | ------------- | ------------------------------------------------------------------------------ |
| `--scheduler-policy`          | `timeframe`   | Choose: `timeframe` (legacy), `critical-path`, or `best-fit`.                  |
| `--drop-should-break`         | off           | In `timeframe`, let light tasks slip past a non-fitting heavy task.            |
| `--monitor-interval-cpu`      | `1.0` (s)     | CPU polling cadence for the background monitor thread.                         |
| `--monitor-interval-mem`      | `5.0` (s)     | PSS polling cadence (much cheaper to read less often).                         |
| `--monitor-backend`           | `psutil`      | Reserved for a future cgroup-v2 backend.                                       |
| `--cache-policy`              | `off`         | Task-completion cache: `off` (legacy), `lenient`, `strict`. See below.         |

### Removed flags

- `--webhook` — the debug Mattermost channel integration is gone. The
  flag is still accepted (for compatibility) but ignored.
- `--checkpoint-on-failure` — the tarball + `alien.py cp` failure
  checkpoint was only used for Grid debugging. Same: accepted, ignored.

### Monitoring

The old runner polled psutil synchronously in the main scheduling loop,
costing 10–20 % of one core on realistic workflows. The new monitor runs
in a background thread with two independent cadences — CPU (cheap,
~1 Hz) and PSS (expensive, ~0.2 Hz). The scheduler reads the latest
snapshot non-blockingly. Result: the runner's self-CPU drops to ~1-2 %.

The scheduler's dynamic-resource sampling (`--dynamic-resources`) still
triggers in `ResourceManager.unbook()`, preserving ordering with
respect to task completions.

### Scheduler policies

Three policies ship, switchable via `--scheduler-policy`:

- **`timeframe`** (default) — exact prototype behavior. Sort by
  `(timeframe, -num_descendants)`; first non-fitting task in the default
  pass breaks the pass (this is the legacy quirk that blocks light tasks
  behind heavy ones). Set `--drop-should-break` to disable that quirk.
- **`critical-path`** — sort by longest-remaining CPU-weighted path to a
  leaf. Standard HEFT-style heuristic; tends to win when resource
  estimates are accurate (after `--update-resources`).
- **`best-fit`** — iterative best-fit bin-packing over the candidate set
  against the remaining CPU/MEM budget. Maximizes parallelism at the cost
  of slightly less predictable task ordering.

For the paper, comparing these three on the same workflow with the same
estimates gives a direct A/B measurement of scheduling strategy impact.

### Cache policy

`_done` files remain the primary skip marker (O2 taskwrapper compatibility
is preserved). With `--cache-policy lenient`, a sidecar `_done.json` is
written containing a fingerprint of (command, env subset, software tag,
needs). On rerun, if the command or `needs` list changed, the `_done`
file is removed and the task re-runs. `strict` additionally invalidates
on env/software changes.

No behavior change unless you pass the flag.

## Bug fixes incorporated

Silently fixed relative to the prototype:

1. `TaskResources.is_within_limits()` compared CPU to mem_limit instead
   of MEM to mem_limit; the memory safety net was effectively disabled.
2. `find_all_dependent_tasks()` cached duplicates but returned
   deduped; cache hits returned different values than cache misses.
3. `filter_workflow()` aliased the caller's dict and mutated it in place.
4. `getallrequirements()` recursed without memoization — exponential on
   diamond DAGs; `sys.setrecursionlimit(100000)` was a workaround.
5. `send_webhook()` shell-interpolated task names into `os.system`
   (command injection). Removed along with the webhook feature.
6. `SIGHandler` only caught SIGINT; Grid preemption via SIGTERM was
   ignored. Now both are handled.
7. The emitted `produce_script` output used `cd $OLDPWD` which broke if
   a task `cd`s internally. Now uses subshells: `( cd "$workdir" && ... )`.
8. `candidates` list used `.count()` for membership checks (O(n));
   replaced by set-based lookups.

## Running the tests

```bash
cd MC/bin
python -m pytest o2dpg_runner/tests/ -q
```

The tests cover:
- `test_graph.py` — Kahn topological sort, memoized descendants/ancestors,
  longest path, diamond + deep-chain cases.
- `test_workflow.py` — load, global-init extraction, filtering by target
  and by label, regex, resource-estimate update.
- `test_resources.py` — booking/unbooking, semaphores, related-task
  grouping, dynamic sampling, limit enforcement.
- `test_scheduler.py` — all three policies, the `should_break` quirk
  and its removal, `n_backfill_max` cap, semaphore blocking.
- `test_cache.py` — cache policies (off/lenient/strict), fingerprint
  sensitivity, sidecar round-trip.
- `test_executor_e2e.py` — the tiny fixture workflow driven end-to-end
  with real subprocesses, exercising each policy, `--dry-run`,
  `--produce-script`, rerun-from-cache behavior.

Integration test (from the prototype, still valid):
```bash
NSIGEVENTS=5 NTIMEFRAMES=2 bash MC/bin/tests/wf_test_pp.sh
```

## A/B measurement suggestions for the paper

The Python entry point accepts the same `workflow.json` under all
scheduling policies. A typical paper-ready comparison:

```bash
# baseline (prototype-equivalent)
./o2dpg_workflow_runner.py -f wf.json \
    --metric-logfile metric_timeframe.log

# drop the should_break quirk
./o2dpg_workflow_runner.py -f wf.json --drop-should-break \
    --metric-logfile metric_timeframe_nobrk.log

# critical path
./o2dpg_workflow_runner.py -f wf.json --scheduler-policy critical-path \
    --metric-logfile metric_cp.log

# best-fit bin-packing
./o2dpg_workflow_runner.py -f wf.json --scheduler-policy best-fit \
    --metric-logfile metric_bf.log
```

The metric logs have the same schema as before (`o2dpg_sim_metrics.py`
post-processing is unaffected), plus the run's meta line now records
`scheduler_policy`, `drop_should_break`, and `cache_policy` for easy
downstream grouping.
