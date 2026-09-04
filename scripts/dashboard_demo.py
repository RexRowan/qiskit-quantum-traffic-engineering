"""Offline dashboard demo -- no IBM credentials needed.

Uses fake backends and a synthetic job that transitions QUEUED -> RUNNING
-> DONE over time, and a queue-depth change partway through to make a
reroute visible without waiting for a real queue.

Run:
    python scripts/dashboard_demo.py
"""

import sys
import time

from qiskit import QuantumCircuit
from qiskit_ibm_runtime.fake_provider import FakeManilaV2, FakeSherbrooke

from qiskit_traffic_engineering import TrafficManager, BackendSelector, HybridScoring
from qiskit_traffic_engineering.reroute import RerouteEngine
from qiskit_traffic_engineering.ledger import JobLedger
from qiskit_traffic_engineering.dashboard import run_dashboard


class DemoJob:
    """A fake job that runs through a realistic lifecycle on its own clock,
    so the dashboard has something changing to show without a real backend."""

    _counter = 0

    def __init__(self, seconds_queued: float = 8.0, seconds_running: float = 6.0):
        DemoJob._counter += 1
        self._id = f"demo-job-{DemoJob._counter}"
        self._created = time.monotonic()
        self._seconds_queued = seconds_queued
        self._seconds_running = seconds_running
        self._cancelled = False

    def job_id(self):
        return self._id

    def status(self):
        if self._cancelled:
            return "CANCELLED"
        elapsed = time.monotonic() - self._created
        if elapsed < self._seconds_queued:
            return "QUEUED"
        if elapsed < self._seconds_queued + self._seconds_running:
            return "RUNNING"
        return "DONE"

    def cancel(self):
        if self.status() != "QUEUED":
            raise RuntimeError("cannot cancel: no longer queued")
        self._cancelled = True


def bell_pair() -> QuantumCircuit:
    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure([0, 1], [0, 1])
    return qc


def simulate_congestion(backend, pending_jobs: int):
    """Monkeypatch a fake backend to report a busy queue, so the demo has
    something worth rerouting away from. Real backends report this on
    their own; fakes always report zero pending jobs."""
    real_status = backend.status

    def congested_status():
        status = real_status()
        status.pending_jobs = pending_jobs
        return status

    backend.status = congested_status


def main():
    backends = [FakeManilaV2(), FakeSherbrooke()]
    # Make Manila look congested so the demo has a visible reroute to show
    # -- real fake backends always report an empty queue otherwise.
    simulate_congestion(backends[0], pending_jobs=50)

    selector = BackendSelector(strategy=HybridScoring())
    ledger = JobLedger()
    # Short SLA so a reroute is visible in a short demo session -- not a
    # realistic real-world value, see README's caveat on defaults.
    reroute_engine = RerouteEngine(ledger, selector, max_wait_seconds=6.0, min_score_improvement=0.05)
    manager = TrafficManager(selector, ledger=ledger, reroute_engine=reroute_engine)

    def submit_fn(backend, circuit):
        return DemoJob(seconds_queued=20.0, seconds_running=5.0)

    circuit = bell_pair()
    # Force the initial submission onto the (already congested) Manila --
    # otherwise the selector would just pick Sherbrooke immediately and
    # there'd be nothing to reroute away from.
    manager.submit(circuit, submit_fn, backends=[backends[0]])
    manager.submit(circuit, submit_fn, backends=[backends[0]])

    auto_reroute = "--observe-only" not in sys.argv
    print("Starting demo dashboard (Ctrl+C to stop)."
          f" auto_reroute={auto_reroute}. Try `--observe-only` for dry-run mode.")
    run_dashboard(manager, backends, submit_fn, circuit=circuit, refresh_seconds=2.0, auto_reroute=auto_reroute)


if __name__ == "__main__":
    main()
