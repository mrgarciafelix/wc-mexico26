"""FastAPI app: betting-style dashboard over the WC2026 prediction engine.

Run:  .venv\\Scripts\\python.exe -m uvicorn backend.app:app --port 8000
Then open http://127.0.0.1:8000
"""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db as dbm
from . import updater
from .betting import evaluate
from .config import (FRONTEND, HOST_CITY_COUNTRY, WC_CONFIDENCE,
                     WC_HOST_ELO_BONUS)
from . import odds_api
from . import predictions as predmod
from . import props as propmod
from . import urgency
from .match_model import MAX_GOALS, outcome_probs, params, style_multipliers
from .optimizer import optimize
from .daily_card import daily_card as build_daily_card

_update_lock = threading.Lock()
scheduler = BackgroundScheduler()

METRICS = ("champion", "final", "sf", "qf", "r16", "r32", "group_win", "exp_pts")


def do_update(trigger: str, sync_wiki: bool = True) -> dict:
    with _update_lock:
        con = dbm.connect()
        try:
            res = updater.run_update(con, trigger=trigger, sync_wiki=sync_wiki)
            try:
                odds_api.sync(con)          # refresh live odds (cached/rate-limited)
            except Exception as e:
                dbm.add_event(con, "warning", None, f"odds sync failed: {e}")
                con.commit()
            return res
        finally:
            con.close()


def scheduled_job():
    try:
        do_update("scheduled")
    except Exception as e:
        con = dbm.connect()
        dbm.add_event(con, "warning", None, f"scheduled update failed: {e}")
        con.commit()
        con.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    con = dbm.connect()
    seeded = dbm.init_db(con)
    has_runs = con.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
    interval = int(dbm.get_setting(con, "update_interval_min", "15"))
    con.close()
    if seeded or not has_runs:
        do_update("startup", sync_wiki=False)
    scheduler.add_job(scheduled_job, "interval", minutes=interval,
                      id="auto-update")
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="WC Mexico 26 — Edge Finder", lifespan=lifespan)


# ---------------------------------------------------------------- helpers ---

def slot_label(slot_json: str | None) -> str:
    if not slot_json:
        return "?"
    s = json.loads(slot_json)
    t = s["type"]
    if t == "W":
        return f"Winner {s['group']}"
    if t == "RU":
        return f"2nd {s['group']}"
    if t == "3RD":
        return "3rd " + "/".join(s["from"])
    if t == "WM":
        return f"W{s['match']}"
    if t == "LM":
        return f"L{s['match']}"
    return s.get("team", "?")


def latest_strengths(con, run_id: int) -> dict[str, dict]:
    return {r["team"]: dict(r) for r in con.execute(
        "SELECT * FROM strengths WHERE run_id=?", (run_id,))}


def match_forecast(m, strengths, urg=None) -> dict | None:
    """Analytic 1X2 for a fixture with both teams known and unplayed.
    urg = (u_home, u_away) group-stage urgency, applied when present."""
    if not (m["home_team"] and m["away_team"]) or m["home_score"] is not None:
        return None
    sh = strengths.get(m["home_team"])
    sa = strengths.get(m["away_team"])
    if not (sh and sa):
        return None
    host = HOST_CITY_COUNTRY.get(m["city"])
    d = (sh["strength"] - sa["strength"]
         + (WC_HOST_ELO_BONUS if host == m["home_team"] else 0)
         - (WC_HOST_ELO_BONUS if host == m["away_team"] else 0)) * WC_CONFIDENCE
    gm, lean = 1.0, 0.0
    if urg:
        d, gm, lean = urgency.apply(d, urg["sig_home"], urg["sig_away"],
                                    urg.get("gp", 1))
    style_mult = style_multipliers(
        (sh.get("style_attack", 0.0), sh.get("style_defense", 0.0)),
        (sa.get("style_attack", 0.0), sa.get("style_defense", 0.0)))
    base_db = params().get("draw_boost", 0.0)
    return outcome_probs(d, draw_boost=base_db + lean, goals_mult=gm,
                         style_mult=style_mult)


