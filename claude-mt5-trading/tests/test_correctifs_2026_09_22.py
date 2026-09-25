"""Deux correctifs approuvés par l'utilisateur le 2026-09-22, chacun motivé par une perte mesurée.

1. `07b_spread_vs_sl` — le spread ne peut pas dévorer le risque du trade. Le contrôle `07_spread`
   (spread / ATR) ne voyait pas le cas d'un stop bien plus serré que l'ATR : mesuré sur le journal,
   GBPTRY proposait un spread médian de 4 126 points pour un stop médian de 38 points (108x).
2. `break_even_requires_structure: false` — l'exigence de structure neutralisait le break-even à 1,0 R
   tant que TP1 (1,5 R) n'était pas pris : 4 trades sur 54 sont montés au-delà de 1 R puis revenus au
   stop plein, -1 895 $ (dont CADCHF -1,17 R après un pic à +1,11 R).
"""
from __future__ import annotations

import pandas as pd
import pytest

from tradinglab.core.types import Side
from tests.test_position_watchdog_orchestrator import open_buy, pm_for
from tests.test_risk_and_gate import ctx_for, gate_for, make_candidate, make_state


# ---------------------------------------------------------------- 1. spread vs risque
def test_spread_superieur_au_risque_refuse(settings, broker):
    """Stop serré + spread large → refus, alors que le contrôle spread/ATR passe encore."""
    gate, _ = gate_for(settings, broker)
    st = make_state()
    c, atr = make_candidate(broker, "EURUSD", Side.BUY)
    tick = broker.tick("EURUSD")
    spec = broker.symbol_info("EURUSD")
    # stop à 1,5x le spread : le coût d'entrée vaut 67 % du risque
    dist = tick.spread_points(spec) * spec.point * 1.5
    c.sl = c.entry - dist
    c.tp_plan = [c.entry + 3 * dist]
    res, _ = gate.evaluate(ctx_for(broker, c, st, atr, correlations=pd.DataFrame()))
    checks = {ch.name: ch for ch in res.checks}
    assert checks["07b_spread_vs_sl"].ok is False and "% du risque" in checks["07b_spread_vs_sl"].detail
    assert checks["07_spread"].ok is True, "le contrôle spread/ATR ne voit pas ce cas : c'est bien 07b qui protège"
    assert not res.approved


def test_spread_normal_accepte(settings, broker):
    """Un stop normal (plusieurs fois le spread) passe : le contrôle ne bloque pas le trading courant."""
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker, "EURUSD", Side.BUY)
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr, correlations=pd.DataFrame()))
    checks = {ch.name: ch for ch in res.checks}
    assert checks["07b_spread_vs_sl"].ok is True
    assert res.approved, [ch.detail for ch in res.checks if not ch.ok]


def test_spread_vs_sl_refuse_sans_repere(settings, broker):
    """Tick absent → refus (jamais d'hypothèse favorable), comme le reste du gate."""
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker, "EURUSD", Side.BUY)
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr, tick=None, correlations=pd.DataFrame()))
    assert {ch.name: ch.ok for ch in res.checks}["07b_spread_vs_sl"] is False


def test_seuil_present_en_config(settings):
    assert float(settings.execution["max_spread_sl_ratio"]) == 0.20      # 0.35 → 0.20 le 2026-09-25 (accord utilisateur)
    assert settings.profit_management["break_even_requires_structure"] is False
    assert float(settings.profit_management["break_even_r"]) == 1.0


# ---------------------------------------------------------------- 2. break-even sans exigence de structure
def test_break_even_arme_sans_structure(broker, tmp_path):
    """Cas CADCHF : +1,11 R sans structure confirmée et sans TP1 → le stop passe quand même au break-even."""
    from tradinglab.execution.position_manager import MarketContext, PMConfig

    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    pm.cfg = PMConfig(break_even_r=1.0, break_even_requires_structure=False)
    broker.set_price("EURUSD", pos.price_open + 1.11 * dist)
    pos = broker.position(pos.ticket)
    acts = pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))
    assert plan.break_even_done and any("break-even" in a for a in acts), acts
    assert not plan.tp1_done, "aucun partiel n'est nécessaire pour armer le break-even"
    assert broker.position(pos.ticket).sl >= plan.entry, "le stop est remonté à l'entrée au moins"


