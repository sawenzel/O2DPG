# Plan — CHEP26 proceedings final + new runner to production

Two tracks. They share one dependency: the numbers in the paper must come
from the code that gets merged, so **Track B's B1 (simulator test) is
upstream of Track A's results section**. Otherwise they run in parallel.

Companion document: `CODE_REVIEW.md` (findings referenced below as B1–B5,
C1–C8, D1–D4).

## Status (2026-07-31)

**Track A is done.** `main.tex` builds at **8 pages**, no overfull boxes,
no draft artefacts. Committed in the `CHEP26Proceedings` repo as
"Trim proceedings to the 8-page limit". Base file decision: `main.tex`,
confirmed to be the newest state — its uncommitted edits were back-ports
from `main_alt.tex`. `main_alt.tex` is now a wording donor only; retiring
it is still open (A4).

**Track B0 is done.** The poster rebuilds from the repository alone.

**Track B1+ is deferred by decision** — tooling fixes and production
readiness are a separate, later step. Two decisions taken meanwhile:

- **`--cgroup` (B1): retire it.** The feature is not worth carrying;
  `--systemd-run` supersedes it. But retiring the *feature* is not the
  same as removing the *flag*: the runner must still accept `--cgroup`
  and ignore it with a warning, exactly as it already does for
  `--webhook` and `--checkpoint-on-failure`. Otherwise any JDL passing it
  through `ALIEN_O2DPG_ADDITIONAL_WORKFLOW_RUNNER_ARGS` aborts the job at
  argparse time. One line in `cli.py`; revisit a real cgroup path only if
  a site needs it.
- **`--dynamic-resources` (B5): probably never ported.** Recorded for the
  integration step. Note the discrepancy: the sibling-sampling machinery
  *is* present and does work when driven directly (verified — three
  siblings declared at 4.0 cores / 2000 MB were reassigned to 1.5 / 800
  after the first completed). So whatever is missing is not the sampler.
  Prime suspect is C3 (samples fed per poll rather than per monitor tick,
  which defeats the "<3 samples" guard). Since `anchorMC.sh` passes this
  flag unconditionally, it must be settled before the runner is flipped
  on in production — but it does not block the proceedings.

---

# Track A — proceedings to 8 pages

*(Completed — kept as the record of what was cut and why.)*

## A0. Starting state (measured, not estimated)

Both `main.pdf` and `main_alt.pdf` rebuilt from source: **10 pages**.
Target 8.

Content inventory of `main_alt.tex`:

| Element | Words |
|---|---|
| Abstract | 175 |
| Introduction | 477 |
| From DPL topology to a graph of sub-tasks | 479 |
| Workflow runner | 406 |
| Scheduling and admission | 363 |
| Resource estimates and learning | 389 |
| Results | 580 |
| Disc-space management | 230 |
| Related work | 117 |
| Outlook | 183 |
| Conclusion | 183 |
| Acknowledgements | 34 |
| **Body total** | **3441** |
| Figure/table captions (5) | 236 |
| Bibliography (11 entries) | 180 |

Floats: 4 figures + 1 table, in a **single-column 130 mm** layout
(`webofc` default — `figure*` and `figure` are the same width here).

**The key diagnosis:** text (~5.3 pp) + front matter (~0.5 pp) +
bibliography (~0.75 pp) + figures and captions (~1.4 pp) + table (~0.3 pp)
≈ **8.25 pages of actual content in a 10-page document**. Roughly
1.5–1.75 pages are float-placement whitespace — five floats fighting for
placement in a narrow single column. Pages 3, 5, 7 and 8 run 340–420 words
against 630 on a full text page.

So the 2 pages come from **two independent levers**, and the layout lever is
the cheaper one:

- **A2 (layout): ~1–1.25 pages, zero content lost.**
- **A3 (text): ~600–800 words, the remaining ~0.75–1 page.**

## A1. Decide the base file — *needs your call*

`main.tex` and `main_alt.tex` are both tracked, same section structure;
`main_alt` is the codex editorial rewrite (`ee2eebd` → `a90eed8`, at HEAD),
`main.tex` is the older lineage and carries **93 lines of uncommitted edits**.

Recommendation: **take `main_alt.tex` as the base**, port anything worth
keeping from `main.tex`'s uncommitted diff, then delete `main.tex` so there
is one file to trim rather than two. Maintaining both through a page-limit
squeeze is wasted effort.

