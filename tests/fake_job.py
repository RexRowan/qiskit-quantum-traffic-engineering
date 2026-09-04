"""A minimal stand-in for qiskit_ibm_runtime's RuntimeJobV2, controllable
in tests: status() returns whatever you set, cancel() can be made to fail
to simulate a job that started running before cancellation landed.
"""

import itertools

_id_counter = itertools.count()


class FakeJob:
    def __init__(self, status: str = "QUEUED", cancel_should_fail: bool = False):
        self._job_id = f"fakejob{next(_id_counter)}"
        self._status = status
        self.cancel_should_fail = cancel_should_fail
        self.cancelled = False

    def job_id(self):
        return self._job_id

    def status(self):
        return self._status

    def set_status(self, status: str):
        self._status = status

    def cancel(self):
        if self.cancel_should_fail:
            raise RuntimeError("cannot cancel: job already running")
        self.cancelled = True
        self._status = "CANCELLED"
