import json
import time

import pytest

pytest.importorskip("rich")

from qiskit import QuantumCircuit

from qiskit_traffic_engineering.dashboard import render_backend_table, render_job_table, render_events, _log_line
from qiskit_traffic_engineering.ledger import JobLedger
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


def test_render_backend_table_without_circuit():
    backends = [FakeBackend("a", pending_jobs=3), FakeBackend("b", pending_jobs=0, operational=False)]
    table = render_backend_table(backends)
    assert table.row_count == 2
    assert len(table.columns) == 4  # no score column without a circuit


def test_render_backend_table_with_circuit_adds_score_column():
    backends = [FakeBackend("a", pending_jobs=3), FakeBackend("b", pending_jobs=0)]
    selector = BackendSelector(strategy=QueueOnlyScoring())
    table = render_backend_table(backends, circuit=bell_pair(), selector=selector)
    assert len(table.columns) == 5
    assert table.row_count == 2


def test_render_backend_table_marks_unfit_backend():
    too_small = FakeBackend("tiny", num_qubits=1, pending_jobs=0)
    selector = BackendSelector(strategy=QueueOnlyScoring())
    table = render_backend_table([too_small], circuit=bell_pair(), selector=selector)
    # Rendered cell text for the "unfit" backend's score column
    score_column = table.columns[-1]
    assert "unfit" in score_column._cells[0]


def test_render_job_table_orders_most_recent_first():
    ledger = JobLedger()
    older = ledger.track(FakeJob(status="QUEUED"), "backend_a", bell_pair(), 0.5)
    time.sleep(0.01)
    newer = ledger.track(FakeJob(status="RUNNING"), "backend_b", bell_pair(), 0.7)

    table = render_job_table(ledger)
    assert table.row_count == 2
    job_id_column = table.columns[0]
    assert job_id_column._cells[0] == newer.job_id[:16]
    assert job_id_column._cells[1] == older.job_id[:16]


def test_render_events_shows_placeholder_when_empty():
    panel = render_events([])
    assert "(no reroute activity yet)" in panel.renderable


def test_render_events_truncates_to_max():
    events = [f"event {i}" for i in range(20)]
    panel = render_events(events, max_events=3)
    assert "event 19" in panel.renderable
    assert "event 17" in panel.renderable
    assert "event 16" not in panel.renderable


def test_log_line_writes_jsonl_records(tmp_path):
    log_path = tmp_path / "run.jsonl"
    _log_line(log_path, {"jobs": [{"job_id": "abc", "status": "QUEUED"}], "events": []})
    _log_line(log_path, {"jobs": [{"job_id": "abc", "status": "RUNNING"}], "events": [{"type": "rerouted"}]})

    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["jobs"][0]["status"] == "QUEUED"
    assert second["jobs"][0]["status"] == "RUNNING"
    assert second["events"][0]["type"] == "rerouted"
    assert "timestamp" in first


def test_log_line_noop_when_path_is_none(tmp_path):
    # Should not raise, and should not create anything.
    _log_line(None, {"jobs": [], "events": []})
    assert list(tmp_path.iterdir()) == []
