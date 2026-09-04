"""A minimal backend stand-in for reroute-policy tests: just enough surface
for QueueOnlyScoring (name, num_qubits, status().pending_jobs/.operational).
Deliberately not a full fake_provider backend -- these tests are about
RerouteEngine's decision policy, not scoring accuracy.
"""

from dataclasses import dataclass


@dataclass
class FakeStatus:
    pending_jobs: int = 0
    operational: bool = True


class FakeBackend:
    def __init__(self, name: str, num_qubits: int = 5, pending_jobs: int = 0, operational: bool = True):
        self.name = name
        self.num_qubits = num_qubits
        self._status = FakeStatus(pending_jobs=pending_jobs, operational=operational)

    def status(self):
        return self._status

    def set_pending_jobs(self, n: int):
        self._status.pending_jobs = n

    def set_operational(self, value: bool):
        self._status.operational = value

    def configuration(self):
        raise NotImplementedError("FakeBackend exposes num_qubits directly")
