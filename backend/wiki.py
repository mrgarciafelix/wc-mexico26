"""Wikipedia parsing: matches, groups, third-place allocation, squads.

Used once by scripts/build_seed.py to freeze the tournament structure, and on
every updater cycle to pull fresh scores. Matches are keyed by (kickoff_utc,
city) which is stable on Wikipedia even after match numbers disappear from
played fixtures.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from io import StringIO

import httpx
import pandas as pd
from bs4 import BeautifulSoup

from .config import USER_AGENT, canonical

MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}


def fetch(url: str) -> str:
    r = httpx.get(url, headers={"User-Agent": USER_AGENT},
                  follow_redirects=True, timeout=60)
    r.raise_for_status()
    return r.text


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).replace("\xa0", " ").split()) if el else ""


def _parse_kickoff(box) -> tuple[str | None, str | None]:
    """Return (kickoff_utc_iso, local_label) from a footballbox."""
    bday = box.select_one(".bday")
    date_iso = _text(bday) if bday else None
    ftime = box.select_one(".ftime")
    tlabel = _text(ftime)
    if not date_iso:
        return None, tlabel
    m = re.search(r"(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", tlabel, re.I)
    off = re.search(r"UTC\s*[−\-–]\s*(\d{1,2})(?::(\d{2}))?", tlabel)
    if not m:
        return date_iso, tlabel
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3).lower() == "p" and h != 12:
        h += 12
    if m.group(3).lower() == "a" and h == 12:
        h = 0
    offh = int(off.group(1)) if off else 0
    offm = int(off.group(2) or 0) if off else 0
    local = datetime.fromisoformat(date_iso).replace(hour=h, minute=mi)
    utc = local + timedelta(hours=offh, minutes=offm)  # offsets are all UTC−X
    return utc.replace(tzinfo=timezone.utc).isoformat(), tlabel


def _parse_score(txt: str) -> tuple[int | None, int | None, int | None]:
    """fscore text -> (home, away, match_number). Handles '2–0', '2–0 (a.e.t.)',
    'Match 25'."""
    txt = txt.replace("\xa0", " ").strip()
    mnum = re.search(r"Match\s+(\d+)", txt)
    if mnum:
        return None, None, int(mnum.group(1))
    ms = re.search(r"(\d+)\s*[–\-−]\s*(\d+)", txt)
    if ms:
        return int(ms.group(1)), int(ms.group(2)), None
    return None, None, None


def _parse_penalties(box) -> tuple[int | None, int | None]:
    """Penalty shoot-out score if shown (e.g. 'Penalties 4–3')."""
    for el in box.select(".fgoals, .fevent"):
        t = _text(el)
        m = re.search(r"Penalties.*?(\d+)\s*[–\-−]\s*(\d+)", t)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None, None


def parse_matches(html: str) -> list[dict]:
    """All footballboxes in document order with section-heading context."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    current_heading = ""
    for el in soup.find_all(["h2", "h3", "h4", "div"]):
        if el.name in ("h2", "h3", "h4"):
            current_heading = _text(el)
            continue
        if "footballbox" not in (el.get("class") or []):
            continue
        box = el
        home = _text(box.select_one(".fhome"))
        away = _text(box.select_one(".faway"))
        score_txt = _text(box.select_one(".fscore"))
        hs, as_, num = _parse_score(score_txt)
        kickoff_utc, time_label = _parse_kickoff(box)
        fright = _text(box.select_one(".fright"))
        stadium, city = "", ""
        if fright:
            head = re.split(r"Attendance|Referee", fright)[0].strip()
            parts = [p.strip() for p in head.split(",")]
            if len(parts) >= 2:
                stadium, city = parts[0], parts[-1]
            else:
                stadium = head
        ph, pa = _parse_penalties(box)
        out.append({
            "heading": current_heading,
            "home_label": home, "away_label": away,
            "home_score": hs, "away_score": as_,
            "pen_home": ph, "pen_away": pa,
            "match_number": num,
            "kickoff_utc": kickoff_utc, "time_label": time_label,
            "stadium": stadium, "city": city,
        })
    return out