def model_prob_for(con, market: str, selection: str) -> float | None:
    """champion -> latest sim prob; match:{n} -> analytic 1X2."""
    runs = dbm.latest_runs(con, 1)
    if not runs:
        return None
    if market in METRICS:
        row = con.execute(
            "SELECT value FROM probs WHERE run_id=? AND team=? AND metric=?",
            (runs[0]["id"], selection, market)).fetchone()
        return row["value"] if row else None
    if market.startswith("match:"):
        n = int(market.split(":")[1])
        m = con.execute("SELECT * FROM matches WHERE number=?", (n,)).fetchone()
        if not m:
            return None
        fc = match_forecast(m, latest_strengths(con, runs[0]["id"]))
        if not fc:
            return None
        return fc.get({"home": "home", "draw": "draw", "away": "away",
                       "over2.5": "over_2_5", "btts": "btts"}.get(selection, selection))
    return None


SEL_WORD = {"home": "win", "away": "win", "draw": "draw",
            "over2.5": "Over 2.5", "btts": "BTTS"}
MARKET_WORD = {"champion": "to win the World Cup", "final": "to reach the Final",
               "sf": "to reach the Semis", "qf": "to reach the QF",
               "r16": "to reach the R16", "r32": "to advance from group",
               "group_win": "to win the group"}


def candidate_meta(con, market: str, selection: str) -> tuple[set[str], str]:
    """Underlying team set (correlation key) and a human label for a selection."""
    if market.startswith("match:"):
        n = int(market.split(":")[1])
        m = con.execute("SELECT home_team, away_team FROM matches WHERE number=?",
                        (n,)).fetchone()
        if not m:
            return {selection}, f"{market} {selection}"
        home, away = m["home_team"], m["away_team"]
        teams = {t for t in (home, away) if t}
        if selection == "home":
            label = f"{home} to beat {away}"
        elif selection == "away":
            label = f"{away} to beat {home}"
        elif selection == "draw":
            label = f"{home} vs {away} — draw"
        else:
            label = f"{home} vs {away} — {SEL_WORD.get(selection, selection)}"
        return teams, label
    label = f"{selection} {MARKET_WORD.get(market, market)}"
    return {selection}, label


def build_candidates(con, odds_rows) -> list[dict]:
    """Annotate (market, selection, odds) rows with model_p, team key and label."""
    out = []
    for o in odds_rows:
        p = model_prob_for(con, o["market"], o["selection"])
        if p is None:
            continue
        teams, label = candidate_meta(con, o["market"], o["selection"])
        out.append({"market": o["market"], "selection": o["selection"],
                    "decimal_odds": o["decimal_odds"], "model_p": p,
                    "teams": teams, "label": label})
    return out


# ----- self-contained snapshot (same shape live and on Netlify) -------------

OUTRIGHTS = ("champion", "final", "sf")
OUTRIGHT_TOPN = {"champion": 20, "final": 14, "sf": 10}
MATCH_SAMPLE = 18           # upcoming matches that get illustrative 1X2 odds
PROP_MATCHES = 10           # near-window matches that get player props
PROP_TOPN = 6               # scoring threats per team


def sample_book(p: float, key: str, overround: float = 0.05) -> float:
    """Deterministic *illustrative* bookmaker odds that behave like a real
    board: a bookmaker margin **plus the favourite–longshot bias** — books shade
    longshots short, so underdogs price as NEGATIVE value — with a small
    market-disagreement wobble so a realistic minority of selections (mostly
    favourites) show an edge. Placeholders only: enter your bookmaker's real
    prices for genuine edges. (implied = p**0.93 lifts low p → dogs overpriced.)"""
    if not p or p <= 0:
        return 0.0
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)
    wobble = 1.0 + ((h % 1000) / 1000.0 - 0.5) * 0.38        # ±19% disagreement
    implied = min(0.97, (p ** 0.93) * (1.0 + overround) * wobble)
    return round(1.0 / implied, 2)


