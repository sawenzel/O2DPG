# Does an unprivileged backend learn the same file graph as fanotify?

The file-artefact dependency graph that drives
`o2dpg_workflow_runner.py --remove-files-early` has until now been learned
with fanotify, which needs `CAP_SYS_ADMIN` on the monitor binary and
therefore a machine somebody with root has prepared.  This is the record
of what happened when the same graph was learned with inotify and with
strace instead, neither of which needs any privilege.

Everything below was measured on `alibicompute01` (56 cores, EL9, kernel
5.14, strace 6.12) with `O2sim/v20260820-1` from CVMFS, except where a
laptop measurement is named.  The reference workflow is the one from
`MC/run/test_NEW_FANOTIFY/run_pipeline.sh`: pp, PYTHIA 8, 13.6 TeV,
20 signal events, `-interactionRate 500000`, TGeant4, run to the `aod`
target with `--scheduler-policy critical-path` on an 8-core limit.

## The short answer

On the real two-timeframe MC pilot, against the fanotify graph:

| Backend | files | edges | missing | extra | verdict | template report |
|---|---|---|---|---|---|---|
| fanotify (reference) | 405 | 1389 | — | — | — | — |
| strace | 405 | 1390 | **0** | 1 | SAFE | **EXACT** |
| inotify, 8-way pilot | 405 | 2175 | **0** | 786 | SAFE | SAFE |
| inotify, serial pilot | 405 | 1390 | **0** | 1 | SAFE | **EXACT** |

In every case the one extra edge is the same one, and it is a fanotify
miss rather than an invention: see *What the reference itself gets wrong*
below.

Neither backend loses a single producer or consumer edge, which is the
only direction that can break a production: the runner deletes a file
once every task listed against it is finished, so an edge that is missing
deletes a file a later task still reads, while an extra edge only keeps
the file on disc a little longer.

## Does it do the same job?

Matching reports is necessary, not sufficient.  The graph exists to drive
`--remove-files-early`, so each learned graph was fed to a **fresh
three-timeframe workflow** — one more timeframe than it was learned on,
which also exercises the `./tfN/` -> `./tfX/` templating — alongside a
control run with no early removal at all.

| Learned by | rc | wall | peak disc | files removed early | disc left at the end |
|---|---|---|---|---|---|
| nothing (control) | 0 | 611 s | 410.9 MB | 0 | 430.9 MB |
| fanotify | 0 | 652 s | 271.5 MB | 402 | 71.6 MB |
| inotify | 0 | 610 s | 278.7 MB | 402 | 71.7 MB |
| strace | 0 | 604 s | 270.5 MB | 402 | 71.6 MB |

Every run completed, and **all three graphs removed exactly the same 402
files**.  The peak footprint spans 270.5 to 278.7 MB against a 410.9 MB
control, so early removal moves it by 34 % and the choice of backend
moves it by 3 %.  The wall times in that table are not a measurement of
anything: they are single runs on a shared machine and they scatter by
about 8 %.  That is what the inotify graph's 786 extra
edges are worth in practice: 7.2 MB of a 411 MB working set, because
they sit almost entirely on the CCDB snapshot cache
(480 of them; 50 MB of the 305 MB run directory, and material every task
reads anyway) and on small per-timeframe configuration files.  Of the
157 non-CCDB files carrying an extra edge, two are above 10 MB.

The AODs come out the same size to within 0.1 % (`AO2D.root` 2 160 558 B
in the control, 2 161 912 B with the fanotify graph, 2 162 346 B with the
inotify one, 2 162 222 B with the strace one); they are not bit-identical
because two separate MC runs are not, not because anything was deleted.

## Cost

Wall time for the same workflow, 8-core limit, same machine, no other load:

| Run | wall | vs baseline |
|---|---|---|
| no backend | 469 s | — |
| fanotify | 477 s | +1.7 % |
| inotify | 451 s | −3.8 % (i.e. inside the run-to-run scatter) |
| strace | 553 s | +17.9 % |

strace traced 757 168 opens across the 74 tasks, of which 4 297 were
inside the working directory.  At the measured 59 µs per traced call that
accounts for about 45 s of the 84 s difference; the rest is process
startup, which strace also intercepts.  The cost is paid once, by the
pilot run, and never by production.

`--seccomp-bpf` is what makes this affordable at all.  On a read-heavy
synthetic task (200 opens, 400 000 `pread`s, some CPU): baseline 0.95 s,
`strace -f -e trace=%file` **22.5 s**, the same with `--seccomp-bpf`
**1.00 s**.  The backend probes for the flag and uses it when present.

## What the reference itself gets wrong

Two defects in the fanotify backend showed up only because there was
something to compare it against.

