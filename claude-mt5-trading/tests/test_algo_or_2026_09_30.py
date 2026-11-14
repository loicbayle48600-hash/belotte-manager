"""2026-09-30, demande utilisateur : « essaie de faire un algorithme pour l'or ».

Cassure du range asiatique (00-07 UTC) en H1, seulement dans le sens de la tendance D1 (option `trend_only` du screener
`session_breakout`). En live le seuil de score (58, +15 si aligné, seuil 65) écartait déjà la contre-tendance ; le shadow
n'a pas de seuil de score, d'où l'option explicite. Les signaux « tendance seule » sont un SOUS-ENSEMBLE des autres, de
même sens."""
from __future__ import annotations

from tradinglab.research.adapters import make_signal_fn

from test_fastsig_2026_09_29 import SPEC_SYM, _spec, _synth

REG = ["TRENDING", "RANGING", "BREAKOUT", "HIGH_VOLATILITY", "LOW_VOLATILITY", "UNCERTAIN", "RISK_ON", "RISK_OFF"]


def _signaux(trend_only: bool, df):
    spec = _spec("session_breakout", {"start": "00:00", "end": "07:00", "sl_atr": 3.0, "rr": 2.0, "trend_only": trend_only},
                 "H1", "D1", sessions=["LONDON", "OVERLAP_LDN_NY"], regimes=REG)
    fn = make_signal_fn(spec, SPEC_SYM, "H1")
    fn.prepare(df)
    assert fn.data_error is None, fn.data_error
    out = {}
    for i in range(fn.required_bars, len(df)):
        s = fn(df.iloc[: i + 1])
        if s is not None:
            out[i] = s.side
    return out


def test_tendance_seule_est_un_sous_ensemble_de_meme_sens():
    df = _synth(2600, 21, "1h")
    tous = _signaux(False, df)
    tendance = _signaux(True, df)
    assert tous, "la cassure asiatique doit produire des signaux sur ces données"
    assert set(tendance) <= set(tous)
    assert all(tous[i] == tendance[i] for i in tendance)
    assert len(tendance) < len(tous), "le filtre doit écarter les cassures à contre-tendance"


def test_agent_or01_propose_en_shadow():
    import json
    from pathlib import Path

    from tradinglab.learning.shadow_board import FAMILLES

    assert FAMILLES.get("OR") == "or (algorithme dédié)"
    f = Path(__file__).resolve().parents[1] / "config" / "agents_or.json"
    d = json.loads(f.read_text(encoding="utf-8"))
    assert d["agent_id"] == "OR01" and d["status"] == "SHADOW" and d["markets"] == ["XAUUSD"]
    assert d["params"]["trend_only"] is True and d["timeframes"] == {"entry": "H1", "trend": "D1"}