def build_snapshot() -> dict:
    """Everything the frontend needs in one object: dashboard data, a catalog
    of every bettable selection with its model probability, an illustrative
    odds board (merged with any real odds in the DB) and the optimal plan."""
    ov = overview()
    gr = groups()
    ms = matches()
    try:
        mv = movers("champion").get("movers", [])
    except Exception:
        mv = []

    con = dbm.connect()
    try:
        markets: list[dict] = []
        sample: dict[str, float] = {}
        # real odds (live API feed + any manual entries) keyed market|selection
        db_odds = {f"{o['market']}|{o['selection']}": o["decimal_odds"]
                   for o in con.execute("SELECT * FROM odds GROUP BY market, "
                                        "selection HAVING id = MAX(id)")}
        # --- outright winner / final / SF -----------------------------------
        for mk in OUTRIGHTS:
            ranked = sorted(ov["teams"], key=lambda t: -t["probs"][mk])
            for i, t in enumerate(ranked):
                p = t["probs"][mk]
                _, label = candidate_meta(con, mk, t["team"])
                key = f"{mk}|{t['team']}"
                markets.append({"key": key, "kind": "outright", "market": mk,
                                "selection": t["team"], "label": label,
                                "model_p": round(p, 4), "teams": [t["team"]],
                                "mutex": f"outright:{mk}", "group": t["group"]})
                if i < OUTRIGHT_TOPN[mk] and p > 0.003:
                    sample[key] = sample_book(p, key)

        upcoming = sorted(
            [m for m in ms if m.get("forecast") and m.get("home_team")
             and m.get("away_team") and m.get("home_score") is None],
            key=lambda m: m.get("kickoff_utc") or "")
        # --- match 1X2 ------------------------------------------------------
        for idx, m in enumerate(upcoming):
            fc = m["forecast"]
            for sel in ("home", "draw", "away"):
                _, label = candidate_meta(con, f"match:{m['number']}", sel)
                key = f"match:{m['number']}|{sel}"
                markets.append({
                    "key": key, "kind": "match",
                    "market": f"match:{m['number']}", "selection": sel,
                    "label": label, "model_p": round(fc[sel], 4),
                    "teams": [m["home_team"], m["away_team"]],
                    "mutex": f"m{m['number']}:1x2",
                    "match_no": m["number"], "stage": m["stage"],
                    "kickoff": m.get("kickoff_utc")})
                if idx < MATCH_SAMPLE:
                    sample[key] = sample_book(fc[sel], key)

        # --- player props (near window) -------------------------------------
        squad_cache: dict[str, list[dict]] = {}
        shots_lk = propmod.shots_lookup_safe()   # real shot volume (Understat)

        def squad(team: str) -> list[dict]:
            if team not in squad_cache:
                squad_cache[team] = [dict(r) for r in con.execute(
                    "SELECT id,name,pos,caps,goals,importance,available "
                    "FROM players WHERE team=?", (team,))]
            return squad_cache[team]

        def add_prop(mno, pid, team, teams, kickoff, stage, match_txt,
                     name, sel, p):
            if p < 0.02 or p > 0.985:
                return
            market = f"prop:{mno}:{pid}"
            key = f"{market}|{sel}"
            markets.append({
                "key": key, "kind": "prop", "market": market, "selection": sel,
                "label": f"{name} — {propmod.PROP_LABEL[sel]}",
                "model_p": round(p, 4), "teams": teams,
                "mutex": f"prop:{pid}:{propmod.PROP_FAMILY[sel]}",
                "match_no": mno, "stage": stage, "kickoff": kickoff,
                "team": team, "player": name,
                "prop_type": propmod.PROP_LABEL[sel], "match": match_txt})
            sample[key] = sample_book(p, key)

        for m in upcoming[:PROP_MATCHES]:
            fc, mno = m["forecast"], m["number"]
            teams = [m["home_team"], m["away_team"]]
            match_txt = f"{m['home_team']} v {m['away_team']}"
            lam = {m["home_team"]: fc["exp_goals_home"],
                   m["away_team"]: fc["exp_goals_away"]}
            for team, opp in ((m["home_team"], m["away_team"]),
                              (m["away_team"], m["home_team"])):
                for pr in propmod.outfield_props(squad(team), lam[team],
                                                 PROP_TOPN, shots_lk):
                    for sel, p in pr["props"].items():
                        add_prop(mno, pr["id"], team, teams, m.get("kickoff_utc"),
                                 m["stage"], match_txt, pr["name"], sel, p)
                gk = propmod.gk_props(squad(team), lam[opp])
                if gk:
                    for sel, p in gk["props"].items():
                        add_prop(mno, gk["id"], team, teams, m.get("kickoff_utc"),
                                 m["stage"], match_txt, gk["name"], sel, p)

        # real odds override the illustrative ones; tag those markets "live"
        for mk in markets:
            mk["live"] = mk["key"] in db_odds
        for k, v in db_odds.items():
            sample[k] = v
        live_n = sum(1 for mk in markets if mk["live"])
        live_file = odds_api._load_file() or {}
        odds_source = {"live": live_n > 0, "live_markets": live_n,
                       "books": live_file.get("books", 0),
                       "fetched_at": live_file.get("fetched_at")}
        try:
            from . import backtest as _bt
            bt = _bt.cached()
            accuracy = {k: bt.get(k) for k in (
                "n", "accuracy", "baseline_accuracy", "model_logloss",
                "baseline_logloss", "logloss_edge_pct", "ece", "window")}
        except Exception:
            accuracy = None
        try:
            track_record = predmod.settled(con)
        except Exception:
            track_record = {"n": 0, "items": []}

        # plan candidates straight from the catalog (same as the frontend)
        by_key = {mk["key"]: mk for mk in markets}
        cands = []
        for key, o in sample.items():
            mk = by_key.get(key)
            if mk:
                cands.append({**mk, "decimal_odds": o})
            else:
                market, sel = key.split("|", 1)
                p = model_prob_for(con, market, sel)
                if p is not None:
                    teams, label = candidate_meta(con, market, sel)
                    cands.append({"market": market, "selection": sel,
                                  "decimal_odds": o, "model_p": p,
                                  "teams": list(teams), "label": label})
        bankroll = float(dbm.get_setting(con, "bankroll", "200"))
        kf = float(dbm.get_setting(con, "kelly_fraction", "0.25"))
        # honest data-age: when results were last pulled from Wikipedia and
        # whether that pull succeeded (the static site can only be as fresh as
        # the last CI/publish that regenerated this snapshot).
        data_status = {
            "last_sync_ts": dbm.get_setting(con, "last_sync_ts", "") or None,
            "last_sync_ok": dbm.get_setting(con, "last_sync_ok", "1") == "1",
            "last_sync_boxes": int(dbm.get_setting(con, "last_sync_boxes", "0") or 0),
            "interval_min": int(dbm.get_setting(con, "update_interval_min", "15")),
        }
        plan = optimize(cands, bankroll, kf)
        # zero-input daily parlay card: uses REAL consensus odds where we have
        # them, model-fair price elsewhere — never the illustrative sample_book
        # odds. Squads of the next slate's teams feed the embedded prop legs.
        slate_squads: dict[str, list[dict]] = {}
        if upcoming:
            slate_day = (upcoming[0].get("kickoff_utc") or "")[:10]
            for m in upcoming:
                if (m.get("kickoff_utc") or "")[:10] != slate_day:
                    break
                for tm in (m["home_team"], m["away_team"]):
                    if tm and tm not in slate_squads:
                        slate_squads[tm] = squad(tm)
        card = build_daily_card(ms, db_odds, slate_squads, shots_lk)
        try:
            predmod.log_staking_plan(con, card)         # lock today's plan
            staking = predmod.staking_record(con)       # daily-betting track record
        except Exception:
            staking = {"n_settled": 0, "days": []}
    finally:
        con.close()

    return {
        "meta": {"generated": dbm.now(), "run": ov["run"],
                 "matches_played": ov["matches_played"],
                 "matches_total": ov["matches_total"],
                 "bankroll": bankroll, "kelly_fraction": kf,
                 "rho": params().get("rho", 0.0), "max_goals": MAX_GOALS,
                 "odds_source": odds_source, "accuracy": accuracy,
                 "data_status": data_status},
        "teams": ov["teams"], "groups": gr, "matches": ms,
        "events": ov["events"], "movers": mv,
        "markets": markets, "sample_odds": sample, "plan": plan,
        "daily_card": card, "track_record": track_record,
        "staking_record": staking,
    }


