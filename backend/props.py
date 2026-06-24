"""Player-prop probabilities derived from the match model.

We don't ingest a granular shots/saves feed, so props are *estimated* from two
things we do have: (1) each team's expected goals in the match (from
match_model) and (2) every player's international scoring rate + importance.
Goals are split across the squad by scoring share; shots / shots-on-target /
saves are backed out with standard football conversion rates. Approximate but
principled — calibrate against the bookmaker's posted odds.

Constants (league-typical):
  GOALS_PER_SOT  ~0.30  a shot on target scores ~30% of the time
  GOALS_PER_SHOT ~0.10  a shot (any) scores ~10% of the time
  save rate       0.70  = 1 - GOALS_PER_SOT
"""
from __future__ import annotations

import math

GOALS_PER_SOT = 0.30
GOALS_PER_SHOT = 0.10
SOT_PER_SHOT = GOALS_PER_SHOT / GOALS_PER_SOT   # ≈0.33 of shots are on target
SHARE_CAP = 0.42            # no single player owns >42% of a team's goals
IMPORTANCE_FLOOR = 0.40     # starter tilt: weight ∝ (floor + importance)
from .playerform import _norm   # name-normaliser shared with the shots feed


def pois_ge(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if lam <= 0:
        return 1.0 if k <= 0 else 0.0
    cum, term = 0.0, math.exp(-lam)
    for i in range(k):
        cum += term
        term *= lam / (i + 1)
    return max(0.0, min(1.0, 1.0 - cum))


def _goal_lambdas(squad: list[dict], team_lambda: float) -> dict[int, float]:
    """Split the team's expected goals across available outfield players."""
    weights = {}
    for p in squad:
        if p["pos"] == "GK" or not p["available"]:
            continue
        rate = (p["goals"] + 0.5) / (p["caps"] + 5)          # smoothed goals/cap
        weights[p["id"]] = rate * (IMPORTANCE_FLOOR + p["importance"])
    tot = sum(weights.values()) or 1.0
    out = {}
    for pid, w in weights.items():
        share = min(SHARE_CAP, w / tot)
        out[pid] = team_lambda * share
    return out


def shots_lookup_safe() -> dict[str, float]:
    """Real per-90 shot volume keyed by normalised name; {} if unavailable."""
    try:
        from . import playerform
        return playerform.shots_per90_lookup()
    except Exception:
        return {}


def outfield_props(squad: list[dict], team_lambda: float, topn: int = 6,
                   shots_lookup: dict[str, float] | None = None) -> list[dict]:
    """Top scoring threats with goal / shot / shot-on-target props.

    SHOTS are priced from REAL per-90 volume (Understat, via `shots_lookup`) when
    the player is covered — this is what makes a high-volume, low-conversion
    shooter (Vinícius: ~3.2 shots/90, but a modest goal share) correctly read as a
    near-lock for 2+ shots, where the goal-derived estimate badly understated him.
    Players outside the top-5 leagues fall back to goals→shots (lam/GOALS_PER_SHOT).
    """
    lams = _goal_lambdas(squad, team_lambda)
    rows = []
    for p in sorted(squad, key=lambda x: -lams.get(x["id"], 0.0))[:topn]:
        lam = lams.get(p["id"], 0.0)
        if lam <= 0:
            continue
        sh90 = (shots_lookup or {}).get(_norm(p["name"]))
        # expected minutes share for a key player (importance gates rotation risk)
        min_frac = 0.60 + 0.35 * p["importance"]
        if sh90:
            shots_lam = sh90 * min_frac
            src = "volume"
        else:
            shots_lam = lam / GOALS_PER_SHOT          # goals→shots fallback
            src = "goals"
        sot_lam = shots_lam * SOT_PER_SHOT
        rows.append({
            "id": p["id"], "name": p["name"], "pos": p["pos"],
            "exp_goals": round(lam, 3), "exp_shots": round(shots_lam, 2),
            "shots_src": src,
            "props": {
                "anytime": pois_ge(1, lam),
                "two_plus": pois_ge(2, lam),
                "shots2": pois_ge(2, shots_lam),
                "sot1": pois_ge(1, sot_lam),
                "sot2": pois_ge(2, sot_lam),
            },
        })
    return rows


def gk_props(squad: list[dict], opp_lambda: float) -> dict | None:
    """Starting keeper save props (expected saves ≈ opp goals × save/score odds)."""
    gks = [p for p in squad if p["pos"] == "GK" and p["available"]]
    if not gks:
        return None
    gk = max(gks, key=lambda p: p["importance"])
    lam_saves = opp_lambda * (1 - GOALS_PER_SOT) / GOALS_PER_SOT
    return {
        "id": gk["id"], "name": gk["name"], "exp_saves": round(lam_saves, 2),
        "props": {"saves2": pois_ge(2, lam_saves), "saves4": pois_ge(4, lam_saves)},
    }


PROP_LABEL = {
    "anytime": "anytime scorer", "two_plus": "2+ goals",
    "shots2": "2+ shots", "sot1": "1+ shot on target", "sot2": "2+ shots on target",
    "saves2": "2+ saves", "saves4": "4+ saves",
}
PROP_FAMILY = {            # mutually-exclusive/nested group for single-bet dedup
    "anytime": "goals", "two_plus": "goals", "shots2": "shots",
    "sot1": "sot", "sot2": "sot", "saves2": "saves", "saves4": "saves",
}
