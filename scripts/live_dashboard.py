"""Live dashboard against a real QiskitRuntimeService.

Defaults to --observe-only: the dashboard will show what WOULD reroute
(SLA breaches, outages) without actually cancelling or resubmitting
anything. This is deliberate -- the reroute engine's cancel+resubmit path
has not yet been verified against a live service (see README's
"What's been checked against real hardware" section). Don't pass
--live-reroute until you've verified that path yourself and are
comfortable with it cancelling real queued jobs.

Run (needs real IBM Quantum credentials configured):

    python scripts/live_dashboard.py                        # REALISTIC: 10-min SLA, observe-only
    python scripts/live_dashboard.py --live-reroute          # REALISTIC: 10-min SLA, REAL cancel+resubmit
    python scripts/live_dashboard.py --sla-minutes 5 --live-reroute  # REALISTIC: 5-min SLA
    python scripts/live_dashboard.py --fast-sla              # manufactured: short SLA, observe-only
    python scripts/live_dashboard.py --fast-sla --live-reroute  # manufactured: short SLA, REAL cancel+resubmit
    python scripts/live_dashboard.py --self-congest 5 --live-reroute --sla-minutes 1
        # genuine (not manufactured) queue pressure: submits 5 real filler
        # jobs to one backend first, so the tracked job has an actual real
        # queue behind them -- see --self-congest note below.

Every run writes a tick-by-tick JSONL log to logs/live_run_<timestamp>.jsonl
regardless of mode -- summarize it afterward with
scripts/summarize_run_log.py. This is the actual durable evidence of what
happened; a terminal dashboard's on-screen state doesn't survive a
disconnected Codespace or a scrolled-away buffer.

The realistic runs (no --fast-sla) default to a 10-minute SLA
(max_wait_seconds=600, min_score_improvement=0.15) and are meant to run
for a while unattended -- override the wait with --sla-minutes N if
10 minutes doesn't reflect your actual patience. This is what actually
tells you whether the reroute engine behaves sensibly under real queue
conditions, as opposed to the --fast-sla runs, which manufacture a
reroute opportunity (short SLA, worst-ranked backend) purely to confirm
the mechanism works at all. Both are useful; only the realistic ones are
evidence of real-world behavior.

--fast-sla exists purely to let you confirm the decision logic actually
fires against real backend data without waiting out a realistic 600s SLA.
It does NOT relax min_score_improvement to zero or disable the outage
check -- it only shortens the wait (to 5s, with a 2s refresh so the
dashboard has a chance to catch the job before it starts running). Don't
leave --fast-sla on for real workloads; a short SLA against small real
score differences will reroute far more often than you actually want.

--self-congest N submits N real filler jobs to the SAME backend as the
tracked job, before submitting the tracked job itself, so there's a
genuine queue behind it -- not monkeypatched status like --fast-sla, an
actual real queue on IBM's side. This is the only way to exercise the
SLA-breach path under organically real conditions when the account's
natural load is too light (as observed: jobs have been leaving QUEUED
within 1-15s under normal Open-plan conditions -- see README). This
BURNS REAL QUEUE SLOTS/QUOTA -- N+1 real jobs get submitted. Start small
(3-5). The filler jobs are never cancelled or tracked by the reroute
engine; they exist purely to create real contention for the one job that
is tracked.

Ctrl+C to stop.
"""

import sys
import time
from pathlib import Path

from qiskit import QuantumCircuit, transpile
from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2 as Sampler

from qiskit_traffic_engineering import TrafficManager, BackendSelector, HybridScoring
from qiskit_traffic_engineering.reroute import RerouteEngine
from qiskit_traffic_engineering.ledger import JobLedger
from qiskit_traffic_engineering.dashboard import run_dashboard


def bell_pair() -> QuantumCircuit:
    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure([0, 1], [0, 1])
    return qc