**The process chain is resolved after the fact.**  `monitor_fileaccess_v2`
receives a pid with the event and then walks `/proc/<pid>/status` up to
the runner.  A process that has already exited yields `getppid() == 0`,
so the chain reads `<pid>;0`, no task matches it, and the access is
dropped.  On the real two-timeframe pilot log in
`MC/run/test_NEW_FANOTIFY` this affects 0.86 % of working-directory
records — and that is a lower bound, because an event whose chain fails
`is_good_pid` is never printed at all.  On short-lived tasks it is far
worse: on the synthetic workflow at 8 timeframes and 8-way concurrency,
fanotify lost 28 of 114 edges (75.4 % recall) while inotify and strace
were both exact.

**A rename is invisible.**  The CCDB downloader writes an object under
its versioned name and renames it to `snapshot.root`.  fanotify's
`FAN_CLOSE_WRITE` fires on the original name, so in the fanotify graph
`./ccdb/GLO/Calib/MeanVertex/snapshot.root` has seven readers and no
writer at all.  Both new backends name `grpcreate`.

A third defect was found by reading: on `FAN_Q_OVERFLOW` the event loop
used `continue` without `FAN_EVENT_NEXT`, so it would have spun on the
same event forever and stopped reporting for the rest of the run.  Fixed.

## Where the two differ from each other

strace attributes by construction — the trace file is named after the
task — so it has nothing to be ambiguous about, and its only difference
from fanotify on real MC is the one rename above.

inotify carries no pid, so attribution comes from the task-interval table
and every event is assigned to the tasks that were running at the time.
With the default `tf-affinity` rule, a concurrently running task of a
different timeframe is dropped, which is what keeps the timeframe part of
the graph tight.  What is left over-attributed is the shared top-level
material — `./ccdb/*/snapshot.root` above all — where every one of the
eight concurrent tasks is a candidate and nothing in the file name says
which one it was.  Hence 786 extra edges over 356 files.

The three attribution modes, all re-analysed from the *same* inotify log
of the real two-timeframe pilot, against the same fanotify reference of
1389 edges:

| Mode | recall | edges | verdict |
|---|---|---|---|
| `tf-affinity` (default) | 100 % | 2175 (+786) | SAFE |
| `all` | 100 % | 2714 (+1325) | SAFE |
| `single` | 17.4 % | 243 (−1147) | UNSAFE |

`single` is the measurement that says why the interval table is not
enough on its own: with eight tasks running, 83 % of the graph rests on
moments when more than one of them was alive.

### The serial pilot removes the ambiguity entirely

The poster already describes the pilot as a serial reference run, and
that is exactly the case inotify is best in.  Re-running the same
two-timeframe pilot with `--maxjobs 1` — not `--cpu-limit 1`, which makes
the runner refuse a workflow whose widest task asks for more CPU than the
limit — leaves one task alive at a time, and the inotify graph becomes
**1390 edges against fanotify's 1389, with the timeframe template report
EXACT**: identical to what strace produces, and with the same single
extra edge, which is the rename fanotify misses.

It costs 827 s instead of 451 s, once, for the pilot only.  So the choice
is between a concurrent pilot with a graph that is safe and slightly
loose, and a serial pilot with a graph that is exact.

## Neither needs a privilege

Run on the same machine as uid 65534 with `--no-new-privs` set, which
also neutralises any file capability:

```
--- can this uid open an fanotify group?
fan = fanotify_init(FAN_CLASS_NOTIF, O_RDONLY): Operation not permitted
```

In that same context both backends produced a graph that was **EXACT**
against the analytic truth of the synthetic workflow.

Unprivileged fanotify is not a way out.  Since Linux 5.13 `fanotify_init`
can be called without `CAP_SYS_ADMIN`, but such a group may not use
`FAN_MARK_MOUNT` and, per `fanotify_init(2)`, "will also not receive the
pid that generated the event" — and the pid is the entire attribution
mechanism.  eBPF is the same problem in a different shape:
`unprivileged_bpf_disabled` is 2 on this machine and bpftrace refuses to
start as non-root.

## Recommendation

Both backends do the job.  strace is the more faithful of the two and
needs no change to how the pilot is run, at about 18 % on the pilot's
wall time; inotify is free but wants the serial pilot the poster already
prescribes if the graph is to be exact rather than merely safe.  Either
retires the `setcap cap_sys_admin+ep` step, and with it the constraint
that a pilot can only run where somebody with root has prepared the
binary — which matters because file capabilities do not survive a
`nosuid` mount, so the monitor as distributed through CVMFS could never
carry one.

## How to reproduce

Offline, in seconds, on any Linux box:

```bash
python3 -m unittest discover -s UTILS/FileIOGraph/tests -t UTILS/FileIOGraph/tests
python3 UTILS/FileIOGraph/tests/equivalence_test.py \
        --backends strace --reference fanotify \
        --ntf 8 --cpu-limit 8 --sleep 0.05
```

Drop `--reference fanotify` where no privileged monitor exists; the
synthetic workflow still carries its own analytic truth.  The inotify
backend measured here lives on the `swenzel/filegraph-inotify` branch;
this branch ships fanotify and strace.
