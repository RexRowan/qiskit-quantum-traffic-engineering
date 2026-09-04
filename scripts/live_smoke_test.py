"""Live smoke test against a real QiskitRuntimeService.

This exercises the SAME submission path already verified in the
predecessor package (qiskit-quantum-loadbalancer) -- see its history for
that verification. What's new and genuinely UNVERIFIED here is the reroute
path: cancel() on a real queued job, then resubmit and re-track it.

Run manually (needs real IBM Quantum credentials configured):

    python scripts/live_smoke_test.py

This deliberately uses a very short `max_wait_seconds` so the SLA breach
fires quickly in a manual test run -- do not use a threshold this short
in real usage, it exists here only to make the reroute path exercisable
without waiting a genuinely long queue time.
"""

from qiskit import QuantumCircuit
from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2 as Sampler

from qiskit_traffic_engineering import TrafficManager, BackendSelector, HybridScoring
from qiskit_traffic_engineering.reroute import RerouteEngine
from qiskit_traffic_engineering.ledger import JobLedger


def bell_pair() -> QuantumCircuit:
    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure([0, 1], [0, 1])
    return qc


def main():
    print("Connecting to QiskitRuntimeService...")
    service = QiskitRuntimeService()
    backends = service.backends(operational=True, simulator=False)

    selector = BackendSelector(service=service, strategy=HybridScoring())
    ledger = JobLedger()
    # NOTE: 10s SLA is for manual smoke-testing only, to make the reroute
    # path exercisable in a short session. Real usage should set this to
    # something that reflects actual patience for a real workload.
    reroute_engine = RerouteEngine(ledger, selector, max_wait_seconds=10.0, min_score_improvement=0.05)
    manager = TrafficManager(selector, ledger=ledger, reroute_engine=reroute_engine)

    circuit = bell_pair()

    def submit_fn(backend, circ):
        return Sampler(mode=backend).run([circ])

    print("\n--- Submitting via TrafficManager ---")
    tracked = manager.submit(circuit, submit_fn, backends=backends)
    print(f"Job {tracked.job_id} submitted to {tracked.backend_name}")

    print("\n--- Waiting 15s, then checking for reroute-worthy jobs ---")
    import time
    time.sleep(15)

    rerouted = manager.tick(backends, submit_fn)
    if rerouted:
        for t in rerouted:
            print(f"Rerouted: new job {t.job_id} on {t.backend_name} (reroute #{t.reroute_count})")
    else:
        print("No reroute triggered (job may have started running or SLA/improvement conditions weren't met).")

    ledger.refresh_statuses()
    for t in ledger.all_jobs():
        print(f"  {t.job_id}: backend={t.backend_name} status={t.status} reroutes={t.reroute_count}")


if __name__ == "__main__":
    main()
