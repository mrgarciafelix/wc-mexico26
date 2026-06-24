"""Validate the STYLE matchup feature out-of-sample — the gate it had to pass
before shipping (config.STYLE_WEIGHT).

Style = each team's rolling attack residual (goals scored vs Poisson-expected)
and defense residual (conceded vs expected), decayed over the Elo replay. A
team's expected goals in a fixture are nudged by ITS attack × the OPPONENT's
defense. We replay history once, hold out 2022-06-01..2026-06-01, and compare
Elo-only vs Elo+style on 1X2 and over-2.5 log-loss, with a paired bootstrap 95%
CI — the same "CI must exclude zero" bar the form feature failed and this passes.

    .venv\\Scripts\\python.exe -m scripts.eval_style
"""
from __future__ import annotations

import csv
import math
import sys

import numpy as np

from backend.config import (ELO_HOME_ADV, STYLE_DECAY, STYLE_MULT_CAP,
                            STYLE_RESID_CAP, STYLE_WEIGHT, canonical)
from backend.elo import RESULTS_CSV, EloState, ensure_results_csv
from backend.match_model import lambdas, score_matrix

TEST_FROM, TEST_TO = "2022-06-01", "2026-06-01"
N_BOOT = 2000


def build() -> list:
    """Replay Elo+style; for each test match store d, raw lambdas, and the style
    (attack/defense) of both sides as known *before* the match."""
    ensure_results_csv()
    st = EloState()
    out = []
    with RESULTS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hs, as_ = row["home_score"], row["away_score"]
            if hs in ("NA", "") or as_ in ("NA", ""):
                continue
            h, a = canonical(row["home_team"]), canonical(row["away_team"])
            nt = row["neutral"].upper() == "TRUE"
            H, A = int(float(hs)), int(float(as_))
            d = st.rating[h] + (0 if nt else ELO_HOME_ADV) - st.rating[a]
            if TEST_FROM <= row["date"] < TEST_TO:
                out.append((d, st.style(h), st.style(a), H, A))
            st.apply(h, a, H, A, row["tournament"], nt)
    return out


def smult(sh, sa, w):
    mh = max(-STYLE_MULT_CAP, min(STYLE_MULT_CAP, w * (sh[0] + sa[1])))
    ma = max(-STYLE_MULT_CAP, min(STYLE_MULT_CAP, w * (sa[0] + sh[1])))
    return math.exp(mh), math.exp(ma)


def score(S, w):
    """Per-match 1X2 and over2.5 log-loss arrays under style weight w (0 = off)."""
    n = len(S)
    ll = np.empty(n)
    ov = np.empty(n)
    acc = 0
    for i, (d, sh, sa, H, A) in enumerate(S):
        mh, ma = smult(sh, sa, w) if w else (1.0, 1.0)
        lh, la = lambdas(d)
        m = score_matrix(lh * mh, la * ma)
        home = float(np.tril(m, -1).sum()); away = float(np.triu(m, 1).sum())
        draw = float(np.trace(m))
        p = {"home": home, "draw": draw, "away": away}
        r = "home" if H > A else "away" if A > H else "draw"
        ll[i] = -math.log(max(1e-9, p[r]))
        acc += max(p, key=p.get) == r
        g = np.add.outer(np.arange(11), np.arange(11))
        po = float(m[g > 2.5].sum())
        ov[i] = -math.log(max(1e-9, po if H + A > 2.5 else 1 - po))
    return ll, ov, acc / n


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    S = build()
    n = len(S)
    base_ll, base_ov, base_acc = score(S, 0.0)
    idx = np.random.default_rng(0).integers(0, n, size=(N_BOOT, n))
    print(f"n={n}  hold-out {TEST_FROM}..{TEST_TO}   "
          f"(DECAY={STYLE_DECAY} RESID_CAP={STYLE_RESID_CAP} MULT_CAP={STYLE_MULT_CAP})")
    print(f"  Elo-only: 1X2 ll={base_ll.mean():.4f} acc={base_acc:.3f}  "
          f"over2.5 ll={base_ov.mean():.4f}\n")
    print(f"  {'weight':>6} {'1X2 ll':>8} {'Δ1X2':>9} {'95% CI (1X2)':>20} "
          f"{'ovr ll':>8} {'Δovr':>9}")
    for w in (0.25, 0.35, 0.45, 0.55, 0.70):
        ll, ov, acc = score(S, w)
        d1 = base_ll - ll
        b = d1[idx].mean(axis=1)
        lo, hi = np.percentile(b, [2.5, 97.5])
        sig = " SIG" if (lo > 0 or hi < 0) else ""
        star = "  <- prod" if abs(w - STYLE_WEIGHT) < 1e-9 else ""
        print(f"  {w:6.2f} {ll.mean():8.4f} {d1.mean():+9.5f} "
              f"[{lo:+.5f},{hi:+.5f}]{sig} {ov.mean():8.4f} "
              f"{(base_ov-ov).mean():+9.5f}{star}")
    print("\n+Δ = style beats Elo-only. SIG = paired-bootstrap 95% CI excludes 0.")


if __name__ == "__main__":
    main()