def parse_groups(html: str) -> dict[str, list[dict]]:
    """Group letter -> standings rows [{team, pld, w, d, l, gf, ga, pts}, ...].
    Wikipedia keeps these tables current, so we reuse them for live tables."""
    soup = BeautifulSoup(html, "lxml")
    groups: dict[str, list[dict]] = {}
    current_group = None
    for el in soup.find_all(["h2", "h3", "h4", "table"]):
        if el.name != "table":
            m = re.search(r"^Group ([A-L])\b", _text(el))
            current_group = m.group(1) if m else current_group
            continue
        if current_group is None or current_group in groups:
            continue
        header = _text(el.find("tr"))
        if not header.startswith("Pos"):
            continue
        try:
            df = pd.read_html(StringIO(str(el)))[0]
        except ValueError:
            continue
        rows = []
        for _, r in df.iterrows():
            team_col = [c for c in df.columns if str(c).startswith("Team")][0]
            rows.append({
                "team": canonical(str(r[team_col])),
                "pld": int(r.get("Pld", 0)), "w": int(r.get("W", 0)),
                "d": int(r.get("D", 0)), "l": int(r.get("L", 0)),
                "gf": int(r.get("GF", 0)), "ga": int(r.get("GA", 0)),
                "pts": int(r.get("Pts", 0)),
            })
        if len(rows) == 4:
            groups[current_group] = rows
    return groups


# R32 match number receiving the third-placed team, keyed by the group whose
# WINNER it faces (matches the allocation table's "1A vs" column headers).
THIRD_SLOT_BY_WINNER_GROUP = {
    "A": 79, "B": 85, "D": 81, "E": 74, "G": 82, "I": 77, "K": 87, "L": 80,
}


def parse_third_place_allocation(knockout_html: str) -> list[dict]:
    """The official 495-combination allocation table.

    Returns [{"combo": "ABCDEFGH", "assign": {"79": "C", ...}}, ...] where
    combo is the sorted set of groups whose thirds advance and assign maps
    R32 match number -> group letter of the third-placed team placed there.
    """
    tables = pd.read_html(StringIO(knockout_html))
    alloc_df = None
    for t in tables:
        if len(t) >= 400 and any("1A vs" in str(c) for c in t.columns):
            alloc_df = t
            break
    if alloc_df is None:
        raise RuntimeError("third-place allocation table not found")
    slot_cols = {}
    for c in alloc_df.columns:
        m = re.match(r"^1([A-L]) vs", str(c))
        if m:
            slot_cols[c] = THIRD_SLOT_BY_WINNER_GROUP[m.group(1)]
    out = []
    for _, row in alloc_df.iterrows():
        assign = {}
        combo = []
        for col, match_no in slot_cols.items():
            v = str(row[col]).strip()
            m = re.match(r"^3([A-L])$", v)
            if not m:
                break
            assign[str(match_no)] = m.group(1)
            combo.append(m.group(1))
        if len(assign) == 8:
            out.append({"combo": "".join(sorted(combo)), "assign": assign})
    if len(out) != 495:
        raise RuntimeError(f"expected 495 combos, got {len(out)}")
    return out


def parse_squads(html: str) -> dict[str, list[dict]]:
    """Country -> 26 players with number, position, name, age, caps, goals, club."""
    soup = BeautifulSoup(html, "lxml")
    squads: dict[str, list[dict]] = {}
    current_team = None
    for el in soup.find_all(["h2", "h3", "h4", "table"]):
        if el.name != "table":
            t = _text(el)
            if t and not t.startswith("Group") and len(t) < 40:
                current_team = canonical(t)
            continue
        header = _text(el.find("tr"))
        if not header.startswith("No.") or current_team is None:
            continue
        rows = []
        for tr in el.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) < 7:
                continue
            txts = [_text(c) for c in cells]
            try:
                no = int(re.sub(r"\D", "", txts[0]) or 0)
            except ValueError:
                no = 0
            pos = re.sub(r"^\d+\s*", "", txts[1])
            dob_age = txts[3]
            age_m = re.search(r"aged?\s+(\d+)", dob_age)
            age = int(age_m.group(1)) if age_m else None
            try:
                caps = int(txts[4])
            except ValueError:
                caps = 0
            try:
                goals = int(txts[5])
            except ValueError:
                goals = 0
            rows.append({
                "no": no, "pos": pos, "name": txts[2], "age": age,
                "caps": caps, "goals": goals, "club": txts[6],
            })
        if rows and current_team not in squads:
            squads[current_team] = rows
    return squads
