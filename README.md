# qiskit-quantum-traffic-engineering

An intelligent traffic-management layer above IBM Quantum Runtime: queue-
and noise-aware backend selection, ISA-safe job submission with failover,
and SLA/outage-driven rerouting of jobs still waiting in queue.

## Where this fits — and where it doesn't

**Core Qiskit (`Qiskit/qiskit`) is hardware-agnostic by design.** It has no
concept of IBM backends, queues, or Runtime jobs — that's precisely why
`qiskit-ibm-runtime` exists as a separate, IBM-maintained package. This
project is scoped to sit **alongside `qiskit-ibm-runtime`**, as a candidate
for that ecosystem (or, more likely at first, an independent package that
demonstrates the idea) — not as a proposed change to core Qiskit. Framing
it as a "Qiskit SDK" contribution would be scoped wrong from the start.

The realistic path to actual IBM adoption is opening a design discussion
against `Qiskit/qiskit-ibm-runtime`'s issue tracker once this is solid,
not submitting a large unsolicited PR. Large new subsystems there get
discussed before they get merged.

## Design stance on rerouting

This is the part worth being careful about. An automated tool that cancels
a queued job and resubmits it elsewhere purely to jump to a shorter line
would look like **gaming IBM's fair-share scheduler** — and a tool built
that way actively hurts, rather than helps, any case for adoption.

So rerouting here is **not** "hunt for the shortest queue every tick."
`RerouteEngine` only reroutes a still-queued job when:

1. It's been queued longer than a **user-declared SLA** (`max_wait_seconds`)
   — enforcing your own stated patience, not opportunistically chasing
   every improvement — **or** the current backend has gone non-operational
   (a real outage/recalibration, not a queue-length judgment call).
2. A candidate backend beats the current backend's **live** score (not
   its score at original submission time — a backend that started great
   can degrade after submission) by more than `min_score_improvement`
   (skipped for outages — anything operational beats a dead backend).
   Small differences aren't worth the cost of re-queuing: you lose your
   position and go to the back of a new line.
3. The job hasn't already been rerouted `max_reroutes_per_job` times — a
   hard cap against thrashing a single circuit around the fleet.

Waiting is the default. Rerouting is the exception, and every reroute
records *why* it happened (`RerouteDecision.reason`).

## Hard constraints this respects

- **A job can only be cancelled while `QUEUED`/`INITIALIZING`.** Once
  `RUNNING`, there is no live-migration API — "rerouting" always means
  cancel-and-resubmit, never moving a running job.
- **Sessions and Batches pin to a single backend for their duration.**
  There's no cross-backend load-spreading within one session — this
  package decides *which* backend gets each submission and whether to
  re-decide while it's still waiting, not live-splitting one workload.

## Quick start

```python
from qiskit import QuantumCircuit
from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2 as Sampler
from qiskit_traffic_engineering import TrafficManager, BackendSelector, HybridScoring

service = QiskitRuntimeService()
selector = BackendSelector(service=service, strategy=HybridScoring())
manager = TrafficManager(selector)

qc = QuantumCircuit(2, 2)
qc.h(0)
qc.cx(0, 1)
qc.measure([0, 1], [0, 1])

def submit_fn(backend, circuit):
    return Sampler(mode=backend).run([circuit])

tracked = manager.submit(qc, submit_fn)
```

Monitor and reroute on whatever cadence you like — nothing here spins its
own thread:

```python
backends = service.backends(operational=True, simulator=False)
rerouted = manager.tick(backends, submit_fn)  # call this periodically
```

Or use the blocking convenience loop for a simple script:

```python
manager.run_forever(backends, submit_fn, interval_seconds=30)
```

## Pinning a job to a specific machine

If you want a circuit to run on a particular backend and never be moved
— even while using `tick()`/`run_forever()` for everything else — pass
`pin=True` (and typically `backends=[that_backend]` to also force initial
submission there):

```python
tracked = manager.submit(qc, submit_fn, backends=[ibm_fez], pin=True)
```

A pinned job still shows up normally in the ledger and dashboard (marked
with 📌), and its status is still tracked — it's just permanently excluded
from `RerouteEngine`'s decisions, checked before the SLA/outage/cap logic
runs at all. Other jobs submitted without `pin=True` in the same
`tick()`/`run_forever()` loop reroute normally.

## Live dashboard

An optional terminal dashboard shows backend telemetry, tracked jobs, and
reroute events, refreshing in place:

```bash
pip install ".[dashboard]"
python scripts/dashboard_demo.py            # offline demo, no credentials needed
python scripts/dashboard_demo.py --observe-only  # dry-run: shows what WOULD reroute, doesn't act
```

The demo script simulates one backend getting congested so you can see an
actual SLA-triggered reroute happen (backend queues in the fake-backend
test suite always report empty, so nothing would visibly happen otherwise).

For a real `QiskitRuntimeService`, use `scripts/live_dashboard.py` instead
of the offline demo:

```bash
python scripts/live_dashboard.py                        # realistic: 10-min SLA, observe-only
python scripts/live_dashboard.py --live-reroute          # realistic: 10-min SLA, REAL cancel+resubmit
python scripts/live_dashboard.py --sla-minutes 5 --live-reroute  # realistic: 5-min SLA
python scripts/live_dashboard.py --fast-sla              # manufactured: short SLA, observe-only
python scripts/live_dashboard.py --fast-sla --live-reroute  # manufactured: short SLA, REAL cancel+resubmit
python scripts/live_dashboard.py --self-congest 5 --live-reroute --sla-minutes 1
    # genuine (not manufactured) queue pressure: submits 5 real filler jobs
    # to one backend, then forces the tracked job onto that same backend.
    # Burns real queue slots (N+1 jobs). Unverified -- added but not yet run.
```

Every run writes a tick-by-tick JSONL log to `logs/live_run_<timestamp>.jsonl`
(gitignored — these are local run records, not something to commit).
Summarize one afterward:

```bash
python scripts/summarize_run_log.py logs/live_run_2026-09-04T120000.jsonl
```

This log is the actual evidence a run happened a certain way — a terminal
dashboard's on-screen state doesn't survive a disconnected Codespace or a
scrolled-away buffer, and "I watched it for a while and it looked fine"
isn't verification. The realistic runs (no `--fast-sla`) default to a
10-minute SLA (override with `--sla-minutes N`) and are meant to run
unattended for a while — that's what tells you how the reroute engine
behaves under actual queue conditions, as opposed to `--fast-sla`, which
manufactures an opportunity (short SLA, submits to the worst-ranked
backend on purpose) purely to confirm the mechanism works at all. Both
are useful; only the realistic ones are evidence of real-world behavior.

It defaults to observe-only because the reroute engine's cancel+resubmit
path hasn't been verified against a live service yet (see below) — it'll
show you what *would* reroute without touching anything real until you
pass `--live-reroute` deliberately. `--fast-sla` only shortens the wait
threshold (5s instead of the realistic default) to make decisions visible
in a short
session — it does not relax the score-improvement threshold or disable
the outage check, and shouldn't be left on for real workloads.

For embedding in your own code:

```python
from qiskit_traffic_engineering.dashboard import run_dashboard

run_dashboard(manager, backends, submit_fn, circuit=qc, refresh_seconds=5)
```

`run_dashboard` is a blocking loop (Ctrl+C to stop) — it's a convenience
driver for scripts and demos, not something to embed in a larger
application; call `manager.tick()` yourself from your own scheduler for
that, and use `render_backend_table`/`render_job_table`/`render_events`
directly if you want the same views inside something else.

## Scoring strategies

- **`QueueOnlyScoring`** — reported pending-job count.
- **`NoiseAwareScoring`** — transpiles to the candidate's real coupling map
  and basis gates, then estimates fidelity by multiplying per-gate and
  per-readout error rates from live calibration data.
- **`HybridScoring`** — weighted sum of the two (defaults 0.4/0.6).

All three exclude non-operational backends and backends the circuit
doesn't fit, and accept an optional `CalibrationCache` to avoid refetching
`status()`/`properties()` on every scoring call.

## Fidelity estimate: validation status

`tests/test_fidelity_validation.py` checks `NoiseAwareScoring`'s analytic
prediction against a Qiskit Aer noise-model simulation built from the same
calibration data (skipped automatically without the optional `qiskit-aer`
extra). Agreement is within ~0.02 across bell-pair/GHZ cases on fake
backends.

**Read this narrowly.** Aer's noise model shares the same independence
assumptions as the analytic estimate, so this shows the math is a
reasonable approximation *of that noise model*, not that the noise model
matches a real device on any given day — crosstalk, drift, and correlated
errors aren't in either one.

## What's been checked against real hardware

The scoring, ISA-transpilation, and submission path (the predecessor to
this package, `qiskit-quantum-loadbalancer`) has been run against a real
`QiskitRuntimeService` twice: the first run caught a genuine bug (Runtime
rejects non-ISA circuits since March 2024; the router was submitting the
raw circuit), the second confirmed the fix on `ibm_fez`.

The reroute engine has been fully verified live, end to end (2026-09-04).
First attempt: `RerouteEngine.evaluate()` correctly detected a real SLA
breach on `ibm_kingston` and picked a better candidate; `execute()` then
crashed on resubmission with a non-ISA-circuit error — the exact bug
class this package's predecessor had already hit and fixed in
`router.py`, just never carried over to `reroute.py`. That real job was
left cancelled with no replacement submitted. Fixed the missing
re-transpilation in `execute()`.

Second attempt, after the fix: a real queued job on `ibm_kingston`
breached its SLA, was cancelled, and was cleanly resubmitted to
`ibm_marrakesh` — which then ran to completion (`DONE`). Cancel,
re-transpile, resubmit, and track-through-to-completion are all confirmed
working against real `QiskitRuntimeService` jobs, not just fake ones.