def test_break_even_ne_bouge_pas_sous_le_seuil(broker, tmp_path):
    """Garde-fou intact : sous 1,0 R rien ne bouge, et le stop ne s'élargit jamais."""
    from tradinglab.execution.position_manager import MarketContext, PMConfig

    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    pm.cfg = PMConfig(break_even_r=1.0, break_even_requires_structure=False)
    sl_avant = broker.position(pos.ticket).sl
    broker.set_price("EURUSD", pos.price_open + 0.6 * dist)
    pos = broker.position(pos.ticket)
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))
    assert not plan.break_even_done and broker.position(pos.ticket).sl == sl_avant


# ---------------------------------------------------------------- 3. secours terminal Claude (2026-09-23)
def test_bascule_terminal_quand_credit_api_epuise(settings, home):
    """« Quand il n'y a plus de crédit API, continuer dans un terminal Claude » : l'appel qui échoue faute
    de crédit repart par le backend de secours, sans perdre la réponse ni redémarrer le bot."""
    from types import SimpleNamespace

    from tradinglab.core.journal import Journal
    from tradinglab.core.state import SystemState
    from tradinglab.models.client import LLMClient, _is_credit_exhausted
    from tradinglab.models.router import ModelRouter

    class _NoCredit:
        def create(self, **kw):
            raise RuntimeError("Error code: 400 - Your credit balance is too low to access the Anthropic API")

    class _Terminal:
        def __init__(self):
            self.calls = 0

        def create(self, **kw):
            self.calls += 1
            return SimpleNamespace(content=[SimpleNamespace(text='{"ok": true}')], stop_reason="end_turn",
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=5), cost_usd=0.17)

    term = _Terminal()
    router = ModelRouter(settings.models)
    router.available_models = ["claude-opus-5"]
    st = SystemState()
    journal = Journal(home / "logs")
    cl = LLMClient(router, st, sdk_client=SimpleNamespace(messages=_NoCredit()), journal=journal,
                   fallback_factory=lambda: SimpleNamespace(messages=term))
    r = cl.complete("bull_thesis", "sys", "u", cache_key="k")
    assert r is not None and r.json() == {"ok": True} and term.calls == 1
    assert cl.using_fallback and r.cost_usd == pytest.approx(0.17)   # coût annoncé par le harnais, pas le barème
    # la bascule est journalisée et définitive pour la session (le crédit ne revient pas en cours de route)
    from datetime import datetime, timezone
    msgs = [e for e in journal.read_day(datetime.now(timezone.utc)) if "crédit API" in str(e.get("message", ""))]
    assert msgs, "la bascule doit laisser une trace dans le journal"
    assert cl.complete("bear_thesis", "sys", "u2", cache_key="k2") is not None and term.calls == 2


def test_bascule_seulement_sur_un_probleme_de_credit(settings, home):
    """Un timeout ou une panne réseau ne doit PAS changer de backend : repli déterministe, comme avant."""
    from types import SimpleNamespace

    from tradinglab.core.state import SystemState
    from tradinglab.models.client import LLMClient, _is_credit_exhausted
    from tradinglab.models.router import ModelRouter

    assert _is_credit_exhausted(RuntimeError("Your credit balance is too low"))
    assert not _is_credit_exhausted(TimeoutError("read timeout"))

    class _Down:
        def create(self, **kw):
            raise TimeoutError("read timeout")

    router = ModelRouter(settings.models)
    router.available_models = ["claude-opus-5"]
    called = []
    cl = LLMClient(router, SystemState(), sdk_client=SimpleNamespace(messages=_Down()),
                   fallback_factory=lambda: called.append(1))
    assert cl.complete("bull_thesis", "sys", "u", cache_key="k") is None
    assert not called and not cl.using_fallback