One thing to check when you do: `codex_review.md` claims the rewrite
"mention[s] the ~25% makespan gain earlier" in the abstract. **It does not** —
`main_alt`'s abstract contains no number at all. `main.tex`'s abstract is the
original submitted-style one (also no number). If the headline result belongs
in the abstract, it still has to be put there.

## A2. Layout savings — do these first (~1–1.25 pages)

**What actually worked, in hindsight.** The original diagnosis above
attributed the slack to float *placement*, and that was wrong: relaxing
`\topfraction`/`\textfraction`/`\totalnumber` changed the output by
exactly zero pages. LaTeX was already placing the floats as tightly as
it could. The space was going to the *separation* around them, and the
fix was:

```latex
\setlength{\textfloatsep}{10pt plus 2pt minus 3pt}   % default 20pt
\setlength{\intextsep}{8pt plus 2pt minus 2pt}       % default 12pt
\setlength{\abovecaptionskip}{5pt}                   % default 10pt
\setlength{\belowcaptionskip}{0pt}
```

With five floats that recovered ~110 pt on one page alone and was the
single change that took the document from 9 pages to 8. Diagnose this
kind of gap by measuring, not by eye:

```bash
pdftotext -f 7 -l 7 -bbox main.pdf - | grep -oE 'yMin="[0-9.]+"' \
  | sed 's/[^0-9.]//g' | sort -n | uniq \
  | awk 'NR>1{if($1-p>25) print "GAP", $1-p, "pt before y=", $1} {p=$1}'
```

The rest of the list below did contribute, and none of it removes
content.

1. **Merge Fig. 3 and Fig. 4 into one two-panel float.**
   `sim_vs_measured` (0.7\linewidth) and the `pilot`+`disc_compare` minipage
   pair are three panels across two floats with two long captions. One float,
   one caption. **~0.4 page.**
2. **Cut caption length.** 236 words across 5 captions, and several restate
   the body text (`fig:policy-cmp`'s caption re-explains backfilling; the
   `todo.md` item "captions repeated more explanatory text than necessary" is
   still open). Target ~120 words total. **~0.15 page.**
3. **Force tight float placement.** All four figures are `[t]`/`[h]`. Use
   `[!ht]` and, for the two wide ones, drop to `width=0.9\textwidth`
   (`Fig1_combined_v1` is aspect 0.41; `Scheduling_cmp_CPUandMem` is 0.27 and
   already very wide/short). **~0.2 page.**
4. **Inline the results table.** `tab:results` is 4 rows × 4 columns wrapped
   in a float with a 3-line caption. Either shrink the caption to one line or
   fold the numbers into the prose, which already restates every one of them
   (see A3.3). **~0.15–0.3 page.**
5. **Tighten the bibliography.** 11 entries, ~0.75 page. Drop the DOIs/URLs
   on entries that also have volume/page (`ref:DPL`, `ref:O2EPN`);
   `ref:Amdahl` is cited only in Outlook. **~0.1 page.**

Measure after each step (`make alt && pdfinfo main_alt.pdf | grep Pages`) —
float repacking is non-linear and you may reach 9 pages before finishing the
list.

## A3. Text trim — ~600–800 words

The poster's logic is more current than the paper's. Use it as the editorial
guide; these are the concrete deltas.

1. **Fold "Resource estimates and learning" (389 w) toward the poster's
   shape.** The poster gives the pilot its own headline section (§7,
   "Pilot-guided MC campaigns", three reusable artefacts) and the simulator
   its own (§9, "ultra-fast lever for job dimensioning"). The paper buries
   both inside §5, then repeats the pilot again in Results ("Pilot cost",
   ~110 w) and again in Disc-space management. `todo.md` already asks for
   exactly this ("discuss simulator in a separate stage/paragraph";
   "Operation mode: Move pilot to the very end"). Stating the pilot once,
   with the poster's three-artefact framing, removes two restatements.
   **~150 w.**
2. **Kill the triple statement of the headline result.** It appears in the
   Introduction ("up to about 25 %"), in Results ("about 26 %"), and in the
   Conclusion ("about one quarter") — three different renderings of the same
   number, which is also an internal inconsistency a referee will notice.
   Pick one figure (the poster says **26 %, 3368 s → 2482 s**) and state it
   once in the abstract, once in Results. **~80 w**, and it fixes a defect.
