"""Deux trous constatés le 2026-09-21 entre le plan approuvé et ce qui arrive vraiment.

1. **Clôture pendant une coupure.** `PositionManager.sync()` détecte les positions disparues et les
   renvoie ; le cycle appelle `_on_position_closed` dessus, mais `startup()` jetait la liste. BTCUSD
   s'est fermée à +1 581 $ pendant un arrêt de 74 min : le gain est sur le compte, mais le trade
   n'existait ni dans `learning.db`, ni dans les statistiques de l'agent B08, ni dans le P&L du jour,
   ni dans le compteur de pertes consécutives. L'événement `recovery` affichait `bot_positions: []`
   sans rien dire de la position évaporée.

2. **Cible broker dégradée par le fill.** Le gate pose `tp_plan[-1]` comme TP broker, calculé sur
   l'entrée du candidat — le prix du *scan*. Quand le marché bouge avant l'envoi, la cible se retrouve
   à un multiple de R plus faible qu'approuvé :

       EURAUD  11:35:38   entrée prévue ~1,61004 → fill 1,61045   SL 1,609
               risque     prévu 623,99 $ → réel 795,79 $  (+28 %)
               cible      2,08 R au plan → 1,35 R réel

   `tp_plan` n'étant jamais relu par le position manager (il gère en multiples de R : TP1 1,5 R,
   TP2 2,5 R, runner), une cible tombée sous 1,5 R ferme **100 % de la position avant que l'échelle
   n'ait pu commencer**. Le gagnant à +1,14 R du 2026-09-21 a cette forme.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.types import Side  # noqa: E402

from test_position_watchdog_orchestrator import make_orch  # noqa: E402
from test_risk_and_gate import ctx_for, executor_for, gate_for, make_candidate  # noqa: E402


# --------------------------------------------------------- 1. clôture pendant une coupure
def _ouvrir_une_position(o, broker):
    """Fait avancer le marché simulé jusqu'à ce qu'une position soit réellement ouverte."""
    o.cycle(); o.cycle()
    for _ in range(10):
        if broker.positions(magic=51000):
            return broker.positions(magic=51000)[0]
        broker.advance_bars(12)
        broker.set_now(broker.now() + timedelta(hours=1))
        o.feed.invalidate()
        o.cycle()
    pytest.skip("aucune position ouverte par le marché simulé sur cette fenêtre")


def test_cloture_hors_ligne_passe_la_revue_post_trade(settings, broker):
    """Cœur du correctif 1 : fermée bot éteint, elle doit quand même être comptée au redémarrage."""
    o = make_orch(settings, broker)
    pos = _ouvrir_une_position(o, broker)
    fermes_avant = o.state.daily.trades_closed

    broker.close_position(pos.ticket, comment="fermeture pendant la coupure")   # bot éteint
    o2 = make_orch(settings, broker)                                            # redémarrage

    revues = [e for e in o2.journal.read_day(kinds={"post_trade_review"}) if e.get("ticket") == pos.ticket]
    assert revues, "la position fermée hors ligne doit passer la revue post-trade au redémarrage"
    assert o2.state.daily.trades_closed > fermes_avant, "elle doit compter dans les trades du jour"
    assert str(pos.ticket) not in o2.state.bot_positions


def test_recovery_nomme_les_positions_fermees_hors_ligne(settings, broker):
    """`recovery` disait `bot_positions: []` sans jamais nommer ce qui avait disparu."""
    o = make_orch(settings, broker)
    pos = _ouvrir_une_position(o, broker)
    broker.close_position(pos.ticket)

    o2 = make_orch(settings, broker)
    rec = [e for e in o2.journal.read_day(kinds={"recovery"}) if pos.ticket in (e.get("closed_offline") or [])]
    assert rec, "le ticket fermé hors ligne doit apparaître dans l'événement recovery"


def test_aucune_cloture_hors_ligne_ne_declenche_rien(settings, broker):
    """Borne : un redémarrage sans fermeture ne doit inventer aucune revue."""
    o = make_orch(settings, broker)
    _ouvrir_une_position(o, broker)
    avant = len(o.journal.read_day(kinds={"post_trade_review"}))

    o2 = make_orch(settings, broker)
    assert len(o2.journal.read_day(kinds={"post_trade_review"})) == avant


# --------------------------------------------------------- 2. cible broker après le fill
def _executer_avec_glissement(settings, broker, tmp_path, glissement: float):
    """Reproduit la cause réelle : le marché bouge entre le scan (candidat) et l'envoi de l'ordre."""
    gate, _ = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    c, atr = make_candidate(broker, rr=2.5, sl_atr=1.5)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    assert res.approved and req.tp > 0
    tp_demande = req.tp

    broker.set_price("EURUSD", broker.tick("EURUSD").bid + glissement)
    out = ex.execute(c, res, req, 250.0, 0.25)
    assert out.executed
    pos = broker.position(out.position.ticket)
    rr_approuve = abs(c.tp_plan[-1] - c.entry) / c.sl_distance
    return pos, tp_demande, rr_approuve


def test_fill_defavorable_restaure_le_r_approuve(settings, broker, tmp_path):
    """Cœur du correctif 2 : la cible retrouve le multiple de R que le gate avait approuvé."""
    pos, tp_demande, rr_approuve = _executer_avec_glissement(settings, broker, tmp_path, +0.0008)

    rr_avant = abs(tp_demande - pos.price_open) / abs(pos.price_open - pos.sl)
    assert rr_avant < 1.5, "le glissement doit bien avoir fait tomber la cible sous TP1"

    rr_apres = abs(pos.tp - pos.price_open) / abs(pos.price_open - pos.sl)
    assert rr_apres == pytest.approx(rr_approuve, abs=0.02)
    assert pos.tp > tp_demande, "sur un achat, la cible ne peut que s'éloigner"


def test_la_cible_ne_passe_jamais_sous_tp1(settings, broker, tmp_path):
    """Conséquence utile : l'échelle TP1/TP2/runner ne peut plus être court-circuitée."""
    pos, _, _ = _executer_avec_glissement(settings, broker, tmp_path, +0.0008)
    rr = abs(pos.tp - pos.price_open) / abs(pos.price_open - pos.sl)
    assert rr >= 1.5, "une cible sous 1,5 R fermerait tout avant le premier TP partiel"


