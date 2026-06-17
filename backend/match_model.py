"""Rating difference -> goal expectancies -> score/outcome probabilities.

lambda(d) = exp(a + b * d/100) fit by Poisson regression on history (see
scripts/calibrate.py). Dixon-Coles low-score correction with fitted rho.
"""
from __future__ import annotations

import json
import math

import numpy as np

from .config import SEED

MAX_GOALS = 10
D_CAP = 600.0

_params = None


def params() -> dict:
    global _params
    if _params is None:
        _params = json.loads((SEED / "model_params.json").read_text())
    return _params


def lambdas(d_eff: float) -> tuple[float, float]:
    """Expected goals (for, against) given effective rating diff."""
    p = params()
    d = max(-D_CAP, min(D_CAP, d_eff)) / 100.0
    return math.exp(p["a"] + p["b"] * d), math.exp(p["a"] - p["b"] * d)


def score_matrix(lh: float, la: float) -> np.ndarray:
    g = np.arange(MAX_GOALS + 1)
    ph = np.exp(-lh) * lh ** g / np.array([math.factorial(int(i)) for i in g])
    pa = np.exp(-la) * la ** g / np.array([math.factorial(int(i)) for i in g])
    m = np.outer(ph, pa)
    rho = params().get("rho", 0.0)
    # Dixon-Coles adjustment on 0/1 scores
    m[0, 0] *= 1 - lh * la * rho
    m[0, 1] *= 1 + lh * rho
    m[1, 0] *= 1 + la * rho
    m[1, 1] *= 1 - rho
    return m / m.sum()


def outcome_probs(d_eff: float) -> dict:
    """Analytic 1X2 + common side markets for a single match."""
    lh, la = lambdas(d_eff)
    m = score_matrix(lh, la)
    home = float(np.tril(m, -1).sum())
    away = float(np.triu(m, 1).sum())
    draw = float(np.trace(m))
    g = np.add.outer(np.arange(MAX_GOALS + 1), np.arange(MAX_GOALS + 1))
    return {
        "home": home, "draw": draw, "away": away,
        "exp_goals_home": lh, "exp_goals_away": la,
        "over_2_5": float(m[g > 2.5].sum()),
        "btts": float(m[1:, 1:].sum()),
    }
