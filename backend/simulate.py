"""Vectorized Monte Carlo simulation of the full 48-team tournament.

Group stage with FIFA tiebreakers (pts, GD, GF; residual ties broken
randomly), best-eight thirds placed via the official 495-combination FIFA
allocation table, knockout rounds resolved through the real bracket graph
parsed from Wikipedia. Played matches (any stage) are pinned to their actual
results across all simulations.
"""
from __future__ import annotations

import numpy as np

from .config import (ET_LAMBDA_FACTOR, HOST_CITY_COUNTRY, N_SIMS,
                     WC_HOST_ELO_BONUS)
from .match_model import D_CAP, params

THIRD_SLOTS = [74, 77, 79, 80, 81, 82, 85, 87]
GROUPS = "ABCDEFGHIJKL"


def _lambdas_vec(d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    p = params()
    d = np.clip(d, -D_CAP, D_CAP) / 100.0
    return np.exp(p["a"] + p["b"] * d), np.exp(p["a"] - p["b"] * d)


def run_simulation(teams: list[dict], matches: list[dict], alloc: list[dict],
                   strengths: dict[str, float], n_sims: int = N_SIMS,
                   rng_seed=None) -> dict:
    rng = np.random.default_rng(rng_seed)
    names = [t["name"] for t in teams]
    idx = {n: i for i, n in enumerate(names)}
    nt = len(names)
    S = np.array([strengths[n] for n in names])
    group_of = {t["name"]: t["group"] for t in teams}
    g_members = {g: [idx[t["name"]] for t in teams if t["group"] == g]
                 for g in GROUPS}

    # host-country venue bonus, by city -> per-team vector
    bonus_vec = {}
    for m in matches:
        city = m.get("city", "")
        if city not in bonus_vec:
            v = np.zeros(nt)
            host = HOST_CITY_COUNTRY.get(city)
            if host and host in idx:
                v[idx[host]] = WC_HOST_ELO_BONUS
            bonus_vec[city] = v

    by_num = {m["number"]: m for m in matches}

    # ---------------- group stage ----------------
    pts = np.zeros((n_sims, nt))
    gd = np.zeros((n_sims, nt))
    gf = np.zeros((n_sims, nt))
    for m in matches:
        if m["stage"] != "group":
            continue
        h, a = idx[m["home_team"]], idx[m["away_team"]]
        if m["home_score"] is not None:
            hs = np.full(n_sims, m["home_score"])
            as_ = np.full(n_sims, m["away_score"])
        else:
            bv = bonus_vec[m.get("city", "")]
            lh, la = _lambdas_vec(np.array([S[h] + bv[h] - S[a] - bv[a]]))
            hs = rng.poisson(lh[0], n_sims)
            as_ = rng.poisson(la[0], n_sims)
        pts[:, h] += 3 * (hs > as_) + (hs == as_)
        pts[:, a] += 3 * (as_ > hs) + (hs == as_)
        gd[:, h] += hs - as_
        gd[:, a] += as_ - hs
        gf[:, h] += hs
        gf[:, a] += as_

    # rank within groups: pts > gd > gf > random
    key = pts * 1e8 + (gd + 100) * 1e5 + gf * 1e2 + rng.random((n_sims, nt))
    winner_of = {}
    runner_of = {}
    third_of = {}
    third_key = np.zeros((n_sims, 12))
    for gi, g in enumerate(GROUPS):
        members = np.array(g_members[g])
        order = np.argsort(-key[:, members], axis=1)
        ranked = members[order]                      # (n_sims, 4)
        winner_of[g] = ranked[:, 0]
        runner_of[g] = ranked[:, 1]
        third_of[g] = ranked[:, 2]
        third_key[:, gi] = key[np.arange(n_sims), ranked[:, 2]]

    # best eight thirds + official allocation
    torder = np.argsort(-third_key, axis=1)
    top8 = torder[:, :8]                             # group indices (n_sims, 8)
    mask = np.zeros(n_sims, dtype=np.int64)
    for j in range(8):
        mask |= 1 << top8[:, j]
    slot_pos = {n: i for i, n in enumerate(THIRD_SLOTS)}
    alloc_table = np.full((1 << 12, 8), -1, dtype=np.int8)
    for entry in alloc:
        m_ = 0
        for ch in entry["combo"]:
            m_ |= 1 << GROUPS.index(ch)
        for mn, g in entry["assign"].items():
            alloc_table[m_, slot_pos[int(mn)]] = GROUPS.index(g)
    assigned = alloc_table[mask]                     # (n_sims, 8) group index
    if (assigned < 0).any():
        bad = int((assigned < 0).any(axis=1).sum())
        raise RuntimeError(f"{bad} sims hit a combo missing from alloc table")
    thirds_matrix = np.stack([third_of[g] for g in GROUPS], axis=1)
    third_team_at = {
        mn: thirds_matrix[np.arange(n_sims), assigned[:, j]]
        for j, mn in enumerate(THIRD_SLOTS)
    }

    # ---------------- knockout ----------------
    def resolve(slot, mn):
        t = slot["type"]
        if t == "W":
            return winner_of[slot["group"]]
        if t == "RU":
            return runner_of[slot["group"]]
        if t == "3RD":
            return third_team_at[mn]
        if t == "WM":
            return winners[slot["match"]]
        if t == "LM":
            return losers[slot["match"]]
        if t == "TEAM":
            return np.full(n_sims, idx[slot["team"]])
        raise ValueError(t)

    winners: dict[int, np.ndarray] = {}
    losers: dict[int, np.ndarray] = {}
    participants: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for n in range(73, 105):
        m = by_num[n]
        # pinned real result?
        if m.get("home_team") and m["home_score"] is not None:
            h = np.full(n_sims, idx[m["home_team"]])
            a = np.full(n_sims, idx[m["away_team"]])
            hs, as_ = m["home_score"], m["away_score"]
            if hs != as_:
                w = h if hs > as_ else a
            else:
                pw = (m.get("pen_home") or 0) > (m.get("pen_away") or 0)
                w = h if pw else a
            winners[n], losers[n] = w, h + a - w
            participants[n] = (h, a)
            continue
        h = resolve(m["home_slot"], n)
        a = resolve(m["away_slot"], n)
        participants[n] = (h, a)
        bv = bonus_vec[m.get("city", "")]
        d = S[h] + bv[h] - S[a] - bv[a]
        lh, la = _lambdas_vec(d)
        gh = rng.poisson(lh).astype(float)
        ga = rng.poisson(la).astype(float)
        tie = gh == ga
        if tie.any():
            gh[tie] += rng.poisson(lh[tie] * ET_LAMBDA_FACTOR)
            ga[tie] += rng.poisson(la[tie] * ET_LAMBDA_FACTOR)
            still = gh == ga
            coin = rng.random(n_sims) < 0.5
            gh[still & coin] += 1
            ga[still & ~coin] += 1
        w = np.where(gh > ga, h, a)
        winners[n], losers[n] = w, h + a - w

    # ---------------- aggregate ----------------
    def freq(arr_list):
        c = np.zeros(nt)
        for arr in arr_list:
            c += np.bincount(arr, minlength=nt)
        return c / n_sims

    p_r32 = freq([participants[n][0] for n in range(73, 89)]
                 + [participants[n][1] for n in range(73, 89)])
    p_r16 = freq([winners[n] for n in range(73, 89)])
    p_qf = freq([winners[n] for n in range(89, 97)])
    p_sf = freq([winners[n] for n in range(97, 101)])
    p_final = freq([winners[n] for n in (101, 102)])
    p_champ = freq([winners[104]])
    p_gwin = freq([winner_of[g] for g in GROUPS])

    out = {}
    for n_, i in idx.items():
        out[n_] = {
            "group": group_of[n_],
            "group_win": round(float(p_gwin[i]), 4),
            "r32": round(float(p_r32[i]), 4),
            "r16": round(float(p_r16[i]), 4),
            "qf": round(float(p_qf[i]), 4),
            "sf": round(float(p_sf[i]), 4),
            "final": round(float(p_final[i]), 4),
            "champion": round(float(p_champ[i]), 4),
            "exp_pts": round(float(pts[:, i].mean()), 2),
        }
    return {"teams": out, "n_sims": n_sims}
