from qiskit import QuantumCircuit

from qiskit_traffic_engineering.ledger import JobLedger

from fake_job import FakeJob


def bell_pair():
    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure([0, 1], [0, 1])
    return qc


def test_track_and_get():
    ledger = JobLedger()
    job = FakeJob(status="QUEUED")
    tracked = ledger.track(job, "ibm_fez", bell_pair(), score_at_submission=0.7)
    assert tracked.job_id == job.job_id()
    assert ledger.get(job.job_id()) is tracked


def test_track_seeds_status_from_job_not_a_hardcoded_default():
    ledger = JobLedger()
    job = FakeJob(status="RUNNING")  # already running by the time it's tracked
    tracked = ledger.track(job, "ibm_fez", bell_pair(), 0.7)
    assert tracked.status == "RUNNING"


def test_track_defaults_to_unpinned():
    ledger = JobLedger()
    tracked = ledger.track(FakeJob(status="QUEUED"), "ibm_fez", bell_pair(), 0.7)
    assert tracked.pinned is False


def test_track_can_be_pinned():
    ledger = JobLedger()
    tracked = ledger.track(FakeJob(status="QUEUED"), "ibm_fez", bell_pair(), 0.7, pinned=True)
    assert tracked.pinned is True


def test_refresh_statuses_updates_from_job():
    ledger = JobLedger()
    job = FakeJob(status="QUEUED")
    tracked = ledger.track(job, "ibm_fez", bell_pair(), 0.7)
    job.set_status("RUNNING")
    ledger.refresh_statuses()
    assert tracked.status == "RUNNING"


def test_refresh_statuses_skips_terminal_jobs():
    ledger = JobLedger()
    job = FakeJob(status="DONE")
    tracked = ledger.track(job, "ibm_fez", bell_pair(), 0.7)
    tracked.status = "DONE"
    job.set_status("QUEUED")  # should never be re-read
    ledger.refresh_statuses()
    assert tracked.status == "DONE"


def test_queued_jobs_filters_correctly():
    ledger = JobLedger()
    queued_job = FakeJob(status="QUEUED")
    running_job = FakeJob(status="RUNNING")
    ledger.track(queued_job, "ibm_fez", bell_pair(), 0.7)
    ledger.track(running_job, "ibm_fez", bell_pair(), 0.7)
    ledger.refresh_statuses()
    queued = ledger.queued_jobs()
    assert len(queued) == 1
    assert queued[0].job_id == queued_job.job_id()


def test_refresh_status_check_failure_keeps_last_known_status():
    ledger = JobLedger()
    job = FakeJob(status="QUEUED")
    tracked = ledger.track(job, "ibm_fez", bell_pair(), 0.7)

    def raising_status():
        raise RuntimeError("transient network error")

    job.status = raising_status
    ledger.refresh_statuses()
    assert tracked.status == "QUEUED"  # unchanged, not crashed


def test_mark_superseded():
    ledger = JobLedger()
    old_job = FakeJob(status="QUEUED")
    new_job = FakeJob(status="QUEUED")
    old_tracked = ledger.track(old_job, "ibm_fez", bell_pair(), 0.7)
    new_tracked = ledger.track(new_job, "ibm_kingston", bell_pair(), 0.9)
    ledger.mark_superseded(old_tracked.job_id, new_tracked.job_id)
    assert old_tracked.status == "CANCELLED"
    assert old_tracked.superseded_by == new_tracked.job_id
