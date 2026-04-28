"""Cleanup utilities invoked after a task completes.

Two concerns:
 1. Early file removal based on a FileIOGraph-derived file dependency map
    (--remove-files-early). Files that no task will read/write again are
    deleted to keep disc pressure low during large productions.

 2. Production-mode log archival: .log / .log_done / .log_time files are
    appended to a tar archive and removed. Mirrors the prototype's
    production_endoftask_hook().

Both are side-effecting; errors are logged but don't abort the run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tarfile
from copy import deepcopy
from typing import Dict, Iterable, List, Optional, Set

log = logging.getLogger(__name__)


def _filegraph_expand_timeframes(
    data: Dict, timeframes: Set[int], target_namelist: List[str]
) -> Dict[str, List[Dict]]:
    """Take a single-timeframe template file_report and replicate per TF."""
    tf_entries = [
        e for e in data.get("file_report", [])
        if re.match(r"^\./tf\d+/", e.get("file", ""))
    ]
    result: Dict[str, List[Dict]] = {}
    for i in timeframes:
        if i == -1:
            continue
        new_entries = deepcopy(tf_entries)
        for entry in new_entries:
            entry["file"] = re.sub(r"^\./tf\d+/", f"./tf{i}/", entry["file"])
            entry["written_by"] = [
                re.sub(r"_\d+$", f"_{i}", w) for w in entry.get("written_by", [])
            ]
            for w in entry["written_by"]:
                if w in target_namelist:
                    entry["keep"] = True
            entry["read_by"] = [
                re.sub(r"_\d+$", f"_{i}", r) for r in entry.get("read_by", [])
            ]
        result[f"timeframe-{i}"] = new_entries
    return result


class EarlyFileRemover:
    """Owns the timeframe-expanded file dependency dict and performs
    per-task-completion file deletion."""

    def __init__(
        self,
        filegraph_path: str,
        timeframes: Set[int],
        target_namelist: List[str],
    ):
        with open(filegraph_path) as f:
            data = json.load(f)
        self.file_dict = _filegraph_expand_timeframes(data, timeframes, target_namelist)
        self.timeframes = timeframes
        # Pre-build a reverse index: task_name -> list[file_entry dict] (all TFs)
        # so completions don't re-scan the whole file map.
        self._by_task: Dict[str, List[Dict]] = {}
        for entries in self.file_dict.values():
            for e in entries:
                for t in e.get("written_by", []):
                    self._by_task.setdefault(t, []).append(e)
                for t in e.get("read_by", []):
                    self._by_task.setdefault(t, []).append(e)

    def on_task_done(self, taskname: str) -> None:
        entries = self._by_task.get(taskname, [])
        for entry in entries:
            if taskname in entry.get("read_by", []):
                entry["read_by"].remove(taskname)
            if taskname in entry.get("written_by", []):
                entry["written_by"].remove(taskname)
            if (not entry.get("read_by") and not entry.get("written_by")
                    and not entry.get("keep", False)):
                self._remove_if_exists(entry["file"])

    @staticmethod
    def _remove_if_exists(path: str) -> bool:
        if os.path.exists(path):
            try:
                sz = os.path.getsize(path)
                os.remove(path)
                log.info("Removing %s (no longer needed); freed %.2f MB",
                         path, sz / 1024.0 / 1024.0)
                return True
            except OSError as e:
                log.warning("Could not remove %s: %s", path, e)
        return False


def archive_task_logs(logfile: str) -> None:
    """Append <logfile>, <logfile>_done, <logfile>_time to a tar archive
    and delete the originals. Used in production mode."""
    done = logfile + "_done"
    timef = logfile + "_time"
    try:
        tf = tarfile.open(name="pipeline_log_archive.log.tar", mode="a")
    except Exception as e:
        log.warning("Could not open log archive: %s", e)
        return
    try:
        for path in (logfile, done, timef):
            if os.path.exists(path):
                try:
                    tf.add(path)
                except Exception as e:
                    log.warning("tar add %s failed: %s", path, e)
    finally:
        tf.close()

    for path in (logfile, done, timef):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                log.warning("Could not remove %s: %s", path, e)
