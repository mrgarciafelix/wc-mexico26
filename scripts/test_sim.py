"""Pipeline smoke test: Elo -> blended strengths -> Monte Carlo -> sanity print."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.config import SEED
from backend.elo import compute_base_elo
from backend.ratings import team_strengths
from backend.simulate import run_simulation

sys.stdout.reconfigure(encoding="utf-8")

teams = json.loads((SEED / "teams.json").read_text(encoding="utf-8"))
matches = json.loads((SEED / "matches.json").read_text(encoding="utf-8"))
alloc = json.loads((SEED / "third_place_alloc.json").read_text(encoding="utf-8"))

t0 = time.time()
state = compute_base_elo()
print(f"Elo computed in {time.time()-t0:.1f}s")
elo = {t["name"]: state.rating[t["name"]] for t in teams}
form = {t["name"]: state.form(t["name"]) for t in teams}
strengths = team_strengths(teams, elo, form, {})

print("\nTop 12 by blended strength:")
for n, s in sorted(strengths.items(), key=lambda kv: -kv[1]["strength"])[:12]:
    print(f"  {n:22s} {s['strength']:7.1f}  (elo {s['elo']:.0f}  mv {s['mv_adj']:+.0f}  form {s['form_adj']:+.0f})")

t0 = time.time()
res = run_simulation(teams, matches, alloc,
                     {k: v["strength"] for k, v in strengths.items()},
                     n_sims=20000, rng_seed=42)
print(f"\nSimulated 20000 tournaments in {time.time()-t0:.1f}s")
print("\nTitle race:")
rows = sorted(res["teams"].items(), key=lambda kv: -kv[1]["champion"])
for n, p in rows[:15]:
    print(f"  {n:22s} champ {p['champion']*100:5.1f}%  final {p['final']*100:5.1f}%  "
          f"SF {p['sf']*100:5.1f}%  R32 {p['r32']*100:5.1f}%")
total = sum(p["champion"] for _, p in rows)
print(f"\nSum of champion probs: {total:.4f} (should be 1.0)")
mex = res["teams"]["Mexico"]
print(f"Mexico (host, won opener): {json.dumps(mex)}")
