"""Top-level orchestrator: submit circuits, track them, and periodically
check whether any still-queued job should be rerouted.

This is the "both ends" traffic-management layer: `submit()` uses the
backend-side scoring (scoring.py) to pick where a circuit goes; `tick()`
refreshes the job-side ledger and applies the SLA/outage-based reroute
policy (reroute.py) to anything still waiting.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from qiskit import QuantumCircuit

from .ledger import JobLedger, TrackedJob
from .reroute import RerouteEngine
from .router import BackendRouter
from .selector import BackendSelector


class TrafficManager:
    def __init__(
        self,
        selector: BackendSelector,
        router: Optional[BackendRouter] = None,
        reroute_engine: Optional[RerouteEngine] = None,
        ledger: Optional[JobLedger] = None,
    ):
        self.selector = selector
        self.router = router or BackendRouter(selector)
        self.ledger = ledger or JobLedger()
        self.reroute_engine = reroute_engine or RerouteEngine(self.ledger, selector)

    def submit(
        self,
        circuit: QuantumCircuit,
        submit_fn,
        backends: Optional[Sequence] = None,
        pin: bool = False,
    ) -> TrackedJob:
        """Submit `circuit` via the router and start tracking it.

        `pin=True` marks the resulting job as never eligible for
        rerouting -- for someone who deliberately wants a specific machine
        (pass `backends=[that_backend]` to also force submission there)
        and is fine with normal status tracking in the dashboard/ledger,
        just not ever having `tick()` move it elsewhere."""
        job, chosen = self.router.submit(circuit, submit_fn, backends=backends)
        return self.ledger.track(job, chosen.backend_name, circuit, chosen.score, pinned=pin)

    def tick(self, backends: Sequence, submit_fn) -> List[TrackedJob]:
        """One monitoring pass: refresh job statuses, reroute anything that
        qualifies. Call this on whatever cadence suits you (a loop, a cron
        job, a scheduler) -- nothing here spins its own thread, so this stays
        trivially testable without real sleeps."""
        return self.reroute_engine.tick(backends, submit_fn)

    def run_forever(self, backends: Sequence, submit_fn, interval_seconds: float = 30.0):
        """Convenience blocking loop for scripts that want a simple daemon.
        Prefer calling `tick()` yourself from an existing event loop /
        scheduler in anything more serious than a demo script."""
        import time

        while True:
            self.tick(backends, submit_fn)
            time.sleep(interval_seconds)