def test_le_stop_n_est_pas_touche(settings, broker, tmp_path):
    """Seule la sortie est corrigée : le risque reste exactement celui qui a été mesuré."""
    gate, _ = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    c, atr = make_candidate(broker, rr=2.5, sl_atr=1.5)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    broker.set_price("EURUSD", broker.tick("EURUSD").bid + 0.0008)
    out = ex.execute(c, res, req, 250.0, 0.25)
    assert broker.position(out.position.ticket).sl == pytest.approx(req.sl)


def test_fill_favorable_laisse_la_cible_intacte(settings, broker, tmp_path):
    """Contre-exemple : un fill meilleur que prévu améliore le R, on n'y touche pas."""
    pos, tp_demande, _ = _executer_avec_glissement(settings, broker, tmp_path, -0.0005)
    assert pos.tp == pytest.approx(tp_demande), "la cible ne doit jamais être rapprochée"


def test_sans_glissement_aucune_modification(settings, broker, tmp_path):
    """Le cas courant ne doit produire ni appel broker ni bruit dans le journal."""
    pos, tp_demande, _ = _executer_avec_glissement(settings, broker, tmp_path, 0.0)
    assert pos.tp == pytest.approx(tp_demande)


def test_la_correction_est_journalisee(settings, broker, tmp_path):
    """Une cible déplacée doit être traçable, avec les deux R."""
    gate, _ = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    c, atr = make_candidate(broker, rr=2.5, sl_atr=1.5)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    broker.set_price("EURUSD", broker.tick("EURUSD").bid + 0.0008)
    ex.execute(c, res, req, 250.0, 0.25)

    msgs = [e for e in ex.journal.read_day() if "cible broker" in str(e.get("message", ""))]
    assert msgs, "le déplacement de cible doit laisser une trace"
    assert msgs[-1]["rr_reel"] < msgs[-1]["rr_approuve"]
    # l'adaptateur peut muter la Position en place : relever `ancienne` après coup afficherait
    # deux fois la même valeur et rendrait la trace inutilisable.
    assert msgs[-1]["ancienne"] != msgs[-1]["nouvelle"], "l'ancienne cible doit être celle d'avant l'appel"


def test_vente_la_cible_descend(settings, broker, tmp_path):
    """Symétrie : sur une vente, un fill défavorable abaisse la cible au lieu de la monter."""
    gate, _ = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    c, atr = make_candidate(broker, side=Side.SELL, rr=2.5, sl_atr=1.5)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    assert res.approved
    tp_demande = req.tp
    broker.set_price("EURUSD", broker.tick("EURUSD").bid - 0.0008)      # défavorable pour une vente
    out = ex.execute(c, res, req, 250.0, 0.25)

    pos = broker.position(out.position.ticket)
    rr = abs(pos.tp - pos.price_open) / abs(pos.price_open - pos.sl)
    assert pos.tp < tp_demande and rr == pytest.approx(2.5, abs=0.02)
