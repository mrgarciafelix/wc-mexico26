"""SQLite persistence: tournament state, runs/probability snapshots, betting."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH, SEED
from .ratings import player_importance

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
  name TEXT PRIMARY KEY, group_letter TEXT, market_value_eur REAL,
  fifa_points REAL, confed TEXT);
CREATE TABLE IF NOT EXISTS players (
  id INTEGER PRIMARY KEY, team TEXT, no INTEGER, pos TEXT, name TEXT,
  age INTEGER, caps INTEGER, goals INTEGER, club TEXT,
  importance REAL, available INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS matches (
  number INTEGER PRIMARY KEY, stage TEXT, group_letter TEXT,
  kickoff_utc TEXT, stadium TEXT, city TEXT,
  home_team TEXT, away_team TEXT, home_slot TEXT, away_slot TEXT,
  home_score INTEGER, away_score INTEGER, pen_home INTEGER, pen_away INTEGER);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, trigger TEXT, n_sims INTEGER);
CREATE TABLE IF NOT EXISTS probs (
  run_id INTEGER, team TEXT, metric TEXT, value REAL,
  PRIMARY KEY (run_id, team, metric));
CREATE TABLE IF NOT EXISTS strengths (
  run_id INTEGER, team TEXT, elo REAL, mv_adj REAL, form_adj REAL,
  injury_adj REAL, manual_adj REAL, club_form_adj REAL DEFAULT 0, strength REAL,
  PRIMARY KEY (run_id, team));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, type TEXT, team TEXT,
  summary TEXT);
CREATE TABLE IF NOT EXISTS odds (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market TEXT,
  selection TEXT, decimal_odds REAL);
CREATE TABLE IF NOT EXISTS bets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market TEXT, selection TEXT,
  decimal_odds REAL, stake REAL, model_p REAL, status TEXT DEFAULT 'open',
  pnl REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(con: sqlite3.Connection) -> bool:
    """Create schema; load seed data on first run. Returns True if seeded."""
    con.executescript(SCHEMA)
    try:                                   # migrate older DBs: add club_form_adj
        con.execute("ALTER TABLE strengths ADD COLUMN club_form_adj REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    if con.execute("SELECT COUNT(*) c FROM teams").fetchone()["c"]:
        return False
    teams = json.loads((SEED / "teams.json").read_text(encoding="utf-8"))
    matches = json.loads((SEED / "matches.json").read_text(encoding="utf-8"))
    squads = json.loads((SEED / "squads.json").read_text(encoding="utf-8"))
    con.executemany(
        "INSERT INTO teams VALUES (?,?,?,?,?)",
        [(t["name"], t["group"], t["market_value_eur"], t["fifa_points"],
          t["confed"]) for t in teams])
    for m in matches:
        con.execute(
            """INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (m["number"], m["stage"], m.get("group"), m["kickoff_utc"],
             m["stadium"], m["city"], m.get("home_team"), m.get("away_team"),
             json.dumps(m.get("home_slot")) if m.get("home_slot") else None,
             json.dumps(m.get("away_slot")) if m.get("away_slot") else None,
             m["home_score"], m["away_score"], m["pen_home"], m["pen_away"]))
    for team, squad in squads.items():
        imps = player_importance(squad)
        con.executemany(
            """INSERT INTO players (team, no, pos, name, age, caps, goals,
               club, importance, available) VALUES (?,?,?,?,?,?,?,?,?,1)""",
            [(team, p["no"], p["pos"], p["name"], p["age"], p["caps"],
              p["goals"], p["club"], imp) for p, imp in zip(squad, imps)])
    defaults = {"bankroll": "200", "kelly_fraction": "0.25",
                "n_sims": "50000", "update_interval_min": "15"}
    con.executemany("INSERT OR IGNORE INTO settings VALUES (?,?)",
                    defaults.items())
    con.commit()
    return True


def get_setting(con, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con, key: str, value: str):
    con.execute("INSERT INTO settings VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
    con.commit()


def add_event(con, type_: str, team: str | None, summary: str):
    con.execute("INSERT INTO events (ts, type, team, summary) VALUES (?,?,?,?)",
                (now(), type_, team, summary))


def matches_for_sim(con) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM matches ORDER BY number"):
        m = dict(r)
        m["stage"] = r["stage"]
        m["number"] = r["number"]
        if r["home_slot"]:
            m["home_slot"] = json.loads(r["home_slot"])
        if r["away_slot"]:
            m["away_slot"] = json.loads(r["away_slot"])
        out.append(m)
    return out


def teams_list(con) -> list[dict]:
    return [{"name": r["name"], "group": r["group_letter"],
             "market_value_eur": r["market_value_eur"],
             "fifa_points": r["fifa_points"], "confed": r["confed"]}
            for r in con.execute("SELECT * FROM teams ORDER BY name")]


def latest_runs(con, n: int = 2) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def probs_for_run(con, run_id: int) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for r in con.execute("SELECT * FROM probs WHERE run_id=?", (run_id,)):
        out.setdefault(r["team"], {})[r["metric"]] = r["value"]
    return out
