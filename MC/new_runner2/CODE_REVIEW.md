# Code review — `o2dpg_runner` (new workflow runner)

Reviewed at `swenzel/o2dpg_runner_refactor` @ `0407cceb` ("CHEP poster").
Scope: `MC/new_runner2/MC/bin/` — the `o2dpg_runner` package, the
`o2dpg_workflow_runner.py` shim, `o2dpg_schedule_simulator.py`,
`o2dpg_sim_metrics.py`.

## Verdict

The refactor is sound and worth putting into production. The 2003-line
single-file prototype with module-global `args` has become 13 focused
modules (~2100 lines) behind a `RunnerConfig` dataclass, with a policy ABC,
a threaded monitor, and 66 unit tests. Module boundaries are the right ones;
docstrings explain *why*, not just what; the bug fixes listed in `README.md`
are real fixes, not cosmetics.

What follows is what stands between this and a production merge.

Measured on this review:

| | |
|---|---|
| Test suite | **65 passed, 1 failed** (`test_simulator.py`) |
| Smoke run, 11-task synthetic DAG, `--dynamic-resources` | passes, exit 0 |
| Metric-log schema | unchanged, no duplicate `(iter, name)` rows |
| `--cgroup` (prototype flag) | **hard argparse error** |
| `--produce-script` | emits, but drops global-init cmd |

## Blockers

**B1 — `--cgroup` is not accepted and aborts the run.**
The prototype has `--cgroup` (`o2_dpg_workflow_runner.py:58`, applied at
:1950). The new runner dropped it entirely rather than accept-and-ignore it
the way `--webhook` and `--checkpoint-on-failure` are handled. Verified:

```
o2dpg_workflow_runner.py: error: unrecognized arguments: --cgroup /sys/fs/cgroup/foo/cgroup.procs
```

This matters because `anchorMC.sh:436` appends
`${ALIEN_O2DPG_ADDITIONAL_WORKFLOW_RUNNER_ARGS}` — a JDL-controlled string.
Any production JDL carrying `--cgroup` would fail the job at startup, before
a single task runs. `MC/run/EmbeddingTest_OldRunner/run_OmegaCInjected.sh`
still passes it. Fix: accept-and-ignore, with a warning pointing at
`--systemd-run`.

**B2 — entry-point naming is inverted.**
In production the real file is `MC/bin/o2_dpg_workflow_runner.py` and
`o2dpg_workflow_runner.py` is a symlink to it. `anchorMC.sh:433` calls the
*underscore* name. The new layout makes `o2dpg_workflow_runner.py` the real
(18-line) shim and has no `o2_dpg_workflow_runner.py` at all. Both names must
resolve after the merge, or anchored production breaks on the first job.

**B3 — the branch is not green.**
`test_simulator.py::test_simulator_backfill_slowdown_marks_and_slows_backfill_tasks`
fails: expects `cpu == 3.0/1.25 = 2.4`, gets `2.376`, i.e.
`3.0*8.0/(8.0*1.25 + 0.1)`. The simulator divides the task's CPU work by the
walltime *including* the 0.1 s per-task overhead; the test's expectation
excludes it. One of the two is wrong — decide which, because it changes
reported CPU efficiency slightly, and CPU efficiency is a headline number in
the proceedings.

**B4 — `o2dpg_sim_metrics.py` is forked.**
`MC/new_runner2/MC/bin/o2dpg_sim_metrics.py` diverges from the production
`MC/utils/o2dpg_sim_metrics.py` by ~500 diff lines (adds `cgroup_cpu` /
`cgroup_mem` metric names, `json-stat`, walltime handling). Two copies of a
1689-line tool will drift. Merge to one file at `MC/utils/` before anything
else lands.

**B5 — `--dynamic-resources` is the flag production actually uses.**
`anchorMC.sh:434` passes `--dynamic-resources` unconditionally, and
`todo.md` lists "fix `--dynamic-resources` code path" as item one. I
exercised the path directly and the mechanics work — three siblings declared
at 4.0 cores / 2000 MB were reassigned to 1.5 / 800 after the first finished:

```
sgnsim_1: cpu=4.0 mem=2000.0 sampled_cpu=1.5 sampled_mem=800.0
sgnsim_2: cpu=1.5 mem=800.0
sgnsim_3: cpu=1.5 mem=800.0
```

So the defect is not visible from the code or from a synthetic DAG. **This
needs the concrete symptom you saw** before it can be closed; it is the one
blocker I cannot specify further. See C3 for a related real defect on the
same path that may or may not be what you hit.

## Correctness and robustness

**C1 — production mode deletes the resume markers.**
`cleanup.archive_task_logs()` tars `<task>.log`, `_done`, `_time` and then
removes all three. With `--production-mode` the `_done` sentinel — the sole
skip marker — is gone, so a restart after Grid preemption re-runs completed
work. This is *inherited* from the prototype, which carries the same TODO
(`o2_dpg_workflow_runner.py:1728`), so it is not a regression. It is worth
fixing on the way in, because `--production-mode` is the Grid path and
start-stop-resume is a claimed feature of the framework.

**C2 — `produce_script()` emits an incomplete script.**
Verified output on an 11-task DAG:

