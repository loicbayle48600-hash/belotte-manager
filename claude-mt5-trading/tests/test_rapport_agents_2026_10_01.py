"""2026-10-01, décision utilisateur (« 5 oui ») : le rapport de 17 h New York liste les agents prêts pour le live et
ceux à remettre en shadow ; le texte du rapport est protégé pour Telegram."""
from __future__ import annotations

import json

from tradinglab.learning.shadow_board import lignes_rapport_agents
from tradinglab.learning.store import LearningStore, TradeRecord
from tradinglab.monitoring import telegram_notifier as tn


def _t(store, agent, mode, rs, base):
    for i, r in enumerate(rs):
        store.record_trade(TradeRecord(ticket=base + i, agent_id=agent, symbol="EURUSD", side="BUY", entry=1.0, sl=0.99,
                                       risk_money=100, risk_percent=0.1, result_r=r, pnl=100 * r, mode=mode,
                                       opened_at="2026-10-01T08:00:00+00:00", closed_at="2026-10-01T09:00:00+00:00",
                                       exit_reason="sl"))


def test_prets_et_a_remettre_en_shadow(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "reports").mkdir()
    st = LearningStore(tmp_path / "data" / "learning.db")
    _t(st, "SH1", "shadow", [2.0, -1.0] * 12, 0)                  # 24 trades, PF 2,0 : prêt
    _t(st, "SH2", "shadow", [1.0, -1.0] * 12, 100)                # PF 1,0 : pas prêt
    _t(st, "LV1", "live", [1.0, -1.0, -1.0] * 4, 200)             # 12 trades réels, PF 0,5 : à remettre en shadow
    _t(st, "LV2", "live", [1.0, -1.0] * 6, 300)                   # PF 1,0 : garde
    (tmp_path / "data" / "agent_status.json").write_text(json.dumps(
        {"status": {"SH1": "SHADOW", "SH2": "SHADOW", "LV1": "LIVE", "LV2": "LIVE"}}), encoding="utf-8")
    lignes = lignes_rapport_agents(tmp_path, {})
    assert lignes[0].startswith("Prêts pour le live") and "SH1" in lignes[0] and "SH2" not in lignes[0]
    assert lignes[1].startswith("À remettre en shadow") and "LV1" in lignes[1] and "LV2" not in lignes[1]
    assert not any("<" in l or "&" in l for l in lignes)


def test_rapport_protege_pour_telegram():
    t = tn.format_event({"kind": "report_day", "text": "écart : volume < minimum & spread"})
    assert "&lt;" in t and "&amp;" in t and "<b>Rapport du jour</b>" in t
