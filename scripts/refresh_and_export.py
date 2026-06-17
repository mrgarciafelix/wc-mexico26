"""Scrape the latest results, recompute the model, and export the static
snapshot in one shot. Used by publish.ps1 and the scheduled CI job.

    .venv\\Scripts\\python.exe -m scripts.refresh_and_export

Safety guard: if the scrape comes back with *fewer* played matches than the
snapshot we already published (e.g. a flaky/blocked fetch in CI), we keep the
good snapshot instead of regressing the live site.
"""
from __future__ import annotations

import json

from backend import db as dbm
from backend import odds_api
from backend import updater
from scripts.export_static import OUT, main as export_snapshot


def main() -> None:
    con = dbm.connect()
    new_played = None
    try:
        dbm.init_db(con)
        res = updater.run_update(con, trigger="publish", sync_wiki=True)
        changes = res.get("changes", []) if isinstance(res, dict) else []
        new_played = con.execute(
            "SELECT COUNT(*) c FROM matches WHERE home_score IS NOT NULL"
        ).fetchone()["c"]
        print(f"update: run {res.get('run_id')} · {len(changes)} changes · "
              f"{res.get('n_sims')} sims · {new_played} played")
        odds_api.sync(con)                      # live bookmaker odds (cached/rate-limited)
    except Exception as e:
        print(f"update warning: {e} (keeping last good state)")
    finally:
        con.close()

    if OUT.exists() and new_played is not None:
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8"))["meta"]["matches_played"]
            if new_played < prev:
                print(f"SKIP export: scrape regressed ({new_played} < {prev} "
                      f"played) — keeping the current snapshot.")
                return
        except Exception:
            pass
    export_snapshot()


if __name__ == "__main__":
    main()
