"""Freeze the live model into a static snapshot the frontend serves without a
backend (so the whole dashboard + bet optimizer runs on Netlify).

    .venv\\Scripts\\python.exe -m scripts.export_static

Writes frontend/data/snapshot.json — the exact object `/api/snapshot` returns,
so the published site and the local server behave identically. `markets` +
`sample_odds` let the client-side optimizer recompute the plan as the user
edits odds; no server needed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from backend import db as dbm
from backend.app import build_snapshot

# Default writes the local FastAPI copy; CI overrides to the repo-root data dir.
OUT = Path(os.environ.get("WC_SNAPSHOT_OUT")
           or (dbm.DB_PATH.parent.parent / "frontend" / "data" / "snapshot.json"))


def main() -> None:
    snap = build_snapshot()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snap, separators=(",", ":")), encoding="utf-8")
    plan, kb = snap["plan"], OUT.stat().st_size / 1024
    print(f"wrote {OUT}  ({kb:.0f} KB)")
    print(f"  teams={len(snap['teams'])} matches={len(snap['matches'])} "
          f"markets={len(snap['markets'])} sample_odds={len(snap['sample_odds'])}")
    print(f"  plan: {len(plan['singles'])} value singles, "
          f"{len(plan['parlays'])} parlays, "
          f"stake ${plan['summary']['singles_stake']}, "
          f"exp profit ${plan['summary']['singles_exp_profit']}")


if __name__ == "__main__":
    main()
