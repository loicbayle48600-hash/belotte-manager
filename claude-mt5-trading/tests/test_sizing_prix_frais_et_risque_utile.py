"""Deux corrections du gate décidées le 2026-09-21 après mesure sur le compte réel.

**1. La taille se calcule sur le prix courant, plus sur celui du scan.**

`ExecutionGate` dimensionnait la position avec `candidate.entry`, relevé au moment du scan. Les
screeners signalent sur du mouvement : le prix continue dans le sens du trade avant l'envoi, l'entrée
se dégrade, et la distance réelle au SL dépasse celle qui a servi au calcul. Comme `06_fresh_price`
tolère jusqu'à 0,5 ATR de dérive, un SL à 1,5 ATR encaissait déjà +33 % de risque, bien plus s'il
était serré. Cinq fills mesurés dans la journée, tous au-dessus du plan, aucun en dessous :

    02:15:06   260,48 -> 348,60   (+34 %)
    11:35:38   623,99 -> 795,79   (+28 %)
    11:44:20    12,10 ->  19,60   (+62 %)
    11:59:40   623,23 -> 1138,95  (+83 %)
    12:15:06   622,73 -> 783,58   (+26 %)

Un biais à sens unique n'est pas du bruit. Conséquence portefeuille : 0,55 % de risque ouvert là où
le gate croyait en avoir approuvé 0,375 %, donc des plafonds de concentration appliqués sur des
chiffres faux.

**2. Une position rabotée au point de ne plus rien peser est refusée.**

`volume_max` peut réduire la taille à presque rien. XRPUSD a été pris deux fois à 11,52 $ et 12,10 $
de risque sur un compte de 500 000 $ (0,0023 %), en occupant un emplacement et le quota d'une
position par symbole. Le refus n'est pas de la prudence, c'est de l'utilité : un pari qui ne peut
rien changer au compte n'a pas sa place. Le plancher est un **ratio** de `risk_per_trade_percent` et
non une valeur absolue, pour ne pas reproduire la dérive des plafonds de corrélation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.risk.risk_manager import compute_volume  # noqa: E402

from test_risk_and_gate import ctx_for, gate_for, make_candidate, make_state  # noqa: E402

ATR = 0.0012
DERIVE = 0.0005          # sous la tolérance de 0,5 ATR du contrôle 06_fresh_price


def risque_cible(settings) -> float:
    """Risque par trade CONFIGURÉ : le coder en dur ferait échouer ces tests au moindre ajustement du
    budget (0,125 → 0,05 % le 2026-09-23 en mode test), alors qu'aucune règle n'est violée."""
    return float(settings.risk["risk_per_trade_percent"])


def _checks(res):
    return {c.name: c for c in res.checks}


# ------------------------------------------------------- 1. sizing sur le prix courant
def test_la_taille_suit_le_prix_courant(settings, broker):
    """Cœur du correctif : le volume est celui qu'impose la distance réelle au SL."""
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker, sl_atr=1.5)
    spec = broker.specs["EURUSD"]
    broker.set_price("EURUSD", broker.tick("EURUSD").bid + DERIVE)      # le marché a avancé

    res, req = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    px = broker.tick("EURUSD").ask
    attendu = compute_volume(res_equity(res, broker), risque_cible(settings), px, c.sl, spec, 0.35)
    perime = compute_volume(res_equity(res, broker), risque_cible(settings), c.entry, c.sl, spec, 0.35)

    assert perime.volume > attendu.volume, "le prix périmé sous-estime la distance au SL"
    assert req.volume == pytest.approx(attendu.volume), "le gate doit dimensionner sur le tick"


def res_equity(res, broker):
    return broker.account_info().equity