- the workflow's **global-init command is never emitted** (only the global
  env exports are);
- per-task `alternative_alienv_package` is not emitted, so a mixed-software-
  tag workflow produces a script that runs everything in one environment;
- no `set -e` and no per-task status check — tasks run unconditionally in
  sequence, so a mid-script failure continues silently to the end;
- `workdir` is interpolated unquoted.

The `--produce-script` route is what `_noprogress_error()` tells users to
fall back to, so it should actually reproduce the workflow.

**C3 — monitor samples are recorded once per poll, not once per tick.**
`executor.wait_for_any()` guards the *metric log* with
`tick != self._last_metric_tick` but calls `rm.add_monitored()` for every
snapshot on *every* poll. The poll loop runs at 0.1 s ramping to 1.0 s while
the monitor ticks at `--monitor-interval-cpu` (1.0 s default), so each
snapshot is recorded several times. Consequences:

- `time_collect` / `cpu_collect` / `mem_collect` grow with poll count rather
  than tick count — unbounded memory over a long production job;
- the `len(self.time_collect) < 3` "not enough samples" guard in
  `sample_resources()` becomes meaningless, since three duplicates of one
  reading satisfy it;
- the CPU integral itself survives (duplicates contribute `dt = 0` and the
  deltas telescope), so this degrades the *quality* of dynamic-resource
  sampling rather than obviously breaking it.

Fix: move the `add_monitored` loop inside the existing tick guard. Given B5,
this is my first suspect for the `--dynamic-resources` complaint.

**C4 — `res.nice_value` is never cleared.**
`unbook()` leaves it set. If a policy yields a task but `submit()` returns
`None` (cwd exists and is not a directory), the executor skips booking and
the stale value persists; `book()`'s "never checked → force backfill" safety
net can then no longer fire for that tid on a retry.

**C5 — the runner hardcodes the backfill model the simulator parameterises.**
`ResourceManager` fixes `backfill_cpu_factor = backfill_mem_factor = 1.5`,
and `fits_backfill()` hardcodes `0.9 * cpu_limit` and the `1900` MB-per-core
sanity cut. The simulator exposes `--backfill-cpu-factor`,
`--backfill-mem-factor`, `--n-backfill`, `--backfill-slowdown-factor`. The
simulator therefore cannot be calibrated against the runner it claims to
model without editing runner source. Promote all four to runner CLI flags —
cheap, and it makes the simulator-vs-measurement figure reproducible.

**C6 — sibling grouping can over-match.**
`_global_name()` strips any trailing `_<digits>`. A task whose name ends in a
digit for a non-timeframe reason is grouped with unrelated tasks and
cross-contaminates dynamic sampling. Prototype behaviour; guard on
`timeframe >= 1` while you are in there.

**C7 — systemd scope names collide on retry.**
`_unit_name()` returns `task-<name>-<tid>.scope`, stable across retries of
the same tid. `todo.md` records exactly this symptom:
`Failed to start transient scope unit: Unit task-sgnsim_8-250.scope was
already loaded`. A scope not yet reaped by `--collect` collides with the
retry. Add a run-unique suffix (attempt counter or runner PID).

**C8 — per-task cgroup resolution never gives up.**
`monitoring._one_pass()` probes `children()[0]` on every pass until a cgroup
resolves. If scope creation failed the probe repeats for the task's whole
lifetime. Bounded cost, but add an attempt cap.

## Design observations (not defects)

- **D1** `TimeframeFirstPolicy.pick_submittable()` calls `ordered.index(tid)`
  inside the scan — O(n²) worst case. Irrelevant at current candidate-set
  sizes; one-line fix with `enumerate`.
- **D2** `scheduler/base.py` documents "policies only read `rm`; they don't
  mutate it", but all three policies assign `res.nice_value` inside
  `pick_submittable`. Either move the assignment into `book()` or correct the
  docstring — as written the contract is misleading for the next policy author.
- **D3** `best_fit._fitness()` multiplies `cp_weight * tightness`, where
  `cp_weight` falls back to `descendants_count + 1` when no learned walltime
  exists. That mixes seconds and task counts in one score depending on
  whether `--update-resources` was passed. Only affects the no-learned-data
  case, which is also the case where best-fit is not recommended.
- **D4** `--cache-policy strict` inspects only the allow-listed keys in
  `task["env"]`, not the global or process environment. A changed `O2_ROOT`
  goes unnoticed unless the workflow declares it per task. "strict"
  over-promises; either widen the fingerprint or rename the policy.

## Test coverage gaps

The 66 tests are good where they exist, but the untested surface is exactly
the surface that touches production state:

| Module | Lines | Direct tests |
|---|---|---|
| `cleanup.py` (incl. **file deletion**) | 221 | none |
| `monitoring.py` | 505 | none |
| `alienv.py` | 66 | none |
| `executor.py` retry / failure / `--keep-going` | — | none |

`EarlyFileRemover` deletes files on a live production node driven by a
learned graph, and has no test at all. That is the single most important gap.

Also missing: a test that asserts the README's central claim — that the
default invocation reproduces prototype scheduling decisions. See the
equivalence harness in `PLAN.md` (Track B, B2).