3. **Compress the Results walk-through (580 w).** Four paragraphs currently
   re-read the table cell by cell. The poster's decomposition —
   backfilling ~20 %, resource learning ~9 %, critical-path ~5 % — carries
   the same information in three numbers. Keep the table (or the inline
   numbers, per A2.4), keep one paragraph of interpretation, drop the
   cell-by-cell recitation. **~200 w.**
4. **Introduction (477 w) → ~350.** Paragraphs 1–2 set up the EPN↔Grid gap
   twice, once in Run-3 generalities and once concretely. The poster does it
   in one box. The roadmap paragraph (last, ~70 w) can go entirely — with 8
   pages and 10 sections nobody needs a section-by-section map. **~130 w.**
5. **Related work (117 w) → ~70, Outlook (183 w) → ~120.** Related work can
   lose the three-part enumeration and keep the claim that two-tier niced
   backfilling is the non-standard ingredient. Outlook's third bullet
   (elastic workers / Amdahl, ~90 w) is the longest and the least developed;
   compress to two sentences and drop `ref:Amdahl` with it (see A2.5).
   **~110 w.**

That totals ~670 words — the middle of the target range. Items 2 and 3 also
improve the paper independently of the page limit.

## A4. Things to fix while trimming

- Reconcile 25 % / 26 % / "one quarter" (A3.2).
- Put the headline number in the abstract, or decide deliberately not to.
- `todo.md` open item: *"Actually compare memory to max memory used in a full
  DPL workflow"*. The Results "Peak memory" paragraph asserts "more than an
  order of magnitude" against "several hundred GB" from `ref:O2EPN`, without
  a measured number for the MC chain. Either measure it in the pilot run
  (the monitor already records PSS — one number from an existing metric log)
  or soften the claim. A referee will ask.
- `todo.md`: *"What is 'other' in the cpu plots?"* — still unanswered, and
  the plots are in the paper.
- Retire `main.tex`, `image.png`, `image-1.png` (identical 811 kB
  duplicates, both untracked and unused), `tiger.eps` (webofc sample file).

## A5. Verification

```bash
cd ~/alisw/CHEP26Proceedings
make alt && pdfinfo main_alt.pdf | grep Pages     # must read 8
grep -n 'TODO\|PLACEHOLDER' main_alt.tex          # must be empty
```

The `\TODO`/`\PLACEHOLDER` macros are still defined in the preamble — remove
the definitions once the last marker is gone, so a stray one fails the build
instead of rendering in red.

---

# Track B — new runner to production

## B0. Clean starting point (do this before anything else)

`MC/new_runner2/` in your working tree is **untracked on `master`** and its
`.py` files are absent — the sources live on branch
`swenzel/o2dpg_runner_refactor` (31 files, 93 commits ahead of master). What
is on disk but *not* on that branch:

- `todo.md`, `amdahl_simulation.md`
- `CHEP_Poster/Makefile` and **all of `CHEP_Poster/Figs/`** — the poster
  `.tex` is committed but its figures are not, so `poster.pdf` **cannot be
  rebuilt from the branch**. Fix this first; it is the presented artefact.
- `CHEP_Poster/poster_plan.md`, `gemini_review.md`, the poster PDFs
- `foo.tar.gz` (40 kB, Apr 24 — looks like the original rewrite tarball)

Nothing was lost: the aliBuild install at
`sw/…/O2DPG/swenzel-o2dpg_runner_refactor-local1/` is a **stale** May-6 copy,
older than the branch, not newer.

Steps:

1. On the branch, commit the poster figures + `Makefile`, `todo.md`,
   `amdahl_simulation.md`, and these two plan documents. Verify with a fresh
   `git clone` + `make` that the poster builds.
2. Decide on `foo.tar.gz` (almost certainly droppable) and the `.venv/`,
   `__pycache__/` leftovers — add a `.gitignore` rather than deleting.
3. `CHEP26Proceedings` (separate repo) has uncommitted `main.tex` and
   `Makefile`; commit or discard before Track A starts.

## B1. Close the blockers

In dependency order. B1 gates Track A's results section.

| | Item | Where |
|---|---|---|
| 1 | Fix the failing simulator test — decide whether per-task overhead belongs in the CPU denominator | `tests/test_simulator.py`, `o2dpg_schedule_simulator.py` |
| 2 | Merge the forked `o2dpg_sim_metrics.py` back to `MC/utils/` (~500 diff lines) | B4 |
| 3 | Accept-and-ignore `--cgroup` with a warning | B1 |
| 4 | Restore `o2_dpg_workflow_runner.py` as a working name | B2 |
| 5 | Move `rm.add_monitored()` under the tick guard | C3 |
| 6 | Reproduce and fix `--dynamic-resources` — **needs your symptom**, see B5 | B5 |

