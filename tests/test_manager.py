import time

from qiskit import QuantumCircuit
from qiskit_ibm_runtime.fake_provider import FakeManilaV2, FakeSherbrooke

from qiskit_traffic_engineering.manager import TrafficManager
from qiskit_traffic_engineering.scoring import QueueOnlyScoring
from qiskit_traffic_engineering.selector import BackendSelector
from qiskit_traffic_engineering.reroute import RerouteEngine
from qiskit_traffic_engineering.ledger import JobLedger

from fake_backend import FakeBackend
from fake_job import FakeJob


def bell_pair():
    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure([0, 1], [0, 1])
    return qc


def test_submit_tracks_job_with_isa_circuit():
    selector = BackendSelector(strategy=QueueOnlyScoring())
    manager = TrafficManager(selector)

    received = {}

    def submit_fn(backend, circuit):
        received["backend"] = backend
        received["circuit"] = circuit
        return FakeJob(status="QUEUED")

    tracked = manager.submit(bell_pair(), submit_fn, backends=[FakeManilaV2(), FakeSherbrooke()])

    backend = received["backend"]
    circuit = received["circuit"]
    used = {instr.operation.name for instr in circuit.data}
    assert used <= set(backend.target.operation_names)
    assert tracked.backend_name == backend.name


def test_submit_pin_pins_the_job_and_forces_its_backend():
    selector = BackendSelector(strategy=QueueOnlyScoring())
    manager = TrafficManager(selector)

    def submit_fn(backend, circuit):
        return FakeJob(status="QUEUED")

    specific = FakeManilaV2()
    tracked = manager.submit(bell_pair(), submit_fn, backends=[specific], pin=True)

    assert tracked.pinned is True
    assert tracked.backend_name == specific.name


def test_tick_reroutes_via_manager_with_real_backends():
    selector = BackendSelector(strategy=QueueOnlyScoring())
    ledger = JobLedger()
    reroute_engine = RerouteEngine(ledger, selector, max_wait_seconds=0.01, min_score_improvement=0.1)
    manager = TrafficManager(selector, ledger=ledger, reroute_engine=reroute_engine)

    backend_a = FakeManilaV2()
    backend_b = FakeSherbrooke()

    def patch_pending(backend, n):
        real_status = backend.status

        def patched():
            status = real_status()
            status.pending_jobs = n
            return status

        backend.status = patched

    patch_pending(backend_a, 100)
    patch_pending(backend_b, 0)

    old_job = FakeJob(status="QUEUED")
    ledger.track(old_job, "fake_manila", bell_pair(), score_at_submission=0.05)
    time.sleep(0.02)

    def submit_fn(backend, circuit):
        return FakeJob(status="QUEUED")

    rerouted = manager.tick([backend_a, backend_b], submit_fn)
    assert len(rerouted) == 1
    assert rerouted[0].backend_name == "fake_sherbrooke"
    assert old_job.cancelled


def test_tick_skips_pinned_job_but_still_reroutes_an_unpinned_one():
    selector = BackendSelector(strategy=QueueOnlyScoring())
    ledger = JobLedger()
    reroute_engine = RerouteEngine(ledger, selector, max_wait_seconds=0.01, min_score_improvement=0.1)
    manager = TrafficManager(selector, ledger=ledger, reroute_engine=reroute_engine)

    backend_a = FakeManilaV2()
    backend_b = FakeSherbrooke()

    def patch_pending(backend, n):
        real_status = backend.status

        def patched():
            status = real_status()
            status.pending_jobs = n
            return status

        backend.status = patched

    patch_pending(backend_a, 100)
    patch_pending(backend_b, 0)

    pinned_job = FakeJob(status="QUEUED")
    ledger.track(pinned_job, "fake_manila", bell_pair(), score_at_submission=0.05, pinned=True)
    unpinned_job = FakeJob(status="QUEUED")
    ledger.track(unpinned_job, "fake_manila", bell_pair(), score_at_submission=0.05, pinned=False)
    time.sleep(0.02)

    def submit_fn(backend, circuit):
        return FakeJob(status="QUEUED")

    rerouted = manager.tick([backend_a, backend_b], submit_fn)
    assert len(rerouted) == 1
    assert rerouted[0].backend_name == "fake_sherbrooke"
    assert not pinned_job.cancelled  # left alone despite qualifying otherwise
    assert unpinned_job.cancelled
