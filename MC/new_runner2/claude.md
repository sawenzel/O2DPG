# O2DPG Workflow Runner — Rewrite Handoff

This file is a handoff note from a claude.ai chat session to Claude Code.
Read it first; the rewrite is well underway and several decisions are
already made.

## Context

- **Project:** ALICE O2DPG, file `MC/bin/o2dpg_workflow_runner.py` (the
  prototype was ~2000 lines, single file, module-global `args`).
- **Goal:** modular Python rewrite for CHEP 2026 poster/proceedings.
  All current features preserved by default. New scheduling policies
  and a threaded monitor unlock A/B measurements for the paper.
- **Language decision:** **stay in Python**. Considered Rust/C++ but
  rejected as overkill (see "Decisions" below).
- **Status:** v1 package written and unit-tested (65 tests passing).
  Real workflow testing has begun; first run found two monitor bugs
  which were fixed (children-cache CPU=0 and iter counter stuck).
  The user is now continuing real-workflow validation.

## Where the code is

A tarball `o2dpg_runner_rewrite.tar.gz` was produced in the previous
session and the user extracted it into the O2DPG checkout. Layout:

```
MC/bin/
    o2dpg_workflow_runner.py            # 10-line shim, preserves filename
    o2dpg_runner/                        # new package
        __init__.py
        cli.py                           # argparse -> RunnerConfig -> Executor
        config.py                        # RunnerConfig dataclass (replaces global args)
        workflow.py                      # load / filter / build DAG
        graph.py                         # Kahn topo sort, memoized descendants
        resources.py                     # TaskResources, ResourceManager
        monitoring.py                    # threaded psutil monitor
        scheduler/
            base.py                      # SchedulerPolicy ABC
            timeframe.py                 # legacy policy (default)
            critical_path.py
            best_fit.py
        executor.py                      # main control loop
        cleanup.py                       # early file removal, log archival
        alienv.py                        # alienv env resolution
        cache.py                         # _done + _done.json fingerprint cache
        README.md
        tests/
            test_graph.py
            test_workflow.py
            test_resources.py
            test_scheduler.py
            test_cache.py
            test_executor_e2e.py
            test_simulator.py
            fixtures/tiny_workflow.json
```

Run tests with:
```
cd MC/bin && python -m pytest o2dpg_runner/tests/ -q
```

## Decisions already made (do not re-litigate)

1. **Pure Python, no Rust/C++**. Discussed and rejected: the runner's
   self-CPU is dominated by kernel-side smaps reads, not Python overhead;
   moving to a native language doesn't help. The architectural fix
   (threaded monitor + cgroup option) does help and is already done.
2. **Removed features:** `--webhook` (Mattermost debug, also a shell
   injection bug) and `--checkpoint-on-failure` (tarball + `alien.py cp`
   for Grid debugging). Both flags are accepted-but-ignored for backward
   compat with calling scripts.
3. **Kept features:** everything else. `_done` files remain the primary
   skip marker; O2 taskwrapper compatibility preserved.
4. **Cache policy:** v1 ships with optional `_done.json` fingerprint
   sidecar (`--cache-policy lenient|strict`, default `off`). NOT a
   content-addressed output cache. We deliberately did not implement
   the bigger Bazel/Nix-style design — see REWRITE_NOTES.md if asked.
5. **Cgroup-v2 monitor backend:** deferred. v1 uses psutil only. The
   `--monitor-backend` flag is reserved for a future cgroup backend.
6. **Three scheduler policies:**
   - `timeframe` (default) — bit-exact prototype behavior.
   - `critical-path` — HEFT-style longest-remaining-path.
   - `best-fit` — iterative bin-packing.
   - Plus `--drop-should-break` flag on `timeframe` to disable the
     legacy "first non-fitting task breaks the default pass" quirk.

## New CLI flags (all default to current behavior)

| Flag                          | Default       |
| ----------------------------- | ------------- |
| `--scheduler-policy`          | `timeframe`   |
| `--drop-should-break`         | off           |
| `--monitor-interval-cpu`      | `1.0` s       |
| `--monitor-interval-mem`      | `1.0` s       |
| `--monitor-backend`           | `psutil`      |
| `--cache-policy`              | `off`         |

## Bugs from the prototype already fixed in v1

(silently fixed; if you need to discuss them with the user point at
`docs/REWRITE_NOTES.md` for full context)

1. `TaskResources.is_within_limits()` compared CPU to mem_limit instead
   of MEM. Memory safety net was effectively disabled.
2. `find_all_dependent_tasks()` cache returned different values on hit
   vs miss (deduped vs duplicated).
3. `filter_workflow()` aliased the caller's dict via assignment, then
   mutated `.stages` in place.
4. `getallrequirements()` — unmemoized recursion on a DAG; exponential
   on diamond patterns. `sys.setrecursionlimit(100000)` was a workaround.
5. `send_webhook()` — shell injection via `os.system` with task name
   interpolation. Removed with the webhook feature.
6. `SIGHandler` only caught SIGINT, not SIGTERM. Grid preemption now
   triggers the shutdown path.
