"""Value detection and stake sizing (fractional Kelly)."""
from __future__ import annotations


def implied_prob(decimal_odds: float) -> float:
    return 1.0 / decimal_odds if decimal_odds > 1 else 1.0


def edge(model_p: float, decimal_odds: float) -> float:
    """Expected value per unit staked."""
    return model_p * decimal_odds - 1.0


def kelly_fraction(model_p: float, decimal_odds: float) -> float:
    """Full-Kelly fraction of bankroll; 0 if no edge."""
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    f = (model_p * b - (1.0 - model_p)) / b
    return max(0.0, f)


def evaluate(model_p: float, decimal_odds: float, bankroll: float,
             kelly_mult: float) -> dict:
    f = kelly_fraction(model_p, decimal_odds)
    return {
        "model_p": round(model_p, 4),
        "implied_p": round(implied_prob(decimal_odds), 4),
        "fair_odds": round(1.0 / model_p, 2) if model_p > 0 else None,
        "edge": round(edge(model_p, decimal_odds), 4),
        "kelly_full": round(f, 4),
        "stake": round(bankroll * kelly_mult * f, 2),
    }
