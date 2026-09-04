import time

from qiskit import QuantumCircuit
from qiskit_ibm_runtime.fake_provider import FakeManilaV2, FakeSherbrooke, FakeKyiv

from qiskit_traffic_engineering.ledger import JobLedger
from qiskit_traffic_engineering.reroute import RerouteEngine
from qiskit_traffic_engineering.scoring import QueueOnlyScoring
from qiskit_traffic_engineering.selector import BackendSelector

from fake_backend import FakeBackend
from fake_job import FakeJob


def bell_pair():
    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure([0, 1], [0, 1])
    return qc


def patch_status(backend, pending_jobs=0, operational=True):
    """Monkeypatch a real fake_provider backend's status() to report
    controllable telemetry, so scoring behaves predictably in tests."""
    real_status = backend.status

    def patched():
        status = real_status()
        status.pending_jobs = pending_jobs
        status.operational = operational
        return status

    backend.status = patched
    return backend


def make_engine(max_wait_seconds=600.0, min_score_improvement=0.15, max_reroutes_per_job=2):
    ledger = JobLedger()
    selector = BackendSelector(strategy=QueueOnlyScoring())
    engine = RerouteEngine(
        ledger,
        selector,
        max_wait_seconds=max_wait_seconds,
        min_score_improvement=min_score_improvement,
        max_reroutes_per_job=max_reroutes_per_job,
    )
    return ledger, engine


def test_no_reroute_when_within_sla_and_operational():
    ledger, engine = make_engine(max_wait_seconds=600.0)
    job = FakeJob(status="QUEUED")
    tracked = ledger.track(job, "backend_a", bell_pair(), score_at_submission=0.1)

    backend_a = FakeBackend("backend_a", pending_jobs=100)  # busy but operational
    backend_b = FakeBackend("backend_b", pending_jobs=0)  # much better, but SLA not breached

    decision = engine.evaluate(tracked, [backend_a, backend_b])
    assert decision is None


def test_reroute_on_sla_breach_with_sufficient_improvement():
    ledger, engine = make_engine(max_wait_seconds=0.01, min_score_improvement=0.1)
    job = FakeJob(status="QUEUED")
    tracked = ledger.track(job, "backend_a", bell_pair(), score_at_submission=0.05)
    time.sleep(0.02)  # breach the tiny SLA

    backend_a = FakeBackend("backend_a", pending_jobs=100)
    backend_b = FakeBackend("backend_b", pending_jobs=0)

    decision = engine.evaluate(tracked, [backend_a, backend_b])
    assert decision is not None
    assert decision.new_backend_name == "backend_b"
    assert "SLA breached" in decision.reason


def test_no_reroute_when_sla_breached_but_improvement_too_small():
    ledger, engine = make_engine(max_wait_seconds=0.01, min_score_improvement=0.9)
    job = FakeJob(status="QUEUED")
    tracked = ledger.track(job, "backend_a", bell_pair(), score_at_submission=0.5)
    time.sleep(0.02)

    backend_a = FakeBackend("backend_a", pending_jobs=1)
    backend_b = FakeBackend("backend_b", pending_jobs=0)  # only a marginal improvement

    decision = engine.evaluate(tracked, [backend_a, backend_b])
    assert decision is None


def test_reroute_on_outage_regardless_of_improvement_threshold():
    ledger, engine = make_engine(max_wait_seconds=600.0, min_score_improvement=0.99)
    job = FakeJob(status="QUEUED")
    tracked = ledger.track(job, "backend_a", bell_pair(), score_at_submission=0.9)

    backend_a = FakeBackend("backend_a", pending_jobs=0, operational=False)  # outage
    backend_b = FakeBackend("backend_b", pending_jobs=5)  # objectively worse queue-wise

    decision = engine.evaluate(tracked, [backend_a, backend_b])
    assert decision is not None
    assert decision.reason == "backend outage"


def test_no_reroute_beyond_max_reroutes_cap():
    ledger, engine = make_engine(max_wait_seconds=0.01, max_reroutes_per_job=0)
    job = FakeJob(status="QUEUED")
    tracked = ledger.track(job, "backend_a", bell_pair(), score_at_submission=0.05)
    time.sleep(0.02)

    backend_a = FakeBackend("backend_a", pending_jobs=100)
    backend_b = FakeBackend("backend_b", pending_jobs=0)

    decision = engine.evaluate(tracked, [backend_a, backend_b])
    assert decision is None  # already at the cap (0 allowed reroutes)


def test_pinned_job_never_reroutes_even_when_everything_else_qualifies():
    # SLA breached, huge improvement available, cap nowhere near hit --
    # every other condition says "reroute this." Pinning must override all
    # of it, since it's checked first in evaluate().
    ledger, engine = make_engine(max_wait_seconds=0.01, min_score_improvement=0.05, max_reroutes_per_job=5)
    job = FakeJob(status="QUEUED")
    tracked = ledger.track(job, "backend_a", bell_pair(), score_at_submission=0.0, pinned=True)
    time.sleep(0.02)

    backend_a = FakeBackend("backend_a", pending_jobs=1000, operational=True)
    backend_b = FakeBackend("backend_b", pending_jobs=0)

    decision = engine.evaluate(tracked, [backend_a, backend_b])
    assert decision is None


def test_pinned_job_not_rerouted_even_on_outage():
    # Even a real outage shouldn't move a pinned job -- pin means pin.
    ledger, engine = make_engine(max_wait_seconds=600.0)
    job = FakeJob(status="QUEUED")
    tracked = ledger.track(job, "backend_a", bell_pair(), score_at_submission=0.9, pinned=True)

    backend_a = FakeBackend("backend_a", pending_jobs=0, operational=False)  # outage
    backend_b = FakeBackend("backend_b", pending_jobs=0)

    decision = engine.evaluate(tracked, [backend_a, backend_b])
    assert decision is None