7. `produce_script` used `cd $OLDPWD` which broke if a task `cd`s
   internally. Now uses subshells `( cd "$workdir" && ... )`.
8. `candidates` list used `.count()` for membership checks (O(n));
   replaced with set lookups.
9. **Monitor CPU=0.0 on multi-process tasks (fixed in v1.0.1):**
   `psutil.Process.children()` returns NEW Process objects each call,
   resetting `cpu_percent` baselines. Children must be routed through
   `_get_or_add` to use the persistent cache.
10. **`iter` field stuck (fixed in v1.0.1):** `scheduling_iteration`
    was bumped only on submission. The monitor thread now owns a `tick`
    counter that increments per pass; the executor reads it for the
    metric log line.
11. **Simulator stale priorities (fixed):** Amdahl-derived CPU/walltime
    overrides are now applied before simulator critical-path state is
    built, so optimization and policy comparisons use consistent task
    costs.
12. **Simulator unschedulable-task indexing (fixed):** over-limit tasks
    are kept represented and reported as unschedulable instead of being
    dropped from resource bookkeeping and shifting `tid -> resource`
    indexing.
13. **Amdahl invalid-fit guardrail (fixed):** models are no longer
    emitted/accepted when the measured CPU lies outside the physically
    meaningful range `[1, n_ref]`, which would otherwise yield negative
    serial components.

## Open items / what's next

In rough priority order:

1. **Real workflow validation.** User is testing against an
   embedding-style 3-timeframe pp workflow (full SIM/DIGI/RECO/AOD
   chain, 100+ tasks). After two iterations the metric log produces
   sensible CPU/PSS readings; iter advances per second. Wait for
   confirmation before further changes.
2. **Possible adjustments after real measurements:**
   - `BestFitBackfillPolicy` fitness function (currently
     `(desc+1) * tightness`). May need re-tuning once we see actual
     queue dynamics.
   - The `1.5` and `1900` magic numbers in `ResourceManager.fits_backfill`
     (CPU/MEM overcommit factor and MB-per-core sanity).
   - Default `mem_interval` was 5 s, lowered to 1 s; may revisit for
     production runs where 1 Hz PSS reads are too costly.
3. **Poster measurements to produce:**
   - `timeframe` vs `timeframe --drop-should-break` vs `critical-path`
     vs `best-fit` — same workflow, same resource estimates.
   - Effect of nicing on hole-filling (already supported).
   - Effect of `--remove-files-early` on disc usage curves.
   - All measurable through the metric log, which retains the
     prototype's schema (so existing `o2dpg_sim_metrics.py` still works).
4. **Future work explicitly NOT in v1 scope:**
   - cgroup-v2 monitor backend.
   - Memory-pressure SIGSTOP/SIGCONT response.
   - Content-addressed output cache (Bazel/Nix style).

## Simulator / optimizer status

- `MC/bin/o2dpg_schedule_simulator.py` is now part of the rewrite and is
  used for fast policy comparison and worker-count tuning.
- It supports walltime-weighted critical path, Monte Carlo walltime
  sampling, and Amdahl-based worker-count optimization/write-back.
- Simulated backfill now has three modes:
  - `off` — one hard budget only.
  - `structural` — second admission lane with runner-like
    `n_backfill`, CPU-factor, and MEM-factor rules.
  - `slowdown` — same structural model plus a single fitted slowdown
    multiplier for backfill tasks.
- This is deliberately not a kernel-level model of Linux `nice`; it is a
  scheduler-level approximation intended for comparative studies.

## Conventions and style

- `args` is gone. Everything reads from `RunnerConfig` (a dataclass).
  When adding a new knob, edit `config.py` AND `cli.py` (mapping function
  `_args_to_config`).
- New scheduler policies subclass `SchedulerPolicy` in
  `o2dpg_runner/scheduler/base.py` with two methods: `order()` and
  `pick_submittable()`. Register in `scheduler/__init__.py:get_policy()`.
- Tests live in `o2dpg_runner/tests/`. Fixtures in
  `o2dpg_runner/tests/fixtures/`. Use `_drain(picks, rm)` helper
  (in `test_scheduler.py`) when testing pick_submittable, since the
  generator-based interface depends on the caller booking each pick.
- Log via the `o2dpg_runner` logger namespace; routed to the action
  log file by `cli.py`.
- Don't reintroduce module-level state. The class-based design is
  intentional: it lets tests instantiate multiple executors in one process.

## Communication style preferences (from previous session)

- The user is sandro.wenzel@cern.ch, ALICE/CERN, prefers direct
  technical exchange. They appreciate specific concrete proposals
  with tradeoffs over open-ended brainstorming. They are time-pressed
  (CHEP 2026 deadline) and pro-plan token usage matters.
- When uncertain about a design choice, list options with tradeoffs and
  ask. Do not invent a third option silently.
- Keep prose tight. Code-first, explanation-second.

## How to verify a clean state

```
cd MC/bin
python -m pytest o2dpg_runner/tests/ -q          # 61 tests should pass
./o2dpg_workflow_runner.py --help                 # CLI loads cleanly
./o2dpg_workflow_runner.py -f some_workflow.json --list-tasks
```
