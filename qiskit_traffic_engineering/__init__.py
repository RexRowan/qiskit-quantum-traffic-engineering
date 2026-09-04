"""qiskit-quantum-traffic-engineering

An intelligent traffic-management layer above IBM Quantum Runtime:
queue- and noise-aware backend selection, ISA-safe submission with
failover, and SLA/outage-driven rerouting of still-queued jobs.

See README.md for the design stance on rerouting (SLA/outage-only, not
opportunistic queue-hunting) and the honest scope of what's been
validated against real hardware vs. simulation.
"""

from .scoring import ScoringStrategy, QueueOnlyScoring, NoiseAwareScoring, HybridScoring
from .selector import BackendSelector, BackendScore
from .router import BackendRouter, RoutingError
from .monitor import CalibrationCache
from .ledger import JobLedger, TrackedJob
from .reroute import RerouteEngine, RerouteDecision
from .manager import TrafficManager

__version__ = "0.1.0"

__all__ = [
    "ScoringStrategy",
    "QueueOnlyScoring",
    "NoiseAwareScoring",
    "HybridScoring",
    "BackendSelector",
    "BackendScore",
    "BackendRouter",
    "RoutingError",
    "CalibrationCache",
    "JobLedger",
    "TrackedJob",
    "RerouteEngine",
    "RerouteDecision",
    "TrafficManager",
]
