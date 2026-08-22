# CCDBScan — does every condition the job used cover the whole run?

An anchored MC production samples timestamps across the whole anchored run,
but a pilot job only probes one of them. A CCDB object that is valid at the
pilot's timestamp and missing three quarters of an hour later will not be
noticed until a fraction of the production has already failed.

`ccdb_run_coverage.py` closes that gap. It needs no new instrumentation,
because a pilot traced with the FileIOGraph strace backend already recorded
every `./ccdb/<PATH>/snapshot.root` its tasks opened — that report *is* the
list of conditions the production depends on, together with the task that
used each one.

```bash
# 1) pilot run, which writes filegraph_strace_<pid>.json
ALIEN_O2DPG_WORKFLOW_RUNNER=new $O2DPG_ROOT/MC/bin/o2_dpg_workflow_runner.py \
    -f workflow.json --filegraph-backends strace

# 2) check that list against the run the production is anchored to
$O2DPG_ROOT/UTILS/CCDBScan/ccdb_run_coverage.py \
    --filegraph filegraph_strace_1176.json --run 553185
```

## Verdicts

| Verdict | Meaning |
|---|---|
| `COVERED` | run-specific objects span the whole domain |
| `CATCH_ALL` | covered, but only by an object with unbounded validity — the job silently used a default instead of a calibration for this run |
| `GAP` | part of the domain has no valid object; the interval is printed |
| `ABSENT` | nothing valid anywhere in the domain |
| `NOT_TIMESTAMP` | the path is not keyed by a millisecond timestamp, so it cannot be checked this way |

`--fail-on` selects what makes the exit code non-zero: `gap` (the default,
covering `GAP` and `ABSENT`), `catchall` (also fail on `CATCH_ALL`), or
`never` for a report-only run in CI.

A `GAP` is printed together with the run-time-span line that would exclude
it, so the answer to a missing condition can be an exclusion rather than a
blocked production:

```
    candidate span-file line: 553185 1718946000000 1718947000000 # no valid object
```

## The run-time-span file

`ALIEN_JDL_RUN_TIME_SPAN_FILE` already tells anchored MC which parts of a run
to skip, and a condition missing inside a skipped part is not a problem. Pass
the same file and the same invert flag, and the scan runs against the domain
the production will really sample:

```bash
ccdb_run_coverage.py --filegraph fg.json --run 553185 \
    --run-time-span-file spans.txt [--invert-irframe-selection]
```

The file format and the rule that tells a span given in orbits from one given
in milliseconds (`< 1514761200000`) are read exactly as
`MC/bin/o2dpg_sim_workflow_anchored.py:exclude_timestamp()` reads them, so the
two cannot drift apart. Converting an orbit-based span needs the run's first
orbit, which is only available with O2 python bindings; without them such a
span is reported and **not** applied, which can only make the scan stricter.

## Two things that look like bugs and are not

**CCDB validity is half-open.** An object valid until `T` does not answer a
query at exactly `T`. An object scoped precisely to one run therefore reports
`Valid-Until == EOR` and returns 404 at EOR. That is correct. Probing the
closed interval instead makes every run-scoped object — `FT0/Calib/EventsPerBc`
among them — look like it fails before the end of the run.

**Not every path is keyed by a timestamp.** `TPC/Config/FEE` has versions
keyed `12..13`. A timestamp query legitimately finds nothing, and calling that
"missing" is a false alarm, so it is reported as `NOT_TIMESTAMP` instead.

## Run bounds

Taken from `AggregatedRunInfo` when O2 python bindings are importable — the
same source the workflow itself used, and the only one that also gives the
first and last orbit. Otherwise the tool falls back to the `SOR`/`EOR` headers
of `RCT/Info/RunInformation/<run>` over plain REST, so it also runs in CI with
nothing but `requests`.

## Tests

```bash
cd UTILS/CCDBScan && python3 -m unittest discover -s tests -t tests
```

Offline and network-free: a fake CCDB serves intervals that are known by
construction, including the half-open run-scoped case, a real gap, a
catch-all, a non-timestamp-keyed path, and a gap that disappears once a
run-time-span exclusion is applied.
