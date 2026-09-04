"""Live terminal dashboard for TrafficManager.

Requires the optional `dashboard` extra: `pip install ".[dashboard]"`.
Not imported by anything else in the package -- if `rich` isn't installed,
nothing else here breaks, only this module.

The render_* functions are pure (backends/ledger in, a rich renderable
out) and unit-tested directly. `run_dashboard` is the interactive loop --
like `TrafficManager.run_forever`, it's a convenience driver, not
something to unit test by actually running it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional, Sequence, Union

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .ledger import JobLedger
from .manager import TrafficManager

_STATUS_STYLE = {
    "DONE": "green",
    "ERROR": "red",
    "CANCELLED": "yellow",
    "RUNNING": "cyan",
    "QUEUED": "white",
    "INITIALIZING": "white",
}


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def render_backend_table(backends: Sequence, circuit=None, selector=None) -> Table:
    """One row per backend: telemetry, plus a score column if a circuit and
    selector are supplied (scores are inherently circuit-specific, so this
    is optional)."""
    table = Table(title="Backends", expand=True)
    table.add_column("Backend")
    table.add_column("Qubits", justify="right")
    table.add_column("Pending", justify="right")
    table.add_column("Operational")

    ranked_scores = {}
    if circuit is not None and selector is not None:
        table.add_column("Score", justify="right")
        ranked_scores = {r.backend_name: r.score for r in selector.rank(circuit, backends=backends)}

    for backend in backends:
        try:
            status = backend.status()
            pending = str(status.pending_jobs)
            operational = "[green]yes[/]" if status.operational else "[red]NO[/]"
        except Exception:
            pending, operational = "?", "?"
        num_qubits = getattr(backend, "num_qubits", "?")
        row = [backend.name, str(num_qubits), pending, operational]
        if circuit is not None and selector is not None:
            score = ranked_scores.get(backend.name)
            row.append(f"{score:.4f}" if score is not None else "unfit")
        table.add_row(*row)
    return table


def render_job_table(ledger: JobLedger) -> Table:
    """One row per tracked job, most recent first."""
    table = Table(title="Tracked Jobs", expand=True)
    table.add_column("Job ID")
    table.add_column("Backend")
    table.add_column("Status")
    table.add_column("Reroutes", justify="right")
    table.add_column("Waited", justify="right")

    now = time.monotonic()
    for tracked in sorted(ledger.all_jobs(), key=lambda j: j.submitted_at, reverse=True):
        style = _STATUS_STYLE.get(tracked.status, "white")
        backend_label = f"📌 {tracked.backend_name}" if tracked.pinned else tracked.backend_name
        table.add_row(
            tracked.job_id[:16],
            backend_label,
            f"[{style}]{tracked.status}[/]",
            str(tracked.reroute_count),
            _format_elapsed(now - tracked.submitted_at),
        )
    return table


def render_events(events: List[str], max_events: int = 8) -> Panel:
    body = "\n".join(events[-max_events:]) if events else "(no reroute activity yet)"
    return Panel(body, title="Recent reroute events")


def _log_line(log_path: Optional[Path], record: dict) -> None:
    if log_path is None:
        return
    record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def run_dashboard(
    manager: TrafficManager,
    backends: Sequence,
    submit_fn,
    circuit=None,
    refresh_seconds: float = 5.0,
    auto_reroute: bool = True,
    log_path: Optional[Union[str, Path]] = None,
):
    """Blocking live dashboard. Ctrl+C to stop.

    `auto_reroute=True` (default) calls `manager.tick()` each refresh --
    this performs REAL cancel+resubmit against real queued jobs if you're
    pointed at a live service. Set `auto_reroute=False` to watch what
    *would* reroute (a dry-run evaluate pass, no cancellation, no
    submission) without acting on it.

    `log_path`, if given, appends a JSONL record every tick (job statuses,
    any reroute events) to that file. The terminal display doesn't
    survive a disconnected session or a scrolled-away buffer -- for a
    realistic long-running verification, the log file is the actual
    evidence, not what happened to still be on screen when you looked.
    """
    console = Console()
    events: List[str] = []
    log_path = Path(log_path) if log_path is not None else None

    with Live(console=console, refresh_per_second=1, screen=False) as live:
        while True:
            manager.ledger.refresh_statuses()
            tick_events = []

            if auto_reroute:
                rerouted = manager.tick(backends, submit_fn)
                for tracked in rerouted:
                    msg = f"rerouted -> {tracked.backend_name} (reroute #{tracked.reroute_count})"
                    events.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
                    tick_events.append({"type": "rerouted", "job_id": tracked.job_id,
                                         "backend": tracked.backend_name, "reroute_count": tracked.reroute_count})
            else:
                for tracked in manager.ledger.queued_jobs():
                    decision = manager.reroute_engine.evaluate(tracked, backends)
                    if decision is not None:
                        msg = f"WOULD reroute {tracked.job_id[:12]} -> {decision.new_backend_name} ({decision.reason})"
                        events.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
                        tick_events.append({"type": "would_reroute", "job_id": tracked.job_id,
                                             "new_backend": decision.new_backend_name, "reason": decision.reason})

            jobs_snapshot = [
                {
                    "job_id": t.job_id,
                    "backend": t.backend_name,
                    "status": t.status,
                    "reroute_count": t.reroute_count,
                    "waited_seconds": round(time.monotonic() - t.submitted_at, 1),
                }
                for t in manager.ledger.all_jobs()
            ]
            _log_line(log_path, {"jobs": jobs_snapshot, "events": tick_events})

            group = Table.grid(expand=True)
            group.add_row(render_backend_table(backends, circuit=circuit, selector=manager.selector))
            group.add_row(render_job_table(manager.ledger))
            group.add_row(render_events(events))
            live.update(group)

            time.sleep(refresh_seconds)