def test_execute_cancels_old_and_tracks_new():
    ledger, engine = make_engine(max_wait_seconds=0.01)
    old_job = FakeJob(status="QUEUED")
    tracked = ledger.track(old_job, "fake_manila", bell_pair(), score_at_submission=0.05)
    time.sleep(0.02)

    backend_a = patch_status(FakeManilaV2(), pending_jobs=100)
    backend_b = patch_status(FakeSherbrooke(), pending_jobs=0)
    decision = engine.evaluate(tracked, [backend_a, backend_b])
    assert decision is not None

    new_job = FakeJob(status="QUEUED")
    submitted = {}

    def submit_fn(backend, circuit):
        submitted["backend"] = backend
        submitted["circuit"] = circuit
        return new_job

    new_tracked = engine.execute(decision, submit_fn)

    assert old_job.cancelled
    assert tracked.status == "CANCELLED"
    assert tracked.superseded_by == new_tracked.job_id
    assert new_tracked.backend_name == "fake_sherbrooke"
    assert new_tracked.reroute_count == 1
    assert submitted["backend"] is backend_b
    # Regression check: the circuit submit_fn receives must be re-transpiled
    # for the NEW backend's ISA, not the raw circuit (this crashed against a
    # real IBM backend before this was fixed -- IBMInputValueError on 'h').
    used = {instr.operation.name for instr in submitted["circuit"].data}
    assert used <= set(backend_b.target.operation_names)


def test_execute_leaves_job_alone_if_cancel_fails():
    ledger, engine = make_engine(max_wait_seconds=0.01)
    old_job = FakeJob(status="QUEUED", cancel_should_fail=True)
    tracked = ledger.track(old_job, "fake_manila", bell_pair(), score_at_submission=0.05)
    time.sleep(0.02)

    backend_a = patch_status(FakeManilaV2(), pending_jobs=100)
    backend_b = patch_status(FakeSherbrooke(), pending_jobs=0)
    decision = engine.evaluate(tracked, [backend_a, backend_b])
    assert decision is not None

    calls = []

    def submit_fn(backend, circuit):
        calls.append(backend)
        return FakeJob(status="QUEUED")

    result = engine.execute(decision, submit_fn)
    assert result is tracked  # unchanged -- cancel failed, nothing submitted
    assert calls == []


def test_tick_reroutes_qualifying_jobs_end_to_end():
    ledger, engine = make_engine(max_wait_seconds=0.01, min_score_improvement=0.1)
    old_job = FakeJob(status="QUEUED")
    tracked = ledger.track(old_job, "fake_manila", bell_pair(), score_at_submission=0.05)
    time.sleep(0.02)

    backend_a = patch_status(FakeManilaV2(), pending_jobs=100)
    backend_b = patch_status(FakeSherbrooke(), pending_jobs=0)

    def submit_fn(backend, circuit):
        return FakeJob(status="QUEUED")

    rerouted = engine.tick([backend_a, backend_b], submit_fn)
    assert len(rerouted) == 1
    assert rerouted[0].backend_name == "fake_sherbrooke"


def test_max_reroutes_per_job_stops_a_repeatedly_congested_job():
    """Exercises the cap across a real sequence of reroutes, not just the
    boundary check in isolation: a job that keeps getting unlucky (its
    current backend keeps becoming the worst one) should reroute up to
    max_reroutes_per_job times and then stop, staying put even though a
    qualifying improvement is still available."""
    ledger, engine = make_engine(max_wait_seconds=0.01, min_score_improvement=0.1, max_reroutes_per_job=2)

    backend_a = patch_status(FakeManilaV2(), pending_jobs=100)
    backend_b = patch_status(FakeSherbrooke(), pending_jobs=0)
    backend_c = patch_status(FakeKyiv(), pending_jobs=50)
    backends = [backend_a, backend_b, backend_c]

    def submit_fn(backend, circuit):
        return FakeJob(status="QUEUED")

    job = FakeJob(status="QUEUED")
    tracked = ledger.track(job, "fake_manila", bell_pair(), score_at_submission=0.0)
    time.sleep(0.02)

    # Reroute 1: backend_a (congested) -> backend_b (best: pending=0)
    rerouted = engine.tick(backends, submit_fn)
    assert len(rerouted) == 1
    current = rerouted[0]
    assert current.backend_name == "fake_sherbrooke"
    assert current.reroute_count == 1

    # Now make backend_b the worst, backend_c the best -- another
    # qualifying opportunity.
    patch_status(backend_b, pending_jobs=100)
    patch_status(backend_c, pending_jobs=0)
    time.sleep(0.02)

    # Reroute 2: backend_b -> backend_c. Hits the cap (max_reroutes_per_job=2).
    rerouted = engine.tick(backends, submit_fn)
    assert len(rerouted) == 1
    current = rerouted[0]
    assert current.backend_name == "fake_kyiv"
    assert current.reroute_count == 2

    # Make backend_c the worst again, backend_a now best -- a THIRD
    # qualifying opportunity. Should be refused: at the cap.
    patch_status(backend_c, pending_jobs=100)
    patch_status(backend_a, pending_jobs=0)
    time.sleep(0.02)

    rerouted = engine.tick(backends, submit_fn)
    assert rerouted == []  # capped -- stays on fake_kyiv despite the opportunity

    ledger.refresh_statuses()
    final = ledger.get(current.job_id)
    assert final.status == "QUEUED"
    assert final.backend_name == "fake_kyiv"
    assert final.reroute_count == 2
