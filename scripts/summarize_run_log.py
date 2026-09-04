"""Summarize a dashboard run log (JSONL, from live_dashboard.py's --log
option) into a human-readable report.

Run:
    python scripts/summarize_run_log.py logs/live_run_2026-09-04T120000.jsonl
"""

import json
import sys
from collections import defaultdict


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/summarize_run_log.py <log_file.jsonl>")
        sys.exit(1)

    path = sys.argv[1]
    ticks = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                ticks.append(json.loads(line))

    if not ticks:
        print("Log file is empty.")
        return

    start = ticks[0]["timestamp"]
    end = ticks[-1]["timestamp"]

    all_job_ids = set()
    reroute_events = []
    would_reroute_events = []
    final_status_by_job = {}
    # Per job: last tick's waited_seconds while still QUEUED/INITIALIZING,
    # and the first waited_seconds once it left that state -- brackets the
    # real queue-to-running transition, so you know what SLA would
    # actually have caught it.
    last_queued_wait = {}
    first_non_queued_wait = {}

    for tick in ticks:
        for job in tick["jobs"]:
            job_id = job["job_id"]
            all_job_ids.add(job_id)
            final_status_by_job[job_id] = job["status"]
            if job["status"] in ("QUEUED", "INITIALIZING"):
                last_queued_wait[job_id] = job["waited_seconds"]
            elif job_id not in first_non_queued_wait:
                first_non_queued_wait[job_id] = job["waited_seconds"]
        for event in tick.get("events", []):
            if event["type"] == "rerouted":
                reroute_events.append((tick["timestamp"], event))
            elif event["type"] == "would_reroute":
                would_reroute_events.append((tick["timestamp"], event))

    status_counts = defaultdict(int)
    for status in final_status_by_job.values():
        status_counts[status] += 1

    print(f"Run: {start} -> {end}  ({len(ticks)} ticks logged)")
    print(f"Jobs tracked over the run: {len(all_job_ids)}")
    print(f"Final status breakdown: {dict(status_counts)}")
    print()

    if reroute_events:
        print(f"Reroutes executed: {len(reroute_events)}")
        for ts, event in reroute_events:
            print(f"  [{ts}] {event['job_id']} -> {event['backend']} (reroute #{event['reroute_count']})")
    else:
        print("Reroutes executed: 0")

    if would_reroute_events:
        print(f"\nDry-run 'would reroute' decisions: {len(would_reroute_events)}")
        for ts, event in would_reroute_events:
            print(f"  [{ts}] {event['job_id']} -> {event['new_backend']} ({event['reason']})")

    print("\nObserved queue duration per job (brackets when it left QUEUED/INITIALIZING;")
    print("narrower brackets need more frequent ticks -- these are only as precise as refresh_seconds):")
    for job_id in sorted(all_job_ids):
        lo = last_queued_wait.get(job_id)
        hi = first_non_queued_wait.get(job_id)
        if lo is None and hi is None:
            continue
        if hi is None:
            print(f"  {job_id}: still QUEUED/INITIALIZING at end of run (>= {lo:.0f}s and counting)")
        elif lo is None:
            print(f"  {job_id}: already past QUEUED by first tick (< {hi:.0f}s)")
        else:
            print(f"  {job_id}: left QUEUED somewhere between {lo:.0f}s and {hi:.0f}s after submission")


if __name__ == "__main__":
    main()