def main():
    live_reroute = "--live-reroute" in sys.argv
    fast_sla = "--fast-sla" in sys.argv

    sla_minutes = 10.0
    if "--sla-minutes" in sys.argv:
        idx = sys.argv.index("--sla-minutes")
        try:
            sla_minutes = float(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("--sla-minutes requires a numeric value, e.g. --sla-minutes 5")
            sys.exit(1)

    self_congest = 0
    if "--self-congest" in sys.argv:
        idx = sys.argv.index("--self-congest")
        try:
            self_congest = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("--self-congest requires an integer, e.g. --self-congest 5")
            sys.exit(1)
        if self_congest < 1:
            print("--self-congest must be at least 1")
            sys.exit(1)

    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / f"live_run_{time.strftime('%Y-%m-%dT%H%M%S')}.jsonl"
    print(f"Logging every tick to {log_path} -- this is the durable record of the run,")
    print("independent of whatever's still visible on screen when you stop watching.")
    print(f"Summarize it afterward with: python scripts/summarize_run_log.py {log_path}\n")

    print("Connecting to QiskitRuntimeService...")
    service = QiskitRuntimeService()
    backends = service.backends(operational=True, simulator=False)

    selector = BackendSelector(service=service, strategy=HybridScoring())
    ledger = JobLedger()

    if fast_sla:
        max_wait_seconds = 5.0
        min_score_improvement = 0.05
        print("--fast-sla: using max_wait_seconds=5, min_score_improvement=0.05 "
              "(verification only -- do not leave this on for real workloads). "
              "If your backend's queue is very short right now, even this may not "
              "catch the job before it starts RUNNING -- that's not a bug, it just "
              "means there was nothing to reroute.")
    else:
        # Default 10 minutes, overridable with --sla-minutes N -- tune to
        # reflect your actual patience for a real workload.
        max_wait_seconds = sla_minutes * 60.0
        min_score_improvement = 0.15
        print(f"Realistic mode: max_wait_seconds={max_wait_seconds:.0f} "
              f"({sla_minutes:g} min), min_score_improvement=0.15")

    reroute_engine = RerouteEngine(
        ledger, selector, max_wait_seconds=max_wait_seconds, min_score_improvement=min_score_improvement
    )
    manager = TrafficManager(selector, ledger=ledger, reroute_engine=reroute_engine)

    def submit_fn(backend, circuit):
        return Sampler(mode=backend).run([circuit])

    circuit = bell_pair()
    forced_backend = None

    if self_congest:
        print(f"\n--self-congest {self_congest}: submitting {self_congest} real filler "
              "jobs to create genuine queue pressure. This burns real queue slots.")
        ranked = selector.rank(circuit, backends=backends)
        if not ranked:
            print("No viable backend found for filler jobs -- skipping --self-congest.")
        else:
            target_backend = ranked[0].backend
            print(f"Filler target: {target_backend.name}")
            filler_jobs = []
            for i in range(self_congest):
                isa = transpile(circuit, backend=target_backend, optimization_level=1)
                job = submit_fn(target_backend, isa)
                filler_jobs.append(job)
                print(f"  filler {i + 1}/{self_congest}: {job.job_id()}")
            print(f"Submitted {len(filler_jobs)} filler jobs to {target_backend.name}. "
                  "These are never tracked or cancelled by the reroute engine.")
            # Force the tracked job onto the SAME (now congested) backend --
            # otherwise the normal best-backend selection would just notice
            # the real queue we built and avoid it, defeating the point.
            forced_backend = target_backend

    print("Submitting an initial job so there's something to track...")
    if fast_sla:
        # For verification, deliberately submit to the WORST-ranked backend
        # instead of the best -- that's the one most likely to actually have
        # a queue worth testing the reroute against. Never do this for real
        # workloads; it's only useful because we're trying to manufacture a
        # verification opportunity, not get a good result.
        ranked = selector.rank(circuit, backends=backends)
        if ranked:
            worst = ranked[-1].backend
            print(f"--fast-sla: submitting to {worst.name} (worst-ranked of "
                  f"{len(ranked)}) on purpose, to maximize the odds of a real queue.")
            tracked = manager.submit(circuit, submit_fn, backends=[worst])
        else:
            tracked = manager.submit(circuit, submit_fn, backends=backends)
    elif forced_backend is not None:
        tracked = manager.submit(circuit, submit_fn, backends=[forced_backend])
    else:
        tracked = manager.submit(circuit, submit_fn, backends=backends)
    print(f"Job {tracked.job_id} submitted to {tracked.backend_name}")

    mode = "LIVE RESUBMIT (real cancel+resubmit)" if live_reroute else "observe-only (dry run)"
    print(f"\nStarting dashboard in {mode} mode. Ctrl+C to stop.")
    if not live_reroute:
        print("Pass --live-reroute to actually act on reroute decisions.")

    refresh = 2.0 if fast_sla else 10.0
    try:
        run_dashboard(
            manager, backends, submit_fn, circuit=circuit, refresh_seconds=refresh,
            auto_reroute=live_reroute, log_path=log_path,
        )
    except KeyboardInterrupt:
        print("\n\n--- Stopped. Final ledger state ---")
        manager.ledger.refresh_statuses()
        for t in manager.ledger.all_jobs():
            superseded = f" -> superseded by {t.superseded_by}" if t.superseded_by else ""
            print(f"  {t.job_id}: backend={t.backend_name} status={t.status} "
                  f"reroutes={t.reroute_count}{superseded}")
        print("\nCross-check against your IBM Quantum dashboard (quantum.cloud.ibm.com) --")
        print("the cancelled job's ID should show status Cancelled there, and the new")
        print("job's ID should show up as a separate submission on its new backend.")
        print(f"\nFull tick-by-tick log: {log_path}")
        print(f"Summarize it with: python scripts/summarize_run_log.py {log_path}")


if __name__ == "__main__":
    main()
