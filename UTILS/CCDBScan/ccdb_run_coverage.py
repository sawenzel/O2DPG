#!/usr/bin/env python3
"""Check that every CCDB object an anchored job used covers the whole run.

A pilot job traced with the FileIOGraph strace backend records the
``./ccdb/<PATH>/snapshot.root`` each task opened, so its report already is
the list of conditions the production depends on.  The pilot only probes
one timestamp though, while the production samples the whole run.  This
tool takes that list and asks CCDB whether each object is valid across the
run -- or across the part of it a run-time-span file leaves in play.

    ccdb_run_coverage.py --filegraph filegraph_strace_123.json --run 553185

Verdicts per object:

  COVERED       run-specific objects span the whole domain
  CATCH_ALL     covered, but only by an object with unbounded validity,
                so the job silently used a default instead of a real
                calibration for this run
  GAP           part of the domain has no valid object; the interval is
                printed, and it is a candidate run-time-span exclusion
  ABSENT        nothing valid anywhere in the domain
  NOT_TIMESTAMP a path whose versions are not keyed by a ms timestamp
                (TPC/Config/FEE is keyed 12..13); cannot be checked this way

Note CCDB validity is half-open: an object valid until T does not answer a
query at exactly T.  An object scoped precisely to a run therefore reports
``Valid-Until == EOR`` and 404s at EOR, which is correct, not a gap.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    import requests
except ImportError:                                        # pragma: no cover
    requests = None

#: orbit duration in microseconds, as in o2dpg_sim_workflow_anchored.py
LHCMaxBunches = 3564
LHCRFFreq = 400.789e6
LHCBunchSpacingNS = 10 * 1.e9 / LHCRFFreq
LHCOrbitNS = LHCMaxBunches * LHCBunchSpacingNS
LHCOrbitMUS = LHCOrbitNS * 1e-3

#: a validity end at or beyond this is a catch-all, not a real interval
CATCH_ALL_UNTIL = 10 ** 13
#: below this a Valid-From cannot be a milliseconds epoch, so the path is
#: keyed by something else (a run number, a version index)
MIN_PLAUSIBLE_MS = 10 ** 11
#: o2dpg_sim_workflow_anchored.exclude_timestamp uses this to tell a span
#: given in orbits from one given in milliseconds
ORBIT_VS_MS_THRESHOLD = 1514761200000

COVERED, CATCH_ALL, GAP, ABSENT, NOT_TIMESTAMP = (
    "COVERED", "CATCH_ALL", "GAP", "ABSENT", "NOT_TIMESTAMP")

_CCDB_SNAPSHOT_RE = re.compile(r'(?:^|/)ccdb/(?P<path>.+)/snapshot\.root$')


# --------------------------------------------------------------- filegraph

def ccdb_paths_from_filegraph(report: Dict) -> Dict[str, Set[str]]:
    """CCDB object path -> the tasks that opened its snapshot."""
    found: Dict[str, Set[str]] = {}
    for section in ("file_report", "file_template_report"):
        for entry in report.get(section, []):
            m = _CCDB_SNAPSHOT_RE.search(entry.get("file", ""))
            if not m:
                continue
            users = found.setdefault(m.group("path"), set())
            users.update(entry.get("read_by", []))
            users.update(entry.get("written_by", []))
    return found


# ------------------------------------------------------------------- CCDB

class CCDBClient:
    """The two CCDB queries this tool needs, and nothing else."""

    def __init__(self, url: str = "http://alice-ccdb.cern.ch", timeout: int = 30):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def validity(self, path: str, timestamp: int) -> Optional[Tuple[int, int]]:
        """(valid_from, valid_until) of the object served at `timestamp`."""
        if requests is None:                               # pragma: no cover
            raise RuntimeError("the requests module is needed for live queries")
        try:
            r = requests.head(f"{self.url}/{path}/{int(timestamp)}",
                              timeout=self.timeout, allow_redirects=False)
        except requests.RequestException:
            return None
        vf, vu = r.headers.get("Valid-From"), r.headers.get("Valid-Until")
        if vf is None or vu is None:
            return None
        return int(vf), int(vu)

    def any_version(self, path: str) -> Optional[Tuple[int, int]]:
        """Validity of some version of `path`, to tell absent from odd keys."""
        if requests is None:                               # pragma: no cover
            raise RuntimeError("the requests module is needed for live queries")
        try:
            r = requests.get(f"{self.url}/browse/{path}", timeout=self.timeout,
                             headers={"Accept": "application/json"})
            objects = r.json().get("objects", [])
        except Exception:
            return None
        if not objects:
            return None
        o = objects[0]
        return int(o.get("validFrom", 0)), int(o.get("validUntil", 0))


# --------------------------------------------------------------- run bounds

class RunBounds:
    def __init__(self, sor: int, eor: int,
                 first_orbit: Optional[int] = None,
                 last_orbit: Optional[int] = None):
        self.sor, self.eor = int(sor), int(eor)
        self.first_orbit, self.last_orbit = first_orbit, last_orbit

    def orbit_to_ms(self, orbit: int) -> Optional[int]:
        if self.first_orbit is None:
            return None
        return int(self.sor + (orbit - self.first_orbit) * LHCOrbitMUS / 1000.)

    def __repr__(self):
        return (f"RunBounds(sor={self.sor}, eor={self.eor}, "
                f"first_orbit={self.first_orbit}, last_orbit={self.last_orbit})")


def run_bounds(run: int, ccdb_url: str) -> RunBounds:
    """Run boundaries, from AggregatedRunInfo when O2 is available.

    That is what the workflow itself used, so it is the authority.  Without
    O2 python bindings fall back to the RCT/Info/RunInformation headers,
    which carry SOR and EOR but no orbits -- enough unless a run-time-span
    file expresses its spans in orbits.
    """
    try:                                                   # pragma: no cover
        from ROOT import o2
        info = o2.parameters.AggregatedRunInfo.buildAggregatedRunInfo(
            o2.ccdb.BasicCCDBManager.instance(), int(run))
        return RunBounds(info.sor, info.eor, info.orbitSOR, info.orbitEOR)
    except Exception:
        pass
    if requests is None:                                   # pragma: no cover
        raise RuntimeError("neither O2 python bindings nor requests available")
    r = requests.get(f"{ccdb_url.rstrip('/')}/browse/RCT/Info/RunInformation/{int(run)}",
                     headers={"Accept": "application/json"}, timeout=30)
    objects = r.json().get("objects", [])
    if not objects:
        raise RuntimeError(f"no RCT/Info/RunInformation for run {run}")
    o = objects[0]
    if "SOR" not in o or "EOR" not in o:
        raise RuntimeError(f"RunInformation for run {run} carries no SOR/EOR")
    return RunBounds(int(o["SOR"]), int(o["EOR"]))


# ------------------------------------------------------------- run-time span

def parse_run_span_file(filename: str, run: int) -> List[Tuple[int, int, bool, str]]:
    """The (from, to, in_orbits, comment) spans this file lists for `run`.

    Same format and same orbits-vs-milliseconds rule as
    o2dpg_sim_workflow_anchored.exclude_timestamp, so the two cannot drift.
    """
    spans = []
    if not filename or not os.path.isfile(filename):
        return spans
    with open(filename) as f:
        for line in f:
            columns = re.split(r'[,\s;\t]+', line.strip(), maxsplit=3)
            if len(columns) < 3:
                continue
            try:
                num1, num2, num3 = map(int, columns[:3])
            except ValueError:
                continue
            if num1 != run:
                continue
            spans.append((num2, num3, num2 < ORBIT_VS_MS_THRESHOLD,
                          columns[3] if len(columns) > 3 else ""))
    return spans


def _subtract(domain: List[List[int]], lo: int, hi: int) -> List[List[int]]:
    out = []
    for a, b in domain:
        if hi <= a or lo >= b:
            out.append([a, b])
            continue
        if a < lo:
            out.append([a, min(lo, b)])
        if b > hi:
            out.append([max(hi, a), b])
    return [iv for iv in out if iv[1] > iv[0]]


def effective_domain(bounds: RunBounds,
                     spans: Sequence[Tuple[int, int, bool, str]],
                     invert: bool = False) -> Tuple[List[List[int]], List[str]]:
    """The part of the run a production will actually sample, and warnings.

    Without a span file that is [SOR, EOR).  A span file removes the listed
    intervals; inverted, it keeps only them.  A span given in orbits needs
    the run's first orbit, which the REST fallback does not have -- such a
    span is reported and *not* applied, which can only make the scan
    stricter, never blinder.
    """
    warnings: List[str] = []
    converted: List[Tuple[int, int]] = []
    for lo, hi, in_orbits, comment in spans:
        if in_orbits:
            lo_ms, hi_ms = bounds.orbit_to_ms(lo), bounds.orbit_to_ms(hi)
            if lo_ms is None:
                warnings.append(
                    f"span {lo}..{hi} is in orbits and no first orbit is known "
                    f"(no O2 bindings); NOT applied{' -- ' + comment if comment else ''}")
                continue
            lo, hi = lo_ms, hi_ms
        converted.append((lo, hi))

    if invert:
        domain = []
        for lo, hi in converted:
            a, b = max(lo, bounds.sor), min(hi, bounds.eor)
            if b > a:
                domain.append([a, b])
        if not converted:
            warnings.append("inverted selection with no spans for this run: "
                            "nothing would be simulated")
        return sorted(domain), warnings

    domain = [[bounds.sor, bounds.eor]]
    for lo, hi in converted:
        domain = _subtract(domain, lo, hi)
    return domain, warnings


# ------------------------------------------------------------------- scan

class PathResult:
    def __init__(self, path: str, verdict: str, detail: str = "",
                 segments: Optional[List[Tuple[int, int]]] = None,
                 gaps: Optional[List[Tuple[int, int]]] = None):
        self.path, self.verdict, self.detail = path, verdict, detail
        self.segments = segments or []
        self.gaps = gaps or []

    def as_dict(self) -> Dict:
        return {"path": self.path, "verdict": self.verdict,
                "detail": self.detail, "segments": self.segments,
                "gaps": self.gaps}


def scan_path(client: CCDBClient, path: str, domain: Sequence[Sequence[int]],
              max_steps: int = 200) -> PathResult:
    """Walk CCDB validity across `domain`, honouring half-open intervals."""
    segments: List[Tuple[int, int]] = []
    gaps: List[Tuple[int, int]] = []
    catch_all = False
    steps = 0

    for lo, hi in domain:
        t = lo
        while t < hi:
            steps += 1
            if steps > max_steps:
                return PathResult(path, GAP, f"more than {max_steps} versions; gave up",
                                  segments, gaps)
            v = client.validity(path, t)
            if v is None:
                # nothing valid here; find where cover resumes, if it does
                nxt = _next_covered(client, path, t, hi)
                gaps.append((t, nxt if nxt is not None else hi))
                if nxt is None:
                    break
                t = nxt
                continue
            vf, vu = v
            if vu >= CATCH_ALL_UNTIL or vf <= 1:
                catch_all = True
            if vu <= t:                       # cannot make progress
                gaps.append((t, hi))
                break
            segments.append((vf, vu))
            t = vu

    if gaps and len(gaps) == 1 and gaps[0][0] == domain[0][0] and not segments:
        # nothing was valid anywhere: is the path keyed by something else?
        any_v = client.any_version(path)
        if any_v is not None and any_v[0] < MIN_PLAUSIBLE_MS:
            return PathResult(path, NOT_TIMESTAMP,
                              f"versions keyed {any_v[0]}..{any_v[1]}, not a ms timestamp")
        return PathResult(path, ABSENT, "no valid object anywhere in the run",
                          segments, gaps)
    if gaps:
        pretty = ", ".join(f"[{a}..{b}]" for a, b in gaps)
        return PathResult(path, GAP, f"uncovered {pretty}", segments, gaps)
    if catch_all:
        return PathResult(path, CATCH_ALL,
                          "covered only via an object with unbounded validity",
                          segments, gaps)
    return PathResult(path, COVERED, f"{len(segments)} version(s)", segments, gaps)


def _next_covered(client: CCDBClient, path: str, start: int, end: int,
                  probes: int = 16) -> Optional[int]:
    """Where coverage resumes after `start`, or None if it never does.

    Sampled rather than solved: CCDB cannot be asked "when does this become
    valid again", so a gap is bracketed by probing across it.  A resumption
    shorter than (end - start) / probes is missed, which leaves the reported
    gap too wide -- the safe direction for a gate.
    """
    if end <= start:
        return None
    step = max(1, (end - start) // (probes + 1))
    t = start + step
    while t < end:
        v = client.validity(path, t)
        if v is not None:
            return v[0] if v[0] > start else t
        t += step
    return None


# ------------------------------------------------------------------- main

def scan(report_paths: Iterable[str], run: int, ccdb_url: str,
         span_file: str = "", invert: bool = False,
         client: Optional[CCDBClient] = None,
         bounds: Optional[RunBounds] = None) -> Dict:
    used: Dict[str, Set[str]] = {}
    for p in report_paths:
        with open(p) as f:
            for path, tasks in ccdb_paths_from_filegraph(json.load(f)).items():
                used.setdefault(path, set()).update(tasks)

    client = client or CCDBClient(ccdb_url)
    bounds = bounds or run_bounds(run, ccdb_url)
    spans = parse_run_span_file(span_file, run)
    domain, warnings = effective_domain(bounds, spans, invert)

    results = [scan_path(client, path, domain) for path in sorted(used)]
    return {"run": run, "bounds": {"sor": bounds.sor, "eor": bounds.eor,
                                   "first_orbit": bounds.first_orbit,
                                   "last_orbit": bounds.last_orbit},
            "domain": domain, "spans": len(spans), "warnings": warnings,
            "objects": len(used),
            "used_by": {p: sorted(t) for p, t in used.items()},
            "results": [r.as_dict() for r in results]}


def _print(summary: Dict) -> None:
    b = summary["bounds"]
    print(f"run {summary['run']}: SOR {b['sor']} EOR {b['eor']} "
          f"({(b['eor'] - b['sor']) / 1000.:.0f} s)")
    dom = summary["domain"]
    covered_ms = sum(hi - lo for lo, hi in dom)
    print(f"effective domain: {len(dom)} interval(s), "
          f"{covered_ms / 1000.:.0f} s, from {summary['spans']} span-file entr(ies)")
    for w in summary["warnings"]:
        print(f"  WARNING {w}")
    print(f"{summary['objects']} CCDB object(s) used by the job\n")

    by_verdict: Dict[str, List[Dict]] = {}
    for r in summary["results"]:
        by_verdict.setdefault(r["verdict"], []).append(r)
    for v in (GAP, ABSENT, CATCH_ALL, NOT_TIMESTAMP, COVERED):
        hits = by_verdict.get(v, [])
        if not hits:
            continue
        print(f"--- {v} ({len(hits)}) ---")
        if v == COVERED:
            continue                       # the boring majority
        for r in hits:
            print(f"    {r['path']:60s} {r['detail']}")
            for lo, hi in r["gaps"]:
                print(f"        candidate span-file line: "
                      f"{summary['run']} {lo} {hi} # no valid object")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--filegraph", nargs="+", required=True,
                    help="FileIOGraph report(s) from a pilot run")
    ap.add_argument("--run", type=int, required=True, help="the anchored run")
    ap.add_argument("--ccdb-url", default="http://alice-ccdb.cern.ch")
    ap.add_argument("--run-time-span-file", dest="span_file", default="",
                    help="same file as ALIEN_JDL_RUN_TIME_SPAN_FILE")
    ap.add_argument("--invert-irframe-selection", action="store_true",
                    help="same meaning as ALIEN_JDL_INVERT_IRFRAME_SELECTION")
    ap.add_argument("--json", default=None, help="also write the full result here")
    ap.add_argument("--fail-on", default="gap",
                    choices=["gap", "catchall", "never"],
                    help="what makes this exit non-zero (default: gap)")
    args = ap.parse_args(argv)

    summary = scan(args.filegraph, args.run, args.ccdb_url,
                   args.span_file, args.invert_irframe_selection)
    _print(summary)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(summary, f, indent=2)

    verdicts = {r["verdict"] for r in summary["results"]}
    bad = {GAP, ABSENT}
    if args.fail_on == "catchall":
        bad = bad | {CATCH_ALL}
    if args.fail_on == "never":
        bad = set()
    return 1 if verdicts & bad else 0


if __name__ == "__main__":
    sys.exit(main())