# -------------------------------------------------------------- endpoints ---

@app.get("/api/overview")
def overview():
    con = dbm.connect()
    try:
        runs = dbm.latest_runs(con, 2)
        if not runs:
            raise HTTPException(503, "no simulation run yet")
        cur = dbm.probs_for_run(con, runs[0]["id"])
        prev = dbm.probs_for_run(con, runs[1]["id"]) if len(runs) > 1 else {}
        strengths = latest_strengths(con, runs[0]["id"])
        teams = {t["name"]: t for t in dbm.teams_list(con)}
        played = con.execute(
            "SELECT COUNT(*) c FROM matches WHERE home_score IS NOT NULL"
        ).fetchone()["c"]
        out = []
        for name, p in cur.items():
            d = {m: round(p[m] - prev.get(name, {}).get(m, p[m]), 4)
                 for m in METRICS}
            out.append({
                "team": name, "group": teams[name]["group"],
                "market_value_eur": teams[name]["market_value_eur"],
                "probs": p, "delta": d, "strength": strengths.get(name),
            })
        events = [dict(r) for r in con.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT 25")]
        return {"run": dict(runs[0]),
                "prev_run": dict(runs[1]) if len(runs) > 1 else None,
                "teams": out, "events": events,
                "matches_played": played, "matches_total": 104}
    finally:
        con.close()


@app.get("/api/groups")
def groups():
    con = dbm.connect()
    try:
        runs = dbm.latest_runs(con, 1)
        cur = dbm.probs_for_run(con, runs[0]["id"]) if runs else {}
        teams = dbm.teams_list(con)
        stats = {t["name"]: {"team": t["name"], "pld": 0, "w": 0, "d": 0,
                             "l": 0, "gf": 0, "ga": 0, "pts": 0} for t in teams}
        for m in con.execute("SELECT * FROM matches WHERE stage='group' "
                             "AND home_score IS NOT NULL"):
            h, a = stats[m["home_team"]], stats[m["away_team"]]
            hs, as_ = m["home_score"], m["away_score"]
            h["pld"] += 1; a["pld"] += 1
            h["gf"] += hs; h["ga"] += as_; a["gf"] += as_; a["ga"] += hs
            if hs > as_: h["w"] += 1; a["l"] += 1; h["pts"] += 3
            elif hs < as_: a["w"] += 1; h["l"] += 1; a["pts"] += 3
            else: h["d"] += 1; a["d"] += 1; h["pts"] += 1; a["pts"] += 1
        out = {}
        for t in teams:
            g = t["group"]
            row = dict(stats[t["name"]])
            row["gd"] = row["gf"] - row["ga"]
            row.update({m: cur.get(t["name"], {}).get(m) for m in
                        ("group_win", "r32", "champion", "exp_pts")})
            out.setdefault(g, []).append(row)
        for g in out:
            out[g].sort(key=lambda r: (-r["pts"], -r["gd"], -r["gf"],
                                       -(r["group_win"] or 0)))
        return out
    finally:
        con.close()


@app.get("/api/matches")
def matches():
    con = dbm.connect()
    try:
        runs = dbm.latest_runs(con, 1)
        strengths = latest_strengths(con, runs[0]["id"]) if runs else {}
        urg = urgency.match_urgency(con)
        out = []
        for m in con.execute("SELECT * FROM matches ORDER BY number"):
            e = dict(m)
            e["home_label"] = m["home_team"] or slot_label(m["home_slot"])
            e["away_label"] = m["away_team"] or slot_label(m["away_slot"])
            e.pop("home_slot", None); e.pop("away_slot", None)
            fc = match_forecast(m, strengths, urg.get(m["number"]))
            if fc:
                e["forecast"] = {k: round(v, 4) for k, v in fc.items()}
            out.append(e)
        return out
    finally:
        con.close()


@app.get("/api/trends")
def trends(metric: str = "champion", top: int = 12):
    con = dbm.connect()
    try:
        runs = con.execute("SELECT * FROM runs ORDER BY id").fetchall()
        if not runs:
            return {"runs": [], "series": {}}
        last = runs[-1]["id"]
        leaders = [r["team"] for r in con.execute(
            "SELECT team FROM probs WHERE run_id=? AND metric=? "
            "ORDER BY value DESC LIMIT ?", (last, metric, top))]
        series = {t: [] for t in leaders}
        for r in con.execute(
                f"SELECT run_id, team, value FROM probs WHERE metric=? "
                f"AND team IN ({','.join('?'*len(leaders))}) ORDER BY run_id",
                (metric, *leaders)):
            if r["team"] in series:
                series[r["team"]].append({"run": r["run_id"], "v": r["value"]})
        return {"runs": [dict(r) for r in runs], "series": series,
                "metric": metric}
    finally:
        con.close()


@app.get("/api/movers")
def movers(metric: str = "champion"):
    con = dbm.connect()
    try:
        runs = dbm.latest_runs(con, 2)
        if len(runs) < 2:
            return {"movers": [], "events": []}
        cur, prev = (dbm.probs_for_run(con, runs[0]["id"]),
                     dbm.probs_for_run(con, runs[1]["id"]))
        s_cur = latest_strengths(con, runs[0]["id"])
        s_prev = latest_strengths(con, runs[1]["id"])
        window_events = [dict(r) for r in con.execute(
            "SELECT * FROM events WHERE ts > ? AND ts <= ? ORDER BY id",
            (runs[1]["ts"], runs[0]["ts"]))]
        out = []
        for team, p in cur.items():
            dv = p[metric] - prev.get(team, {}).get(metric, p[metric])
            reasons = []
            for ev in window_events:
                if ev["team"] == team or team in (ev["summary"] or ""):
                    reasons.append(ev["summary"])
            sc, sp = s_cur.get(team), s_prev.get(team)
            if sc and sp:
                if abs(sc["elo"] - sp["elo"]) >= 0.5:
                    reasons.append(f"Elo {sp['elo']:.0f} → {sc['elo']:.0f}")
                if abs(sc["injury_adj"] - sp["injury_adj"]) >= 0.5:
                    reasons.append(
                        f"availability adj {sp['injury_adj']:+.0f} → "
                        f"{sc['injury_adj']:+.0f} Elo")
                if abs(sc["form_adj"] - sp["form_adj"]) >= 0.5:
                    reasons.append(
                        f"form adj {sp['form_adj']:+.0f} → {sc['form_adj']:+.0f}")
            if not reasons and abs(dv) > 0.001:
                reasons.append("path shifted (other results) / sim noise")
            out.append({"team": team, "value": p[metric], "delta": round(dv, 4),
                        "reasons": reasons[:4]})
        out.sort(key=lambda r: -abs(r["delta"]))
        return {"movers": out[:16], "events": window_events,
                "metric": metric, "from": runs[1]["ts"], "to": runs[0]["ts"]}
    finally:
        con.close()


@app.post("/api/refresh")
def refresh():
    res = do_update("manual")
    return {"ok": True, **{k: res[k] for k in ("run_id", "changes", "n_sims")}}


@app.get("/api/squad/{team}")
def squad(team: str):
    con = dbm.connect()
    try:
        players = [dict(r) for r in con.execute(
            "SELECT * FROM players WHERE team=? ORDER BY importance DESC",
            (team,))]
        if not players:
            raise HTTPException(404, f"unknown team {team}")
        runs = dbm.latest_runs(con, 1)
        s = latest_strengths(con, runs[0]["id"]).get(team) if runs else None
        return {"team": team, "players": players, "strength": s}
    finally:
        con.close()


@app.post("/api/player/{pid}/availability")
def set_availability(pid: int, body: dict = Body(...)):
    available = 1 if body.get("available") else 0
    con = dbm.connect()
    try:
        row = con.execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "player not found")
        con.execute("UPDATE players SET available=? WHERE id=?", (available, pid))
        verb = "available" if available else "OUT (injury/suspension)"
        dbm.add_event(con, "availability", row["team"],
                      f"{row['name']} ({row['team']}) marked {verb} "
                      f"[importance {row['importance']:.2f}]")
        con.commit()
    finally:
        con.close()
    res = do_update("availability-edit", sync_wiki=False)
    return {"ok": True, "run_id": res["run_id"]}


