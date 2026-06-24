"""Fit the goal model on history and write data/seed/model_params.json.

Poisson GLM  goals ~ exp(a + b*d/100)  via Newton-Raphson, then Dixon-Coles
rho by grid search on 1X2 log-loss over 2015+ matches.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.config import SEED
from backend.elo import calibration_samples


def fit_poisson(d: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    X = np.column_stack([np.ones_like(d), d / 100.0])
    beta = np.array([np.log(max(y.mean(), 0.1)), 0.1])
    for _ in range(50):
        eta = X @ beta
        mu = np.exp(np.clip(eta, -10, 5))
        grad = X.T @ (y - mu)
        H = X.T @ (X * mu[:, None])
        step = np.linalg.solve(H, grad)
        beta += step
        if np.abs(step).max() < 1e-10:
            break
    return float(beta[0]), float(beta[1])


def main():
    samples = calibration_samples()
    d = np.array([s[0] for s in samples])
    y = np.array([s[1] for s in samples], dtype=float)
    keep = np.abs(d) <= 600
    a, b = fit_poisson(d[keep], y[keep])
    print(f"samples={keep.sum()}  a={a:.4f}  b={b:.4f}")
    print(f"  even match: {np.exp(a):.2f} goals each")
    for diff in (100, 200, 400):
        print(f"  d=+{diff}: {np.exp(a + b * diff / 100):.2f} vs "
              f"{np.exp(a - b * diff / 100):.2f}")

    # rho grid search on paired samples (home perspective = even indices)
    dh = d[::2]
    hs = y[::2].astype(int)
    as_ = y[1::2].astype(int)
    ok = np.abs(dh) <= 600
    dh, hs, as_ = dh[ok], hs[ok], as_[ok]
    from backend import match_model

    best = (None, np.inf)
    for rho in np.arange(-0.20, 0.21, 0.02):
        (SEED / "model_params.json").write_text(
            json.dumps({"a": a, "b": b, "rho": round(float(rho), 3)}))
        match_model._params = None
        ll = 0.0
        for dd, h, aa in zip(dh[::7], hs[::7], as_[::7]):  # subsample for speed
            pr = match_model.outcome_probs(dd)
            p = pr["home"] if h > aa else pr["away"] if h < aa else pr["draw"]
            ll -= np.log(max(p, 1e-9))
        if ll < best[1]:
            best = (float(rho), ll)
    rho, ll = best
    (SEED / "model_params.json").write_text(
        json.dumps({"a": round(a, 5), "b": round(b, 5), "rho": round(rho, 3)}))
    print(f"rho={rho:.2f}  logloss={ll:.1f}  -> model_params.json written")


if __name__ == "__main__":
    main()
