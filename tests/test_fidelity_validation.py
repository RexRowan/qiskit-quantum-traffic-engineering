"""Regression test: NoiseAwareScoring's analytic estimate vs Aer noise-model simulation.

Skipped automatically if qiskit-aer isn't installed (optional 'validate'
extra). See README.md's "Fidelity estimate: validation status" section for
what this does and doesn't prove -- in short: this checks the analytic
estimate against a simulation built from the *same* calibration data it
reads, so it's a self-consistency check on the math, not evidence the
underlying noise model matches real hardware.
"""

import math

import pytest
from qiskit import QuantumCircuit, transpile

pytest.importorskip("qiskit_aer")
from qiskit_aer import AerSimulator  # noqa: E402

from qiskit_ibm_runtime.fake_provider import FakeManilaV2  # noqa: E402
from qiskit_traffic_engineering.scoring import NoiseAwareScoring  # noqa: E402

SHOTS = 8000
TOLERANCE = 0.05


def bell_pair() -> QuantumCircuit:
    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure([0, 1], [0, 1])
    return qc


def _hellinger_fidelity(p: dict, q: dict) -> float:
    keys = set(p) | set(q)
    s = sum(math.sqrt(p.get(k, 0.0) * q.get(k, 0.0)) for k in keys)
    return s * s


def test_analytic_estimate_tracks_noise_model_simulation():
    backend = FakeManilaV2()
    circuit = bell_pair()
    transpiled = transpile(circuit, backend=backend, optimization_level=1)

    predicted = NoiseAwareScoring().score(backend, circuit)

    ideal_counts = AerSimulator().run(transpiled, shots=SHOTS).result().get_counts()
    ideal_probs = {k: v / SHOTS for k, v in ideal_counts.items()}

    noisy_counts = AerSimulator.from_backend(backend).run(
        transpiled, shots=SHOTS
    ).result().get_counts()
    noisy_probs = {k: v / SHOTS for k, v in noisy_counts.items()}

    empirical = _hellinger_fidelity(ideal_probs, noisy_probs)

    assert abs(predicted - empirical) < TOLERANCE, (
        f"analytic estimate {predicted:.4f} diverged from simulated "
        f"fidelity {empirical:.4f} by more than {TOLERANCE}"
    )
