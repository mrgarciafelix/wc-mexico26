"""Elo ratings computed from the full international match history (1872+).

Standard World Football Elo: K by match importance, goal-difference
multiplier, +ELO_HOME_ADV for non-neutral home sides. WC2026 matches are
excluded here — the live updater applies them on top from our own DB so
results count exactly once.
"""
from __future__ import annotations

import csv
import math
from collections import defaultdict, deque
from pathlib import Path

import httpx

from .config import (CACHE, ELO_HOME_ADV, ELO_START, K_BY_TOURNAMENT,
                     K_CONTINENTAL, K_DEFAULT, CONTINENTAL_FINALS,
                     RESULTS_CSV_URL, STYLE_DECAY, STYLE_RESID_CAP,
                     USER_AGENT, canonical)

RESULTS_CSV = CACHE / "results.csv"


def ensure_results_csv(force: bool = False) -> Path:
    if force or not RESULTS_CSV.exists():
        r = httpx.get(RESULTS_CSV_URL, headers={"User-Agent": USER_AGENT}, timeout=120)
        r.raise_for_status()
        RESULTS_CSV.write_bytes(r.content)
    return RESULTS_CSV


def k_factor(tournament: str) -> float:
    if tournament in K_BY_TOURNAMENT:
        return K_BY_TOURNAMENT[tournament]
    if any(t in tournament for t in CONTINENTAL_FINALS):
        return K_CONTINENTAL
    if "qualification" in tournament.lower():
        return 40
    return K_DEFAULT


def goal_multiplier(margin: int) -> float:
    if margin <= 1:
        return 1.0
    if margin == 2:
        return 1.5
    return (11 + margin) / 8


def expected(d: float) -> float:
    return 1.0 / (1.0 + 10 ** (-d / 400.0))


def update_pair(ra: float, rb: float, score_a: int, score_b: int,
                k: float, home_adv_a: float) -> tuple[float, float]:
    w = 1.0 if score_a > score_b else 0.0 if score_a < score_b else 0.5
    g = goal_multiplier(abs(score_a - score_b))
    e = expected(ra + home_adv_a - rb)
    delta = k * g * (w - e)
    return ra + delta, rb - delta


class EloState:
    def __init__(self):
        self.rating: dict[str, float] = defaultdict(lambda: ELO_START)
        self.history: dict[str, deque] = defaultdict(lambda: deque(maxlen=10))
        self.attack: dict[str, float] = defaultdict(float)    # style: scoring resid
        self.defense: dict[str, float] = defaultdict(float)   # style: conceding resid
        self.wc_stats: dict[str, dict] = defaultdict(
            lambda: {"matches": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0})

    def update_style(self, home: str, away: str, hs: int, as_: int,
                     d_eff: float) -> None:
        """Decay each side's attack/defense residual = log(actual / Poisson-
        expected goals), using the PRE-match rating diff d_eff. Bounded so a
        single blow-out can't dominate a team's style profile."""
        from .match_model import lambdas
        lh, la = lambdas(d_eff)
        for team, exp_g, act_g in ((home, lh, hs), (away, la, as_)):
            r = max(-STYLE_RESID_CAP, min(STYLE_RESID_CAP,
                                          math.log((act_g + 0.5) / (exp_g + 0.5))))
            self.attack[team] = STYLE_DECAY * self.attack[team] + (1 - STYLE_DECAY) * r
        for team, exp_c, act_c in ((home, la, as_), (away, lh, hs)):
            r = max(-STYLE_RESID_CAP, min(STYLE_RESID_CAP,
                                          math.log((act_c + 0.5) / (exp_c + 0.5))))
            self.defense[team] = STYLE_DECAY * self.defense[team] + (1 - STYLE_DECAY) * r

    def style(self, team: str) -> tuple[float, float]:
        """(attack residual, defense residual) — + attack = scores more than Elo
        implies; + defense = concedes more (leaky)."""
        return self.attack[team], self.defense[team]

    def apply(self, home: str, away: str, hs: int, as_: int,
              tournament: str, neutral: bool):
        k = k_factor(tournament)
        ha = 0.0 if neutral else ELO_HOME_ADV
        old_h, old_a = self.rating[home], self.rating[away]
        self.update_style(home, away, hs, as_, old_h + ha - old_a)
        new_h, new_a = update_pair(old_h, old_a, hs, as_, k, ha)
        self.rating[home], self.rating[away] = new_h, new_a
        self.history[home].append(new_h - old_h)
        self.history[away].append(new_a - old_a)
        if tournament == "FIFA World Cup":
            for team, gf, ga in ((home, hs, as_), (away, as_, hs)):
                st = self.wc_stats[team]
                st["matches"] += 1
                st["gf"] += gf
                st["ga"] += ga
                key = "w" if gf > ga else "l" if gf < ga else "d"
                st[key] += 1

    def form(self, team: str) -> float:
        """Sum of Elo deltas over the last 10 matches."""
        return float(sum(self.history[team]))


def compute_base_elo(cutoff_date: str = "2026-06-01") -> EloState:
    """Run Elo over history, excluding WC2026 finals matches (>= cutoff)."""
    ensure_results_csv()
    state = EloState()
    with RESULTS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hs, as_ = row["home_score"], row["away_score"]
            if hs in ("NA", "") or as_ in ("NA", ""):
                continue
            if row["tournament"] == "FIFA World Cup" and row["date"] >= cutoff_date:
                continue
            state.apply(canonical(row["home_team"]), canonical(row["away_team"]),
                        int(float(hs)), int(float(as_)),
                        row["tournament"], row["neutral"].upper() == "TRUE")
    return state


def calibration_samples(min_date: str = "2010-01-01",
                        max_date: str = "2026-06-01") -> list[tuple[float, int]]:
    """(effective rating diff, goals scored) pairs for fitting the goal model.
    Ratings evolve through the replay so each sample uses pre-match Elo."""
    ensure_results_csv()
    state = EloState()
    samples: list[tuple[float, int]] = []
    with RESULTS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hs, as_ = row["home_score"], row["away_score"]
            if hs in ("NA", "") or as_ in ("NA", ""):
                continue
            home, away = canonical(row["home_team"]), canonical(row["away_team"])
            neutral = row["neutral"].upper() == "TRUE"
            h, a = int(float(hs)), int(float(as_))
            if min_date <= row["date"] < max_date:
                d = (state.rating[home] + (0 if neutral else ELO_HOME_ADV)
                     - state.rating[away])
                samples.append((d, h))
                samples.append((-d, a))
            state.apply(home, away, h, a, row["tournament"], neutral)
    return samples