@app.get("/api/value")
def value_board():
    con = dbm.connect()
    try:
        bankroll = float(dbm.get_setting(con, "bankroll", "200"))
        kf = float(dbm.get_setting(con, "kelly_fraction", "0.25"))
        rows = []
        for o in con.execute(
                "SELECT * FROM odds GROUP BY market, selection "
                "HAVING id = MAX(id) ORDER BY id DESC"):
            p = model_prob_for(con, o["market"], o["selection"])
            entry = {"id": o["id"], "market": o["market"],
                     "selection": o["selection"],
                     "decimal_odds": o["decimal_odds"], "ts": o["ts"]}
            if p is not None:
                entry.update(evaluate(p, o["decimal_odds"], bankroll, kf))
            rows.append(entry)
        rows.sort(key=lambda r: -(r.get("edge") or -9))
        return {"bankroll": bankroll, "kelly_fraction": kf, "rows": rows}
    finally:
        con.close()


@app.get("/api/plan")
def plan():
    """Growth-optimal staking plan over the current odds board."""
    con = dbm.connect()
    try:
        bankroll = float(dbm.get_setting(con, "bankroll", "200"))
        kf = float(dbm.get_setting(con, "kelly_fraction", "0.25"))
        rows = con.execute(
            "SELECT * FROM odds GROUP BY market, selection "
            "HAVING id = MAX(id)").fetchall()
        cands = build_candidates(con, rows)
        return optimize(cands, bankroll, kf)
    finally:
        con.close()