def test_quota_abonnement_suspend_les_appels(settings, home):
    """« Si on atteint le quota de mon abonnement, mets un repli en place » (2026-09-23) : le lab cesse
    d'appeler le LLM pendant la période de recharge au lieu de marteler le terminal, et continue de trader
    sur les règles déterministes. Une panne ordinaire garde l'ancien comportement (pas de suspension)."""
    import time
    from types import SimpleNamespace

    from tradinglab.core.journal import Journal
    from tradinglab.core.state import SystemState
    from tradinglab.models.client import LLMClient, _is_subscription_limited
    from tradinglab.models.router import ModelRouter

    assert _is_subscription_limited(RuntimeError("Claude usage limit reached. Resets at 3am"))
    assert not _is_subscription_limited(TimeoutError("read timeout"))

    class _Limite:
        def __init__(self):
            self.appels = 0

        def create(self, **kw):
            self.appels += 1
            raise RuntimeError("Claude usage limit reached - try again later")

    sdk = _Limite()
    router = ModelRouter(settings.models)
    router.available_models = ["claude-opus-5"]
    journal = Journal(home / "logs")
    cl = LLMClient(router, SystemState(), sdk_client=SimpleNamespace(messages=sdk), journal=journal)
    assert cl.complete("bull_thesis", "sys", "u", cache_key="k1") is None
    assert cl.paused_until > time.time(), "les appels doivent être suspendus"
    # les appels suivants ne touchent plus le terminal du tout
    for i in range(3):
        assert cl.complete("bear_thesis", "sys", f"u{i}", cache_key=f"k{i}") is None
    assert sdk.appels == 1, "un seul appel réel : le reste est court-circuité"
    # journalisé une seule fois, avec le message qui explique la dégradation
    from datetime import datetime, timezone
    jour = journal.read_day(datetime.now(timezone.utc))
    warns = [e for e in jour if e.get("kind") == "warning" and "quota abonnement" in str(e.get("message", ""))]
    assert len(warns) == 1 and warns[0].get("minutes") == 30, "alerte émise une seule fois, pas à chaque appel"
    assert any(e.get("kind") == "models" and "suspendus" in str(e.get("message", "")) for e in jour)
    # la suspension expire d'elle-même
    cl.paused_until = time.time() - 1
    assert cl.complete("bull_thesis", "sys", "u", cache_key="k9") is None and sdk.appels == 2


def test_break_even_arme_sur_le_pic_meme_si_le_prix_est_redescendu(broker, tmp_path):
    """« Je ne veux plus que ça arrive » (2026-09-23) : deux trades ont atteint 1 R puis sont revenus au
    stop plein sans break-even (CADCHF -586 $, NETH25 -521 $) parce que le pic était survenu entre deux
    cycles ou avant un changement de réglage. Le seuil se juge désormais sur le MEILLEUR point atteint."""
    from tradinglab.execution.position_manager import MarketContext, PMConfig

    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    pm.cfg = PMConfig(break_even_r=1.0, break_even_requires_structure=False)
    # le pic a eu lieu hors cycle : seul `max_r` en garde la trace
    plan.max_r = 1.05
    broker.set_price("EURUSD", pos.price_open + 0.40 * dist)      # le prix est redescendu, mais reste en profit
    pos = broker.position(pos.ticket)
    acts = pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))
    assert plan.break_even_done and any("break-even" in a for a in acts), acts
    assert broker.position(pos.ticket).sl >= plan.entry


def test_break_even_sur_pic_ne_force_jamais_un_stop_du_mauvais_cote(broker, tmp_path):
    """Si le prix est repassé SOUS le break-even, le stop ne bouge pas : jamais d'élargissement,
    jamais de stop du mauvais côté — la position garde son stop initial."""
    from tradinglab.execution.position_manager import MarketContext, PMConfig

    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    pm.cfg = PMConfig(break_even_r=1.0, break_even_requires_structure=False)
    plan.max_r = 1.05
    sl_avant = broker.position(pos.ticket).sl
    broker.set_price("EURUSD", pos.price_open - 0.50 * dist)      # repassé en perte
    pos = broker.position(pos.ticket)
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))
    sl_apres = broker.position(pos.ticket).sl
    assert sl_apres == sl_avant, "le stop ne doit pas bouger quand le break-even serait du mauvais côté"
    assert sl_apres >= plan.initial_sl, "le stop ne s'élargit jamais"
