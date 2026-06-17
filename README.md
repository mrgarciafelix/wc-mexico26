# WC Mexico 26 — Edge Finder (live dashboard)

Published static build of the 2026 World Cup betting-edge dashboard.

- **Best Bets** — a growth-optimal staking plan (value singles + the best
  independent-leg parlay) computed live in your browser from the model's
  probabilities and your bookmaker's odds. Set bankroll + risk, type your
  prices, and the plan re-optimizes instantly. Odds are saved on your device.
- **Title Race / Matches / Groups** — the model's tournament forecast.

The numbers come from `data/snapshot.json`, exported from the prediction engine
(Elo on 49k internationals + market value + form → calibrated Poisson →
Monte Carlo). Regenerate it with `python -m scripts.export_static` in the main
project and commit the updated file to refresh the site.

**Research / entertainment only. No model guarantees profit. Bet responsibly. 18+.**