Then the C-list in one pass: C1 (production-mode `_done` deletion), C2
(`produce_script` completeness), C4, C5 (promote backfill knobs to CLI), C6,
C7 (unique scope names), C8.

## B2. Equivalence harness — the thing that makes the merge safe

The README claims the default invocation "should reproduce prototype
behavior bit-for-bit". Nothing tests that, and it is the claim the merge
rests on. Build it before merging:

- Run prototype and new runner on the same `workflow.json` with
  `--dry-run` and `--cpu-limit 8 --mem-limit 16000`, capture the submission
  order from the action logs, assert identical sequences.
- Do it for 3 workflows (below) × {default, `--dynamic-resources`}.
- Keep it as a script in `MC/bin/tests/`; it is also the regression test for
  every later scheduler change.

Divergence is acceptable *if explained* — the point is that every difference
is deliberate and recorded, not discovered on the Grid.

## B3. Test workflows

Escalating cost. Gate each stage on the previous one passing.

**Stage 1 — offline, no O2 (seconds).**
`pytest o2dpg_runner/tests/` green, plus new tests for the gaps in
`CODE_REVIEW.md`: `EarlyFileRemover` (deletes files — highest priority),
`archive_task_logs`, retry/`--keep-going`, `alienv` mocked.

**Stage 2 — small real workflows, local (minutes).**
- `NSIGEVENTS=5 NTIMEFRAMES=2 bash MC/bin/tests/wf_test_pp.sh` — the existing
  integration test, both runners.
- `MC/run/examples/event_pool.sh` — exercises `-tt pool` and `--rerun-from`,
  i.e. the target-pruning and resume paths that nothing else covers.

**Stage 3 — full chains on a real 8-core/16 GB budget (hours).**
- Standalone pp min-bias, 32-orbit, non-anchored (the `LHC22k5_nightly`-style
  configuration) — this is the benchmark shape the paper reports on.
- Anchored MC via `anchorMC.sh`, which is the only way to exercise
  `--dynamic-resources` + `--remove-files-early` together as production runs
  them. Reuse a known-good anchor rather than a fresh one.
- Embedding workflow (`MC/run/EmbeddingTest_NewRunner/`) — already exercised
  during development; keep it as the multi-tag / `software_package` case.

For each: run under `--systemd-run "ncpus:8/mem:16G"` so the budget is
kernel-enforced rather than advisory, and diff the resulting AOD against the
prototype's. **Physics output must be identical** — the runner changes
scheduling, not results. This is the acceptance criterion, more than makespan.

**Stage 4 — Grid.**
Submit the same workflow via `grid_submit.py` with a prodsplit fan-out, old
runner vs new runner, same site. Accept ordinary Grid attrition; compare
completion rate and CPU efficiency, not individual jobs. This is also where
C1 (resume after preemption) gets its only realistic test.

## B4. Merge shape

Target layout on `master`:

```
MC/bin/o2dpg_workflow_runner.py      # shim (real file)
MC/bin/o2_dpg_workflow_runner.py     # symlink -> above  (B2)
MC/bin/o2dpg_runner/                 # the package
MC/bin/o2dpg_schedule_simulator.py
MC/utils/o2dpg_sim_metrics.py        # single merged copy (B4)
MC/bin/tests/                        # equivalence harness
```

`MC/new_runner2/` disappears; the CHEP material (`CHEP_Poster/`, the
proceedings link, these plan docs) moves somewhere that is not `MC/bin` —
suggest `doc/chep2026/` — or out of the repo entirely into the study
workspace.

Split into reviewable PRs rather than one 7000-line drop:

1. `o2dpg_sim_metrics.py` merge (B4) — standalone, no runner dependency.
2. The `o2dpg_runner` package + shim + symlink + tests, **prototype kept in
   place and still the default**.
3. Flip `anchorMC.sh` and the other call sites to the new runner.
4. Remove the prototype.

PR 2 is where review effort belongs. PR 3 is the one that can be reverted in
one line if the Grid disagrees — which is the point of splitting.

## B5. CI

`.github/workflows/syntax-checks.yml` currently validates JSON and `bash -n`
only; no Python tests run anywhere. Add a job that runs
`pytest MC/bin/o2dpg_runner/tests/ -q` on push. Without it the 66 tests
protect nothing after the first unrelated PR.