@app.get("/api/snapshot")
def snapshot():
    """Full self-contained state — same shape as the static Netlify file."""
    return build_snapshot()


@app.post("/api/odds")
def add_odds(body: dict = Body(...)):
    market = str(body["market"]).strip()
    selection = str(body["selection"]).strip()
    o = float(body["decimal_odds"])
    if o <= 1.0:
        raise HTTPException(400, "decimal odds must be > 1.0")
    con = dbm.connect()
    try:
        p = model_prob_for(con, market, selection)
        if p is None:
            raise HTTPException(
                400, f"no model probability for {market}/{selection} — "
                     "use market 'champion'/'final'/'sf' with a team name, "
                     "or 'match:N' with home/draw/away")
        con.execute("INSERT INTO odds (ts, market, selection, decimal_odds) "
                    "VALUES (?,?,?,?)", (dbm.now(), market, selection, o))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.delete("/api/odds/{oid}")
def delete_odds(oid: int):
    con = dbm.connect()
    try:
        con.execute("DELETE FROM odds WHERE id=?", (oid,))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.get("/api/bets")
def bets():
    con = dbm.connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM bets ORDER BY id DESC")]
        staked = sum(r["stake"] for r in rows if r["status"] == "open")
        pnl = sum(r["pnl"] for r in rows)
        return {"bets": rows, "open_stake": round(staked, 2),
                "total_pnl": round(pnl, 2)}
    finally:
        con.close()


