"""Rating difference -> goal expectancies -> score/outcome probabilities.

lambda(d) = exp(a + b * d/100) fit by Poisson regression on history (see
scripts/calibrate.py). Dixon-Coles low-score correction with fitted rho.
"""
from __future__ import annotations

import json
import math

import numpy as np

from .config import SEED, STYLE_MULT_CAP, STYLE_WEIGHT

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


def style_multipliers(style_h: tuple[float, float] | None,
                      style_a: tuple[float, float] | None) -> tuple[float, float]:
    """Per-team goal multipliers from style residuals. A team's expected goals
    rise with ITS attack residual and the OPPONENT's defense residual (their
    leaky/solid identity), scaled by STYLE_WEIGHT and capped. (atk, def) come
    from EloState.style(); None -> neutral 1.0."""
    if not style_h or not style_a:
        return 1.0, 1.0
    ah, dh = style_h
    aa, da = style_a
    mh = max(-STYLE_MULT_CAP, min(STYLE_MULT_CAP, STYLE_WEIGHT * (ah + da)))
    ma = max(-STYLE_MULT_CAP, min(STYLE_MULT_CAP, STYLE_WEIGHT * (aa + dh)))
    return math.exp(mh), math.exp(ma)


def score_matrix(lh: float, la: float, draw_boost: float | None = None) -> np.ndarray:
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
    # draw inflation: Poisson under-predicts draws; nudge the diagonal (calibrated
    # on the 4,448-game backtest, see scripts/tune_draw.py)
    db = params().get("draw_boost", 0.0) if draw_boost is None else draw_boost
    if db:
        idx = np.arange(m.shape[0])
        m[idx, idx] *= 1 + db
    return m / m.sum()


def outcome_probs(d_eff: float, draw_boost: float | None = None,
                  goals_mult: float = 1.0,
                  style_mult: tuple[float, float] = (1.0, 1.0)) -> dict:
    """Analytic 1X2 + common side markets for a single match.
    goals_mult opens/closes the game symmetrically (urgency → more shots/goals);
    style_mult = (home, away) tilts each side's goals by the style matchup."""
    lh, la = lambdas(d_eff)
    lh, la = lh * goals_mult * style_mult[0], la * goals_mult * style_mult[1]
    m = score_matrix(lh, la, draw_boost)
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
