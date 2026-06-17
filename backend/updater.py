"""Refresh cycle: pull live scores from Wikipedia, recompute Elo on top of
the historical base, re-run the Monte Carlo, snapshot probabilities and log
events so the UI can explain why numbers moved."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from . import db as dbm
from . import wiki
from .config import (ELO_HOME_ADV, HOST_CITY_COUNTRY, SEED, WIKI_MAIN,
                     canonical)
from .elo import compute_base_elo, update_pair
from .ratings import team_strengths
from .simulate import run_simulation

_alloc_cache = None


def alloc():
    global _alloc_cache
    if _alloc_cache is None:
        _alloc_cache = json.loads(
            (SEED / "third_place_alloc.json").read_text(encoding="utf-8"))
    return _alloc_cache


def sync_scores_from_wikipedia(con: sqlite3.Connection) -> list[str]:
    """Match live footballboxes to DB fixtures by (kickoff_utc, city);
    record new/changed scores and newly-known knockout teams. Returns a list
    of human-readable change summaries."""
    html = wiki.fetch(WIKI_MAIN)
    boxes = wiki.parse_matches(html)
    changes: list[str] = []
    team_names = {r["name"] for r in con.execute("SELECT name FROM teams")}
    for b in boxes:
        if not b["kickoff_utc"]:
            continue
        row = con.execute(
            "SELECT * FROM matches WHERE kickoff_utc=? AND city=?",
            (b["kickoff_utc"], b["city"])).fetchone()
        if row is None:
            continue
        n = row["number"]
        # knockout fixtures: fill in real teams once labels resolve
        h_lbl, a_lbl = canonical(b["home_label"]), canonical(b["away_label"])
        if row["stage"] != "group":
            if h_lbl in team_names and row["home_team"] != h_lbl:
                con.execute("UPDATE matches SET home_team=? WHERE number=?",
                            (h_lbl, n))
                changes.append(f"M{n} {row['stage'].upper()}: {h_lbl} qualified")
            if a_lbl in team_names and row["away_team"] != a_lbl:
                con.execute("UPDATE matches SET away_team=? WHERE number=?",
                            (a_lbl, n))
                changes.append(f"M{n} {row['stage'].upper()}: {a_lbl} qualified")
        if b["home_score"] is None:
            continue
        if (row["home_score"] != b["home_score"]
                or row["away_score"] != b["away_score"]
                or row["pen_home"] != b["pen_home"]):
            con.execute(
                """UPDATE matches SET home_score=?, away_score=?,
                   pen_home=?, pen_away=? WHERE number=?""",
                (b["home_score"], b["away_score"], b["pen_home"],
                 b["pen_away"], n))
            home = h_lbl if h_lbl in team_names else row["home_team"]
            away = a_lbl if a_lbl in team_names else row["away_team"]
            pens = (f" (pens {b['pen_home']}-{b['pen_away']})"
                    if b["pen_home"] is not None else "")
            summary = f"{home} {b['home_score']}-{b['away_score']} {away}{pens}"
            changes.append(f"Result M{n}: {summary}")
            dbm.add_event(con, "result", None, f"Result: {summary}")
    con.commit()
    return changes


def current_elo_and_form(con) -> tuple[dict, dict, dict]:
    """Base Elo from history + WC2026 results from our DB applied on top.
    Returns (elo, form, wc_deltas) where wc_deltas[team] = Elo gained at
    this World Cup so far (for explainability)."""
    state = compute_base_elo()
    pre_wc = {r["name"]: state.rating[r["name"]]
              for r in con.execute("SELECT name FROM teams")}
    rows = con.execute(
        """SELECT * FROM matches WHERE home_score IS NOT NULL
           AND home_team IS NOT NULL ORDER BY kickoff_utc""").fetchall()
    for m in rows:
        host = HOST_CITY_COUNTRY.get(m["city"])
        ha = (ELO_HOME_ADV if host == m["home_team"]
              else -ELO_HOME_ADV if host == m["away_team"] else 0.0)
        old_h = state.rating[m["home_team"]]
        old_a = state.rating[m["away_team"]]
        new_h, new_a = update_pair(old_h, old_a, m["home_score"],
                                   m["away_score"], 60, ha)
        state.rating[m["home_team"]] = new_h
        state.rating[m["away_team"]] = new_a
        state.history[m["home_team"]].append(new_h - old_h)
        state.history[m["away_team"]].append(new_a - old_a)
    elo = {t: state.rating[t] for t in pre_wc}
    form = {t: state.form(t) for t in pre_wc}
    wc_delta = {t: elo[t] - pre_wc[t] for t in pre_wc}
    return elo, form, wc_delta


def injuries_out(con) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in con.execute(
            "SELECT team, SUM(importance) s FROM players "
            "WHERE available=0 GROUP BY team"):
        out[r["team"]] = r["s"] or 0.0
    return out


def run_update(con: sqlite3.Connection, trigger: str = "scheduled",
               sync_wiki: bool = True) -> dict:
    changes = []
    if sync_wiki:
        try:
            changes = sync_scores_from_wikipedia(con)
        except Exception as e:  # network down -> still recompute from DB
            changes = [f"wiki sync failed: {e}"]
            dbm.add_event(con, "warning", None, f"Wikipedia sync failed: {e}")

    teams = dbm.teams_list(con)
    elo, form, wc_delta = current_elo_and_form(con)
    strengths = team_strengths(teams, elo, form, injuries_out(con))
    matches = dbm.matches_for_sim(con)
    n_sims = int(dbm.get_setting(con, "n_sims", "20000"))
    res = run_simulation(teams, matches, alloc(),
                         {k: v["strength"] for k, v in strengths.items()},
                         n_sims=n_sims)

    cur = con.execute("INSERT INTO runs (ts, trigger, n_sims) VALUES (?,?,?)",
                      (dbm.now(), trigger, n_sims))
    run_id = cur.lastrowid
    prob_rows, strength_rows = [], []
    for team, p in res["teams"].items():
        for metric in ("champion", "final", "sf", "qf", "r16", "r32",
                       "group_win", "exp_pts"):
            prob_rows.append((run_id, team, metric, p[metric]))
        s = strengths[team]
        strength_rows.append((run_id, team, s["elo"], s["mv_adj"],
                              s["form_adj"], s["injury_adj"], s["manual_adj"],
                              s["strength"]))
    con.executemany("INSERT INTO probs VALUES (?,?,?,?)", prob_rows)
    con.executemany("INSERT INTO strengths VALUES (?,?,?,?,?,?,?,?)",
                    strength_rows)
    con.commit()
    return {"run_id": run_id, "changes": changes, "n_sims": n_sims,
            "wc_elo_delta": wc_delta}
