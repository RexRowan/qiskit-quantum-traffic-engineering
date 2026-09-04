"""Tracks jobs submitted through the traffic manager and their live status.

This is the "job side" half of "monitors both ends" -- the backend side
(queue depth, calibration) is handled by scoring.py/monitor.py; this module
tracks what's actually happened to the jobs *you* submitted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Status strings match qiskit_ibm_runtime.runtime_job_v2.RuntimeJobV2.status():
# QUEUED, INITIALIZING, RUNNING, DONE, CANCELLED, ERROR.
QUEUED_STATUSES = {"QUEUED", "INITIALIZING"}
TERMINAL_STATUSES = {"DONE", "CANCELLED", "ERROR"}


@dataclass
class TrackedJob:
    """A single submission's lifecycle as observed by the ledger."""

    job: object
    job_id: str
    backend_name: str
    circuit: object
    score_at_submission: float
    submitted_at: float = field(default_factory=time.monotonic)
    status: str = "QUEUED"
    reroute_count: int = 0
    superseded_by: Optional[str] = None
    pinned: bool = False


class JobLedger:
    """In-memory record of tracked jobs, keyed by job ID.

    Not persisted -- if your process restarts, tracking restarts too. For
    anything beyond a single-process script, back this with real storage
    (a DB, a file) using the same TrackedJob shape.
    """

    def __init__(self):
        self._jobs: Dict[str, TrackedJob] = {}

    def track(
        self, job, backend_name: str, circuit, score_at_submission: float, pinned: bool = False
    ) -> TrackedJob:
        """Track a job. `pinned=True` marks it as never eligible for
        rerouting -- for someone who deliberately wants a specific machine
        and is fine with normal status tracking, just not RerouteEngine
        moving it. See RerouteEngine.evaluate(), which checks this first."""
        job_id = job.job_id()
        try:
            initial_status = str(job.status())
        except Exception:
            initial_status = "QUEUED"
        tracked = TrackedJob(
            job=job,
            job_id=job_id,
            backend_name=backend_name,
            circuit=circuit,
            score_at_submission=score_at_submission,
            status=initial_status,
            pinned=pinned,
        )
        self._jobs[job_id] = tracked
        return tracked

    def refresh_statuses(self) -> None:
        """Poll every non-terminal job's current status. Call this before
        making reroute decisions -- decisions are only as fresh as the last
        refresh."""
        for tracked in self._jobs.values():
            if tracked.status in TERMINAL_STATUSES:
                continue
            try:
                tracked.status = str(tracked.job.status())
            except Exception:
                # Leave the last-known status rather than guessing; a
                # transient status-check failure shouldn't trigger a reroute.
                continue

    def queued_jobs(self) -> List[TrackedJob]:
        return [t for t in self._jobs.values() if t.status in QUEUED_STATUSES]

    def all_jobs(self) -> List[TrackedJob]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> Optional[TrackedJob]:
        return self._jobs.get(job_id)

    def mark_superseded(self, old_job_id: str, new_job_id: str) -> None:
        old = self._jobs.get(old_job_id)
        if old is not None:
            old.status = "CANCELLED"
            old.superseded_by = new_job_id
