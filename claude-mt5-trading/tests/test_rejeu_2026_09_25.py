"""Rejeu de la semaine avec d'autres réglages (plan pro du 2026-09-25, point 6)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from tradinglab.research.replay import ReplayCandidate, compare, decide, load_candidates, simulate

PM = {"tp1_r": 1.5, "tp1_close_percent": 30, "tp2_r": 2.5, "tp2_close_percent": 40, "break_even_r": 1.0,
      "break_even_offset_r": 0.05, "trailing_start_r": 1.05, "trailing_atr_multiplier": 1.5}


def _bars(prix, t0="2026-09-25T10:05:00+00:00"):
    t = pd.date_range(t0, periods=len(prix), freq="5min", tz="UTC")
    return pd.DataFrame({"time": t, "open": prix, "high": [p + 0.0002 for p in prix],
                         "low": [p - 0.0002 for p in prix], "close": prix})


def _c(cout=0.1, failed=(), tp=1.1060):
    checks = {"07b_spread_vs_sl": ("07b_spread_vs_sl" not in failed, f"spread 5 % + commission 5 % = {cout * 100:.0f} % du risque (max 20 %)")}
    for f in failed:
        if f != "07b_spread_vs_sl":
            checks[f] = (False, "refus")
    return ReplayCandidate(ts="2026-09-25T10:00:00+00:00", key="k", symbol="EURUSD", side=1, agent_id="E05",
                           entry=1.1000, sl=1.0980, tp=tp, atr=0.0010, checks=checks, cost_ratio=cout)


def test_decision_selon_les_reglages():
    assert decide(_c(0.1), {"max_spread_sl_ratio": 0.2})
    assert not decide(_c(0.25), {"max_spread_sl_ratio": 0.2})                        # seuil plus strict
    assert decide(_c(0.25, failed=("07b_spread_vs_sl",)), {"max_spread_sl_ratio": 0.35})  # seuil plus large
    assert not decide(_c(0.1, failed=("08_news",)), {"max_spread_sl_ratio": 0.35})     # refus réel conservé
    assert decide(_c(0.1, failed=("06b_rr_live",)), {"gate_checks_disabled": ["06b_rr_live"]})
    assert not decide(_c(0.1), {"agents_exclus": ["E05"]})


def test_simulation_stop_tp_et_break_even():
    r, _ = simulate(_c(), _bars([1.0990, 1.0975]), PM)                     # stop plein
    assert abs(r + 1.0) < 1e-9
    r, _ = simulate(_c(), _bars([1.1010, 1.1030, 1.1062]), PM)             # TP1 à 1,5 R puis TP broker à 3 R
    assert abs(r - (0.3 * 1.5 + 0.7 * 3.0)) < 1e-6
    r, _ = simulate(_c(), _bars([1.1010, 1.1024, 1.0990]), PM)             # +1,2 R puis retour : sortie en bénéfice
    assert r > 0


def test_rejeu_compare_et_une_position_par_symbole(tmp_path):
    ev = []
    for i, cout in enumerate([0.10, 0.30]):
        ev.append({"ts_utc": f"2026-09-25T10:0{i}:00+00:00", "kind": "gate", "candidate_id": f"c{i}", "symbol": "EURUSD",
                   "approved": cout < 0.2, "key": f"k{i}", "agent_id": "E05", "side": "BUY", "entry": 1.1, "sl": 1.098,
                   "tp": 1.106, "atr": 0.001,
                   "checks": [{"name": "07b_spread_vs_sl", "ok": cout < 0.2, "detail": f"spread 5 % + commission 5 % = {cout * 100:.0f} % du risque (max 20 %)"}]})
    (tmp_path / "journal-2026-09-25.jsonl").write_text("\n".join(json.dumps(e) for e in ev), encoding="utf-8")
    cands = load_candidates(tmp_path, datetime(2026, 9, 25, tzinfo=timezone.utc), datetime(2026, 9, 26, tzinfo=timezone.utc))
    assert [c.cost_ratio for c in cands] == [0.10, 0.30]
    bars = _bars([1.0990, 1.0975], t0="2026-09-25T10:05:00+00:00")
    res = compare(cands, {"max_spread_sl_ratio": 0.2}, {"max_spread_sl_ratio": 0.35}, PM, PM, lambda s: bars)
    assert res["actuel"]["trades"] == 1 and res["propose"]["trades"] == 1      # le 2e attend que le symbole se libère
    assert res["actuel"]["total_r"] == -1.1                                    # -1 R et 10 % de coût d'entrée