def test_le_risque_reel_retombe_sur_la_cible(settings, broker):
    """Ce que le correctif achète : plus d'inflation du risque due à la dérive."""
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker, sl_atr=1.5)
    spec = broker.specs["EURUSD"]
    broker.set_price("EURUSD", broker.tick("EURUSD").bid + DERIVE)

    res, req = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    px = broker.tick("EURUSD").ask
    equity = broker.account_info().equity

    reel = compute_volume(equity, risque_cible(settings), px, c.sl, spec, 0.35)
    assert reel.risk_percent_effective == pytest.approx(risque_cible(settings), abs=0.01)

    # Les deux volumes affrontent la même distance réelle |px − sl| une fois la position ouverte :
    # le rapport des volumes EST donc le rapport des risques encourus.
    ancien = compute_volume(equity, risque_cible(settings), c.entry, c.sl, spec, 0.35)
    inflation = ancien.volume / reel.volume
    assert inflation > 1.2, "l'ancien dimensionnement gonflait le risque d'au moins 20 %"
    assert req.volume <= ancien.volume


def test_sans_derive_le_volume_ne_change_pas(settings, broker):
    """Le cas courant doit rester identique : aucun effet de bord."""
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker, sl_atr=1.5)
    spec = broker.specs["EURUSD"]
    res, req = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    attendu = compute_volume(broker.account_info().equity, risque_cible(settings), c.entry, c.sl, spec, 0.35)
    assert res.approved and req.volume == pytest.approx(attendu.volume)


def test_derive_favorable_reduit_aussi_le_risque(settings, broker):
    """Symétrie : un prix qui s'éloigne du SL permet un volume plus grand, pas un risque plus grand."""
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker, sl_atr=1.5)
    broker.set_price("EURUSD", broker.tick("EURUSD").bid - DERIVE)      # entrée meilleure que prévu
    res, req = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    if res.approved:
        perime = compute_volume(broker.account_info().equity, risque_cible(settings), c.entry, c.sl,
                                broker.specs["EURUSD"], 0.35)
        assert req.volume >= perime.volume


# ------------------------------------------------------- 2. plancher de risque utile
def test_position_rabotee_a_rien_est_refusee(settings, broker):
    """Le cas XRPUSD : volume_max réduit la position à une poussière, le gate refuse."""
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    spec = broker.specs["EURUSD"]
    spec.volume_max = spec.volume_min                       # plafond au minimum : risque quasi nul

    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    chk = _checks(res)["11b_risque_utile"]
    assert not chk.ok and not res.approved
    assert "plancher" in chk.detail


def test_une_taille_normale_passe_le_plancher(settings, broker):
    """Contre-exemple : sans écrêtage, le contrôle ne doit rien signaler."""
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    assert _checks(res)["11b_risque_utile"].ok


def test_le_plancher_suit_le_risque_par_trade(settings):
    """Ratio et non valeur absolue : un ajustement du risque par trade déplace le plancher avec lui."""
    from tradinglab.risk.risk_manager import RiskLimits

    L = RiskLimits.from_config(settings.risk)
    assert 0.0 < L.min_effective_risk_ratio < 1.0
    # le plancher est un RATIO : il suit le risque par trade sans être réécrit. Vérifier une valeur
    # absolue figerait le test à une taille de lot donnée (0,125 % avant le 2026-09-23, 0,05 % depuis).
    plancher = L.min_effective_risk_ratio * L.risk_per_trade_percent
    assert plancher == pytest.approx(0.2 * L.risk_per_trade_percent)
    assert 0.0 < plancher < L.risk_per_trade_percent, "un plancher ≥ la cible refuserait tout trade"


def test_le_plancher_est_bien_lu_depuis_la_config(settings):
    assert "min_effective_risk_ratio" in settings.risk, "le plancher doit être explicite dans risk.yaml"


def test_le_plancher_laisse_passer_un_ecretage_modere(settings, broker):
    """SOLUSD à 0,0296 % et ETHUSD à 0,052 % restent des paris utiles : on ne les refuse pas."""
    from tradinglab.risk.risk_manager import RiskLimits

    L = RiskLimits.from_config(settings.risk)
    # cas mesurés quand la cible était 0,125 % : on raisonne en FRACTION de la cible, seule grandeur que le
    # plancher (un ratio) compare — 0,0296/0,125 = 24 %, 0,052/0,125 = 42 %, 0,0023/0,125 = 1,8 %
    for mesure in (0.0296, 0.052):
        assert mesure / 0.125 > L.min_effective_risk_ratio
    assert 0.0023 / 0.125 < L.min_effective_risk_ratio, "le cas XRPUSD doit bien tomber sous le plancher"
