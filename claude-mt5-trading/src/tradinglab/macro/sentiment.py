"""Sentiment de risque macro, déterministe, calculé à partir de rendements récents.

Aucune donnée n'est inventée : si les rendements nécessaires manquent (ou sont
NaN), le label est ``UNKNOWN``. Le résultat est ``CALCULATED`` (règle fixe),
pas une interprétation de modèle.
"""
from __future__ import annotations

import math
from typing import Optional

# Racines de symboles considérées comme « indices actions » (ordre déterministe).
INDEX_ROOTS = ("US500", "SPX500", "SP500", "NAS100", "USTEC", "NDX100", "US30", "DJ30", "GER40", "DE40", "DAX40", "UK100", "JP225", "EU50")
GOLD_ROOTS = ("XAUUSD", "GOLD", "XAU")
RISK_FX_ROOTS = ("AUDJPY",)

INDEX_ON_THRESHOLD = 0.3     # %
INDEX_OFF_THRESHOLD = -0.3   # %
GOLD_OFF_THRESHOLD = 0.5     # %


def _clean(v: object) -> Optional[float]:
    """float fini ou None (bool, NaN, None, texte → None)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _lookup(returns: dict[str, float], roots: tuple[str, ...]) -> dict[str, float]:
    """Sélectionne les rendements dont la racine (sans suffixe broker) figure dans ``roots``."""
    norm = {str(k).upper().split(".")[0].split("_")[0]: v for k, v in (returns or {}).items()}
    out: dict[str, float] = {}
    for r in roots:
        if r in norm:
            f = _clean(norm[r])
            if f is not None:
                out[r] = f
    return out


def risk_sentiment(returns: dict[str, float]) -> tuple[str, dict]:
    """Retourne ``(label, features)`` avec label ∈ {RISK_ON, RISK_OFF, NEUTRAL, UNKNOWN}.

    Règles (rendements en %) :
    - RISK_ON  : moyenne des indices > +0.3 % ET AUDJPY > 0 ;
    - RISK_OFF : moyenne des indices < -0.3 % OU (XAUUSD > +0.5 % ET indices < 0) ;
    - NEUTRAL  : sinon ;
    - UNKNOWN  : aucun indice disponible, ou AUDJPY manquant alors que les
      indices seuls suggéreraient RISK_ON (on ne confirme pas sans la donnée).

    ``features`` documente les valeurs utilisées et la règle déclenchée.
    """
    indices = _lookup(returns, INDEX_ROOTS)
    gold = _lookup(returns, GOLD_ROOTS)
    fx = _lookup(returns, RISK_FX_ROOTS)

    idx_mean: Optional[float] = (sum(indices.values()) / len(indices)) if indices else None
    gold_ret: Optional[float] = next(iter(gold.values()), None)
    audjpy: Optional[float] = fx.get("AUDJPY")

    features: dict = {
        "indices_used": sorted(indices.keys()),
        "index_return_mean": idx_mean,
        "gold_return": gold_ret,
        "audjpy_return": audjpy,
        "thresholds": {
            "index_on": INDEX_ON_THRESHOLD,
            "index_off": INDEX_OFF_THRESHOLD,
            "gold_off": GOLD_OFF_THRESHOLD,
        },
        "rule": "",
        "provenance": "CALCULATED",
    }

    if idx_mean is None:
        features["rule"] = "missing_indices"
        return "UNKNOWN", features

    if idx_mean < INDEX_OFF_THRESHOLD:
        features["rule"] = "indices_below_off_threshold"
        return "RISK_OFF", features
    if gold_ret is not None and gold_ret > GOLD_OFF_THRESHOLD and idx_mean < 0:
        features["rule"] = "gold_up_indices_down"
        return "RISK_OFF", features

    if idx_mean > INDEX_ON_THRESHOLD:
        if audjpy is None:
            features["rule"] = "missing_audjpy_for_risk_on"
            return "UNKNOWN", features
        if audjpy > 0:
            features["rule"] = "indices_up_audjpy_up"
            return "RISK_ON", features

    features["rule"] = "no_rule_triggered"
    return "NEUTRAL", features


__all__ = ["risk_sentiment", "INDEX_ROOTS", "GOLD_ROOTS", "RISK_FX_ROOTS"]