Two subsequent realistic runs — 61 ticks over ~14.5 min (600s SLA) and
19 ticks over ~4.3 min (300s SLA via `--sla-minutes 5`) — both show the
same pattern: one job tracked, finished `DONE`, zero reroutes. Neither
job ever breached its SLA before completing. Checking the second run's
actual observed queue duration (`summarize_run_log.py`'s bracket report)
explains why: the job left `QUEUED` somewhere between 1 and 15 seconds
after submission. **Under current Open-plan load, any SLA measured in
minutes will essentially never fire under best-backend routing** — the
initially-chosen backend is already fast enough that there's rarely
anything to reroute away from. This isn't a verification gap to keep
re-running the same experiment against; it's an accurate characterization
of the current environment. No spurious rerouting either time, consistent
with the design intent that a shorter queue elsewhere is never sufficient
on its own to trigger a reroute; only a real SLA breach or backend outage
is (see `reroute.py`'s module docstring).

The practical implication: confirming the SLA-breach path fires correctly
under genuinely realistic (not manufactured) conditions would need either
heavier real queue contention than the Open plan currently has, a
paid-plan/busier account, or simply waiting for a moment of real demand
spikes. `scripts/live_dashboard.py --self-congest N` offers a middle
ground: it submits N real filler jobs to one backend first, creating
genuine (not monkeypatched) queue depth behind the one tracked job, then
forces that job onto the same now-congested backend so it actually has
something real to wait behind. This burns real queue slots (N+1 jobs) and
hasn't itself been run yet — added but unverified. The manufactured
`--fast-sla` run and the one directly-observed `ibm_kingston` breach
above remain the only positive confirmations that the SLA-breach path
itself works; see limitations below.

While adding test coverage for `max_reroutes_per_job` (a chained scenario:
reroute once, then again, then correctly refuse a third time at the cap),
a real bug in the improvement check surfaced: it compared a candidate's
score against the job's score *at original submission time*, not its
assigned backend's *current* score. A backend that started great could
never be "improved upon" later even after clearly degrading, since the
stale high benchmark was never updated. Fixed to compare against the
current backend's live score instead — caught by writing a test for a
different feature entirely, not by live use, which is itself worth
noting: this class of bug wouldn't show up in a short realistic run like
the one above, only in a longer one with genuine backend degradation
over time.

## Known limitations

- **SLA-breach path has one direct live confirmation (`ibm_kingston`,
  9/4) and one manufactured (`--fast-sla`) confirmation, but no
  confirmation yet under organically realistic conditions.** Two
  realistic-threshold runs both show jobs completing in under 15s of
  actual queue time — Open-plan load right now is too light to breach
  even a 5-minute SLA under best-backend routing. This is an environment
  constraint, not something fixable by re-running the same test; it would
  need heavier real contention (a different account tier, a demand spike,
  or deliberately submitting many concurrent jobs to build up genuine
  queue pressure) to actually exercise. No data yet on repeated reroutes
  hitting `max_reroutes_per_job`, on genuine outage-triggered rerouting,
  or on behavior under heavier real queue contention. A chained scenario
  (a job repeatedly getting unlucky, rerouted twice, then correctly
  refused a third time at the cap) is now covered in
  `tests/test_reroute.py::test_max_reroutes_per_job_stops_a_repeatedly_congested_job`
  — that test is also what caught the stale-baseline bug just above; it's
  fake-backend coverage, not live evidence of the cap firing correctly
  under real repeated degradation.
- **Reroute engine's actual cancel+resubmit path is verified live, but
  only once, on a fast-SLA test configuration.** The 2026-09-04
  verification used `--fast-sla`
  (5s SLA, worst-ranked backend chosen on purpose), not a realistic
  production run, and only exercised one reroute on one job. A proper
  unattended realistic-threshold run (real 600s SLA, real queue
  conditions, tracked via the automatic tick-by-tick JSONL log in
  `logs/`) hasn't been done yet — that's the next concrete verification
  step, not just "the code looks right."
  Real `RuntimeJobV2.cancel()` edge cases — races with the job starting,
  cancellation under heavier load, repeated reroutes on the same job
  hitting `max_reroutes_per_job` — remain unverified either way.
- **No persistence.** `JobLedger` is in-memory; a process restart loses
  tracking of in-flight jobs. Fine for a script or notebook, not for
  anything that needs to survive a restart.
- **SLA/threshold defaults are unvalidated.** `max_wait_seconds=600`,
  `min_score_improvement=0.15`, `max_reroutes_per_job=2` are reasonable
  starting points, not the result of empirical tuning against real queue
  behavior or job cost data.
- **`run_forever()` is a plain blocking loop**, not a real scheduler —
  fine for a demo script, not for production use where you'd want a
  proper async/cron-driven caller invoking `tick()` instead.
- **Dashboard is terminal-only, single-user, no persistence.** It reads
  the same in-memory `JobLedger` as everything else — closing the
  dashboard process loses the same state a restart would anyway (see the
  no-persistence limitation above). Not a multi-viewer or remote-access
  tool.
- **NoiseAwareScoring's independent-error assumption** (see validation
  section above) still applies here unchanged from the predecessor
  package.

## License

Apache-2.0
