"""Decides when a still-queued job should be cancelled and resubmitted
elsewhere, and does it.

Design stance (read before changing thresholds): this is deliberately NOT
"hunt for the shortest queue at every tick." An automated tool that cancels
and resubmits purely to jump to a shorter line looks like gaming IBM's
fair-share scheduler, and undermines rather than helps any pitch to have
this considered for real adoption. Instead, a reroute only fires when:

  1. The job has been queued longer than a user-declared SLA
     (`max_wait_seconds`) -- this enforces *your own* stated patience, it
     doesn't opportunistically chase every improvement -- OR the current
     backend has gone non-operational (a real outage/recalibration, not a
     queue-length judgment call).
  2. A candidate backend beats the current backend's LIVE score (not its
     score at original submission time -- a backend that started great
     can degrade after submission) by more than `min_score_improvement`
     (skipped for outages -- any operational backend beats a dead one).
     Small differences aren't worth the re-queue cost: you lose your
     position and go to the back of a new line.
  3. The job hasn't already been rerouted `max_reroutes_per_job` times --
     a hard cap against thrashing a single circuit around the fleet.
  4. The job isn't pinned (`TrackedJob.pinned`, set via `pin=True` on
     `JobLedger.track()`/`TrafficManager.submit()`) -- for someone who
     deliberately wants a specific machine and is fine with normal status
     tracking, just not ever having it moved. Checked before anything
     else in `evaluate()`.

If none of that is true, the job is left alone, even if a "better" backend
is visible. Waiting is the default; rerouting is the exception.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

from qiskit import transpile

from .ledger import JobLedger, TrackedJob
from .selector import BackendSelector


@dataclass
class RerouteDecision:
    tracked_job: TrackedJob
    new_backend: object
    new_backend_name: str
    new_score: float
    reason: str


class RerouteEngine:
    def __init__(
        self,
        ledger: JobLedger,
        selector: BackendSelector,
        max_wait_seconds: float = 600.0,
        min_score_improvement: float = 0.15,
        max_reroutes_per_job: int = 2,
        optimization_level: int = 1,
    ):
        self.ledger = ledger
        self.selector = selector
        self.max_wait_seconds = max_wait_seconds
        self.min_score_improvement = min_score_improvement
        self.max_reroutes_per_job = max_reroutes_per_job
        self.optimization_level = optimization_level

    @staticmethod
    def _is_backend_operational(backend) -> bool:
        try:
            return bool(backend.status().operational)
        except Exception:
            return True  # unknown -> don't treat as an outage

    def evaluate(self, tracked: TrackedJob, backends: Sequence) -> Optional[RerouteDecision]:
        """Pure decision logic: return a RerouteDecision if `tracked`
        qualifies, else None. Performs backend status/scoring calls but no
        cancellation or submission, so it's testable independently of
        `execute`."""
        if tracked.pinned:
            return None
        if tracked.reroute_count >= self.max_reroutes_per_job:
            return None

        current_backend = next(
            (b for b in backends if getattr(b, "name", None) == tracked.backend_name), None
        )
        outage = current_backend is not None and not self._is_backend_operational(current_backend)

        elapsed = time.monotonic() - tracked.submitted_at
        sla_breached = elapsed > self.max_wait_seconds

        if not (outage or sla_breached):
            return None

        ranked = self.selector.rank(tracked.circuit, backends=backends)
        candidates = [r for r in ranked if r.backend_name != tracked.backend_name]
        if not candidates:
            return None

        # Compare against the current backend's LIVE score, not the score
        # recorded at original submission time. A job's assigned backend
        # can degrade after submission (queue grows, calibration drifts);
        # gating on the stale submission-time snapshot would mean a
        # backend that started great can never be judged "improved upon"
        # later, even after it's clearly gotten worse. If the current
        # backend no longer ranks at all (e.g. it stopped fitting the
        # circuit), treat its current score as 0 -- anything viable beats
        # "nothing works there right now."
        current_entry = next((r for r in ranked if r.backend_name == tracked.backend_name), None)
        current_score = current_entry.score if current_entry is not None else 0.0

        best = candidates[0]
        if not outage and (best.score - current_score) < self.min_score_improvement:
            return None

        reason = (
            "backend outage"
            if outage
            else f"SLA breached ({elapsed:.0f}s > {self.max_wait_seconds:.0f}s)"
        )
        return RerouteDecision(
            tracked_job=tracked,
            new_backend=best.backend,
            new_backend_name=best.backend_name,
            new_score=best.score,
            reason=reason,
        )

    def execute(self, decision: RerouteDecision, submit_fn) -> TrackedJob:
        """Cancel the old job (best-effort), re-transpile to the new
        backend's ISA (IBM Runtime rejects non-ISA circuits, and the new
        backend's basis gates/coupling map generally differ from the old
        one's -- reusing the old ISA circuit is wrong, not just suboptimal),
        and submit the replacement via `submit_fn(backend, circuit) -> job`,
        recording both in the ledger."""
        tracked = decision.tracked_job
        try:
            tracked.job.cancel()
        except Exception:
            # It may have started running between refresh_statuses() and
            # now -- nothing left to reroute at that point.
            return tracked

        isa_circuit = transpile(
            tracked.circuit, backend=decision.new_backend, optimization_level=self.optimization_level
        )
        new_job = submit_fn(decision.new_backend, isa_circuit)
        new_tracked = self.ledger.track(
            new_job, decision.new_backend_name, tracked.circuit, decision.new_score
        )
        new_tracked.reroute_count = tracked.reroute_count + 1
        self.ledger.mark_superseded(tracked.job_id, new_tracked.job_id)
        return new_tracked

    def tick(self, backends: Sequence, submit_fn) -> List[TrackedJob]:
        """Refresh job statuses, evaluate every queued job, and execute any
        qualifying reroutes. Returns the newly-created TrackedJobs (empty if
        nothing was rerouted this tick)."""
        self.ledger.refresh_statuses()
        rerouted: List[TrackedJob] = []
        for tracked in self.ledger.queued_jobs():
            decision = self.evaluate(tracked, backends)
            if decision is not None:
                rerouted.append(self.execute(decision, submit_fn))
        return rerouted