@app.post("/api/bets")
def place_bet(body: dict = Body(...)):
    con = dbm.connect()
    try:
        p = model_prob_for(con, body["market"], body["selection"])
        con.execute(
            "INSERT INTO bets (ts, market, selection, decimal_odds, stake, "
            "model_p) VALUES (?,?,?,?,?,?)",
            (dbm.now(), body["market"], body["selection"],
             float(body["decimal_odds"]), float(body["stake"]), p))
        dbm.add_event(con, "bet", None,
                      f"Bet logged: {body['selection']} @ {body['decimal_odds']} "
                      f"({body['market']}) stake {body['stake']}")
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.post("/api/bets/{bid}/settle")
def settle_bet(bid: int, body: dict = Body(...)):
    status = body.get("status")
    if status not in ("won", "lost", "void"):
        raise HTTPException(400, "status must be won/lost/void")
    con = dbm.connect()
    try:
        b = con.execute("SELECT * FROM bets WHERE id=?", (bid,)).fetchone()
        if not b:
            raise HTTPException(404, "bet not found")
        pnl = (b["stake"] * (b["decimal_odds"] - 1) if status == "won"
               else -b["stake"] if status == "lost" else 0.0)
        con.execute("UPDATE bets SET status=?, pnl=? WHERE id=?",
                    (status, round(pnl, 2), bid))
        con.commit()
        return {"ok": True, "pnl": round(pnl, 2)}
    finally:
        con.close()


@app.get("/api/settings")
def get_settings():
    con = dbm.connect()
    try:
        return {r["key"]: r["value"]
                for r in con.execute("SELECT * FROM settings")}
    finally:
        con.close()


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    allowed = {"bankroll", "kelly_fraction", "n_sims", "update_interval_min"}
    con = dbm.connect()
    try:
        for k, v in body.items():
            if k in allowed:
                dbm.set_setting(con, k, str(v))
        if "update_interval_min" in body:
            scheduler.reschedule_job(
                "auto-update", trigger="interval",
                minutes=int(body["update_interval_min"]))
        return {"ok": True}
    finally:
        con.close()


# ----------------------------------------------------------------- static ---
# Serve the SPA at the root with relative asset paths so the *same* files work
# unchanged on the live server, on Netlify (domain root) and on GitHub Pages
# (project sub-path). API routes are registered above, so they win over the
# catch-all mount below.

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="root")
