"""Blended team strength = Elo + market value + form + availability."""
from __future__ import annotations

import math

import numpy as np

from .config import (FORM_CAP, FORM_WEIGHT, INJURY_ELO_PER_IMPORTANCE,
                     MV_WEIGHT)


def player_importance(squad: list[dict]) -> list[float]:
    """0..1 per player: caps share (experience/likely starter) + goal threat."""
    max_caps = max((p["caps"] for p in squad), default=1) or 1
    max_goals = max((p["goals"] for p in squad), default=1) or 1
    return [round(0.7 * p["caps"] / max_caps + 0.3 * p["goals"] / max_goals, 4)
            for p in squad]


def mv_zscores(teams: list[dict]) -> dict[str, float]:
    logs = {t["name"]: math.log(max(t["market_value_eur"], 1e6)) for t in teams}
    vals = np.array(list(logs.values()))
    mu, sd = vals.mean(), vals.std() or 1.0
    return {k: (v - mu) / sd for k, v in logs.items()}


def team_strengths(teams: list[dict], elo: dict[str, float],
                   form: dict[str, float],
                   injury_importance_out: dict[str, float],
                   manual_adj: dict[str, float] | None = None,
                   club_form: dict[str, float] | None = None) -> dict[str, dict]:
    """Per team: blended strength plus the decomposition (for explainability)."""
    z = mv_zscores(teams)
    out = {}
    for t in teams:
        name = t["name"]
        base = elo.get(name, 1500.0)
        mv_adj = MV_WEIGHT * z[name]
        form_adj = float(np.clip(FORM_WEIGHT * form.get(name, 0.0),
                                 -FORM_CAP, FORM_CAP))
        inj_adj = -INJURY_ELO_PER_IMPORTANCE * injury_importance_out.get(name, 0.0)
        man_adj = (manual_adj or {}).get(name, 0.0)
        cf_adj = (club_form or {}).get(name, 0.0)      # current club xG/form
        out[name] = {
            "elo": round(base, 1),
            "mv_adj": round(mv_adj, 1),
            "form_adj": round(form_adj, 1),
            "injury_adj": round(inj_adj, 1),
            "manual_adj": round(man_adj, 1),
            "club_form_adj": round(cf_adj, 1),
            "strength": round(base + mv_adj + form_adj + inj_adj + man_adj + cf_adj, 1),
        }
    return out
