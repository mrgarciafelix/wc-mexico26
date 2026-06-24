"""Experiment: does a more AGGRESSIVE expected-goals map + a Tweedie-style
overdispersed goal distribution predict World-Cup *results* (W/D/L) better than
the current Poisson + Dixon-Coles baseline?

Motivation (from the user, paraphrasing an analyst's method): tailor the
expected-goals "aggressiveness" (how hard a win-probability edge pushes the
favourite's goal expectation) and the goal distribution's shape parameter,
fitting both to data. The current model OVER-predicts draws on World-Cup games
(P(draw)≈0.28 vs actual≈0.22), a classic symptom of a goal map that's too timid.

Two knobs swept here:
  aggr  — multiplier on the rating gap d feeding λ = exp(a ± b·aggr·d/100).
          aggr=0.88 ≈ current WC production (a confidence *shrink*); aggr>1 is the
          untested *aggressive* direction (sharper favourite/underdog separation).
  disp  — Negative-Binomial dispersion r (variance = μ + μ²/r). r→∞ is Poisson;
          small r adds overdispersion. NegBin is the discrete analogue of the
          Tweedie compound Poisson-Gamma (1<power<2) — same effect: fatter tails,
          more 0-0 and more blowouts, fewer "everyone scores once" draws.

Reports, on every historical World-Cup match and a recent all-comps window:
  acc    1X2 argmax accuracy (the "did we call W/D/L" number)
  ll     1X2 log-loss
  P(dr)  mean predicted draw vs actual draw rate (calibration)
  ovr    mean predicted Over-2.5 vs actual (goals calibration)
  modal  share where the single most-likely scoreline equalled the real score

    .venv\\Scripts\\python.exe -m scripts.eval_aggr
"""
from __future__ import annotations

import csv
import math
import sys

import numpy as np

from backend.config import ELO_HOME_ADV, canonical
from backend.elo import RESULTS_CSV, EloState, ensure_results_csv
from backend.match_model import MAX_GOALS, lambdas, params

WC_KICKOFF = "2026-06-11"
RECENT_FROM = "2018-01-01"
_G = np.add.outer(np.arange(MAX_GOALS + 1), np.arange(MAX_GOALS + 1))
_LGAMMA = np.array([math.lgamma(i + 1) for i in range(MAX_GOALS + 1)])  # log k!


def collect() -> tuple[list, list]:
    ensure_results_csv()
    state = EloState()
    wc, recent = [], []
    with RESULTS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hs, as_ = row["home_score"], row["away_score"]
            if hs in ("NA", "") or as_ in ("NA", ""):
                continue
            h, a = canonical(row["home_team"]), canonical(row["away_team"])
            nt = row["neutral"].upper() == "TRUE"
            H, A = int(float(hs)), int(float(as_))
            d = state.rating[h] + (0 if nt else ELO_HOME_ADV) - state.rating[a]
            tour, date = row["tournament"], row["date"]
            s = {"d": d, "H": min(H, MAX_GOALS), "A": min(A, MAX_GOALS)}
            if tour == "FIFA World Cup" and date < WC_KICKOFF:
                wc.append(s)
            if RECENT_FROM <= date < WC_KICKOFF:
                recent.append(s)
            state.apply(h, a, H, A, tour, nt)
    return wc, recent


def _nb_pmf(mu: float, r: float) -> np.ndarray:
    """Goal-count pmf k=0..MAX_GOALS. r=inf → Poisson; finite r → Negative
    Binomial (Tweedie-style overdispersion)."""
    k = np.arange(MAX_GOALS + 1)
    mu = max(mu, 1e-6)
    if r > 1e6:
        return np.exp(-mu + k * math.log(mu) - _LGAMMA)
    lg = np.array([math.lgamma(i + r) - math.lgamma(r) for i in k]) - _LGAMMA
    return np.exp(lg + r * math.log(r / (r + mu)) + k * math.log(mu / (r + mu)))


def _matrix(lh: float, la: float, disp: float, rho: float) -> np.ndarray:
    m = np.outer(_nb_pmf(lh, disp), _nb_pmf(la, disp))
    m[0, 0] *= 1 - lh * la * rho
    m[0, 1] *= 1 + lh * rho
    m[1, 0] *= 1 + la * rho
    m[1, 1] *= 1 - rho
    s = m.sum()
    return m / s if s > 0 else m


def score(samples: list, aggr: float, disp: float, rho: float) -> dict:
    n = len(samples)
    ll = hits = mpd = adraw = pover = aover = modal = 0.0
    for s in samples:
        lh, la = lambdas(s["d"] * aggr)
        m = _matrix(lh, la, disp, rho)
        home = float(np.tril(m, -1).sum())
        away = float(np.triu(m, 1).sum())
        draw = float(np.trace(m))
        p = {"home": home, "draw": draw, "away": away}
        r = "home" if s["H"] > s["A"] else "away" if s["A"] > s["H"] else "draw"
        ll += -math.log(max(1e-9, p[r]))
        hits += max(p, key=p.get) == r
        mpd += draw
        adraw += r == "draw"
        pover += float(m[_G > 2.5].sum())
        aover += (s["H"] + s["A"]) > 2.5
        mi = np.unravel_index(int(np.argmax(m)), m.shape)
        modal += (mi[0] == s["H"] and mi[1] == s["A"])
    return {"n": n, "acc": hits/n, "ll": ll/n, "pdraw": mpd/n, "adraw": adraw/n,
            "pover": pover/n, "aover": aover/n, "modal": modal/n}


def _row(tag, aggr, disp, r) -> str:
    dl = "inf" if disp > 1e6 else f"{disp:g}"
    return (f"  {tag:9s} aggr={aggr:.2f} disp={dl:>3s}  acc={r['acc']:.3f} "
            f"ll={r['ll']:.4f}  P(dr)={r['pdraw']:.3f}/{r['adraw']:.3f}  "
            f"ovr={r['pover']:.3f}/{r['aover']:.3f}  modal={r['modal']:.3f}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    rho = params().get("rho", 0.0)
    wc, recent = collect()
    print(f"samples: hist_wc={len(wc)}  recent({RECENT_FROM}+)={len(recent)}  "
          f"rho={rho}\n")

    print("=== WORLD-CUP hold-out: aggressiveness × dispersion ===")
    print("  (P(dr)=pred/actual draw · ovr=pred/actual Over2.5)")
    best = (None, -1)
    for aggr in (0.88, 1.00, 1.10, 1.20, 1.30, 1.40):
        for disp in (float("inf"), 12, 8, 5, 3):
            r = score(wc, aggr, disp, rho)
            if r["acc"] > best[1]:
                best = ((aggr, disp), r["acc"])
            print(_row("hist_wc", aggr, disp, r))
        print()
    ba, bd = best[0]
    print(f"best WC accuracy: aggr={ba} disp={'inf' if bd>1e6 else bd} "
          f"(acc {best[1]:.3f})\n")

    print("=== no-regression check on recent all-comps (2018+) ===")
    for aggr, disp in ((0.88, float("inf")), (1.00, float("inf")),
                       (ba, bd), (1.00, 8), (1.10, 8)):
        print(_row("recent", aggr, disp, score(recent, aggr, disp, rho)))


if __name__ == "__main__":
    main()
