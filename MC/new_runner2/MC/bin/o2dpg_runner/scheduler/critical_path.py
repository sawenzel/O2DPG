"""Critical-path-first scheduler.

Sorts candidates by longest remaining path to any leaf, weighted by
per-task CPU*assumed_walltime estimate. Identical submit discipline as
TimeframeFirstPolicy without should_break (no reason to block light
tasks once we've committed to CP prioritization).

This is a standard HEFT-style heuristic. Useful when resource estimates
are learned (--update-resources) and therefore reasonably accurate.
"""

from __future__ import annotations

from typing import Iterator, List, Tuple

from .base import SchedulerPolicy, SchedulerState
from ..resources import ResourceManager


class CriticalPathPolicy(SchedulerPolicy):
    name = "critical-path"

    def order(self, candidates: List[int], state: SchedulerState) -> List[int]:
        cp = state.critical_path
        tfw = state.timeframe_weight
        # primary: longest path (largest first); tie-break: timeframe, tid
        return sorted(
            candidates,
            key=lambda t: (-cp[t] if cp else 0, tfw[t][0], t),
        )

    def pick_submittable(
        self, ordered: List[int], rm: ResourceManager
    ) -> Iterator[Tuple[int, int]]:
        if rm.at_proc_cap():
            return
        skipped: List[int] = []
        for tid in ordered:
            res = rm.resources[tid]
            if not rm.can_be_submitted_at_all(res):
                continue
            if rm.fits_default(res):
                res.nice_value = rm.nice_default
                yield tid, rm.nice_default
            else:
                skipped.append(tid)

        if rm.at_proc_cap():
            return
        for tid in skipped:
            res = rm.resources[tid]
            if not rm.can_be_submitted_at_all(res):
                continue
            if rm.fits_backfill(res):
                res.nice_value = rm.nice_backfill
                yield tid, rm.nice_backfill
