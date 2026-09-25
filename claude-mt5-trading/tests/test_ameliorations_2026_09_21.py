"""Améliorations issues de la revue du 2026-09-21 (après-midi), toutes déterministes.

1. Budget jour incluant le risque ouvert (`14_daily_budget_incl_open_risk`) : la journée était à -0,73 % avec
   0,67 % encore en jeu pour un plafond de 1 % ; ni le verrou perte-jour (passé) ni le risque ouvert total
   (positions) ne voyaient le pire cas à -1,4 %. Le risque restant d'une position au break-even vaut 0.
2. RR live au gate (`06b_rr_live`) : le TP structurel est conservé mais l'entrée est refusée si, au tick courant,
   le RR tombe sous `min_rr_required` (le scan avait vu 1,6, le prix a dérivé, le TP réel ne payait plus 1,5R).
3. Règles d'invalidation en texte libre : 25 formulations, une seule (EMA50) était lue. `parse_invalidation`
   extrait un niveau ou une zone mesurable et le timeframe cité ; `_invalidation_hit` juge sur la barre clôturée.
4. `rule_compliance` observé : `sl_present` / `sl_never_widened` viennent de constats du position manager,
   plus de booléens codés en dur.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from tradinglab.core.state import BotPositionPlan, SystemState
from tradinglab.core.types import Side
from tradinglab.execution.invalidation import parse_invalidation
from tradinglab.execution.position_manager import MarketContext
from tradinglab.learning.post_trade import build_trade_record
from tradinglab.orchestration.orchestrator import Orchestrator
from tradinglab.risk.risk_manager import RiskLimits, RiskManager
from tests.conftest import FIXED_NOW
from tests.test_position_watchdog_orchestrator import open_buy, pm_for
from tests.test_risk_and_gate import ctx_for, gate_for, make_candidate, make_state


def _plan(ticket, symbol, side, entry, sl, risk_money, last_sl=0.0):
    return BotPositionPlan(ticket, symbol, side, "B01", f"c{ticket}", entry, sl, 1.0, risk_money, 0.125,
                           opened_at=FIXED_NOW.isoformat(), last_sl=last_sl or sl)


# ---------------------------------------------------------------- 1. budget jour incluant le risque ouvert
def test_risque_restant_nul_au_break_even_et_partiel_en_trailing():
    st = SystemState()
    p = _plan(1, "EURUSD", "BUY", 1.1000, 1.0900, 100.0)
    assert st.remaining_risk_money(p) == pytest.approx(100.0)
    p.last_sl = 1.1000                      # break-even
    assert st.remaining_risk_money(p) == pytest.approx(0.0)
    p.last_sl = 1.0950                      # moitié du chemin
    assert st.remaining_risk_money(p) == pytest.approx(50.0)
    p.last_sl = 1.1050                      # stop en profit : jamais négatif
    assert st.remaining_risk_money(p) == pytest.approx(0.0)
    s = _plan(2, "EURUSD", "SELL", 1.1000, 1.1100, 100.0)
    s.last_sl = 1.1050
    assert st.remaining_risk_money(s) == pytest.approx(50.0)


def test_pire_cas_jour_refuse_quand_perte_realisee_plus_risque_ouvert_depassent_le_plafond():
    """Cas du 2026-09-21 : -0,73 % réalisé, 0,67 % ouvert, plafond 1 % → toute nouvelle entrée est refusée."""
    st = make_state(100000.0)
    st.update_equity(99270.0, 99270.0)          # -0,73 % réalisé (solde)
    st.bot_positions["1"] = _plan(1, "GBPUSD", "BUY", 1.30, 1.29, 670.0)
    rm = RiskManager(RiskLimits(max_daily_loss_internal_percent=1.0, max_total_open_risk_percent=1.0, max_open_positions=30))
    checks = {c.name: c for c in rm.check_limits(st, "EURUSD", Side.BUY, 125.0, 0, 1)}
    assert checks["daily_loss_internal"].ok, "le verrou perte-jour ne voit que le passé : il laisse passer"
    assert checks["max_total_open_risk"].ok, "le risque ouvert total ne voit que les positions : il laisse passer"
    assert not checks["daily_budget_incl_open_risk"].ok
    assert "pire cas jour 1.5" in checks["daily_budget_incl_open_risk"].detail


def test_pire_cas_jour_accepte_quand_le_stop_est_au_break_even():
    st = make_state(100000.0)
    st.update_equity(99270.0, 99270.0)
    p = _plan(1, "GBPUSD", "BUY", 1.30, 1.29, 670.0)
    p.last_sl = 1.30                             # plus rien en jeu sur cette position
    st.bot_positions["1"] = p
    rm = RiskManager(RiskLimits(max_daily_loss_internal_percent=1.0, max_total_open_risk_percent=1.0, max_open_positions=30))
    checks = {c.name: c for c in rm.check_limits(st, "EURUSD", Side.BUY, 125.0, 0, 1)}
    assert checks["daily_budget_incl_open_risk"].ok          # 0,73 + 0,125 = 0,855 % < 1 %


def test_pire_cas_jour_ne_compte_pas_deux_fois_la_perte_flottante():
    """L'equity porte déjà la perte latente ; on part du SOLDE pour ne pas la compter en plus du SL."""
    st = make_state(100000.0)
    st.update_equity(99500.0, 100000.0)          # -500 flottants, solde intact
    st.bot_positions["1"] = _plan(1, "GBPUSD", "BUY", 1.30, 1.29, 600.0)
    assert st.worst_case_daily_drawdown_percent(0.0) == pytest.approx(0.6)


def test_gate_expose_le_controle_budget_jour(settings, broker):
    # scénario proportionnel à la limite interne (1 % jusqu'au 2026-09-24, 2 % depuis) : perte réalisée 70 % de la
    # limite + risque ouvert 65 % → le nouveau trade ne peut qu'être refusé, quelle que soit la limite
    lim = float(settings.risk["max_daily_loss_internal_percent"])
    gate, _ = gate_for(settings, broker)
    st = make_state(100000.0)
    st.update_equity(100000.0 - 700.0 * lim, 100000.0 - 700.0 * lim)
    st.bot_positions["1"] = _plan(1, "GBPUSD", "BUY", 1.30, 1.29, 650.0 * lim)
    c, atr = make_candidate(broker, "EURUSD", Side.BUY)
    res, req = gate.evaluate(ctx_for(broker, c, st, atr, correlations=pd.DataFrame()))
    names = {ch.name: ch.ok for ch in res.checks}
    assert names["14_daily_budget_incl_open_risk"] is False
    assert not res.approved


# ---------------------------------------------------------------- 2. RR live au gate
def test_rr_live_refuse_si_le_prix_a_derive_vers_le_tp(settings, broker):
    gate, _ = gate_for(settings, broker)
    st = make_state()
    c, atr = make_candidate(broker, "EURUSD", Side.BUY, rr=1.6)
    res, req = gate.evaluate(ctx_for(broker, c, st, atr, correlations=pd.DataFrame()))
    assert {ch.name: ch.ok for ch in res.checks}["06b_rr_live"] is True
    # le prix monte de 20 % de la distance au SL : RR live = (1,6·d − 0,2·d)/(1,2·d) = 1,17 < 1,5
    dist = c.entry - c.sl
    broker.set_price("EURUSD", c.entry + 0.2 * dist)
    res, req = gate.evaluate(ctx_for(broker, c, st, atr, correlations=pd.DataFrame()))
    ok = {ch.name: ch for ch in res.checks}["06b_rr_live"]
    assert ok.ok is False and "rr live" in ok.detail
    assert not res.approved


def test_rr_live_accepte_si_le_prix_recule_le_tp_structurel_est_conserve(settings, broker):
    gate, _ = gate_for(settings, broker)
    st = make_state()
    c, atr = make_candidate(broker, "EURUSD", Side.BUY, rr=1.6)
    dist = c.entry - c.sl
    broker.set_price("EURUSD", c.entry - 0.1 * dist)     # meilleur prix : RR live > 1,6
    res, req = gate.evaluate(ctx_for(broker, c, st, atr, correlations=pd.DataFrame()))
    assert {ch.name: ch.ok for ch in res.checks}["06b_rr_live"] is True
    assert req is not None and req.tp == pytest.approx(c.tp_plan[-1])


def test_rr_live_refuse_sans_tick_sans_lever(settings, broker):
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker, "EURUSD", Side.BUY)
    res, req = gate.evaluate(ctx_for(broker, c, make_state(), atr, tick=None, correlations=pd.DataFrame()))
    names = {ch.name: ch for ch in res.checks}
    assert names["06b_rr_live"].ok is False and "indisponible" in names["06b_rr_live"].detail
    assert not res.approved


# ---------------------------------------------------------------- 3. règles d'invalidation en texte libre
@pytest.mark.parametrize("text,side,entry,level,tf,zone", [
    ("clôture M15 sous niveau retesté 1.6095", Side.BUY, 1.6150, 1.6095, "M15", None),
    ("nouvelle clôture M15 au-delà de 1,6095 (niveau cassé)", Side.SELL, 1.6050, 1.6095, "M15", None),
    ("clôture M15 de retour à l'intérieur du range asiatique (1.0810-1.0835)", Side.BUY, 1.0850, 1.0835, "M15", (1.0810, 1.0835)),
    ("clôture M5 de retour à l'intérieur du range asiatique (1.0810-1.0835)", Side.SELL, 1.0795, 1.0810, "M5", (1.0810, 1.0835)),
    ("clôture H1 au-delà du niveau 2340.5 de l'impulsion", Side.SELL, 2300.0, 2340.5, "H1", None),
    ("clôture au-delà de 18250 (sans timeframe)", Side.BUY, 18300.0, 18250.0, "", None),
])
def test_parse_invalidation_extrait_niveau_zone_et_timeframe(text, side, entry, level, tf, zone):
    rule = parse_invalidation(text, side, entry)
    assert rule is not None
    assert rule.level == pytest.approx(level)
    assert rule.timeframe == tf
    assert rule.zone == (pytest.approx(zone[0]), pytest.approx(zone[1])) if zone else rule.zone is None


@pytest.mark.parametrize("text", [
    "clôture H1 au-delà de l'EMA50 H1",
    "ADX H1 < 20 ou croisement inverse des DI",
    "clôture M15 au-delà de 0,5 ATR",
    "clôture H1 au-delà du niveau 61,8 % de l'impulsion",
    "20 barres sans nouveau plus haut",
    "",
])
def test_parse_invalidation_ne_devine_jamais(text):
    assert parse_invalidation(text, Side.BUY, 1.1000) is None


def test_parse_invalidation_ignore_les_nombres_hors_de_la_zone_de_prix():
    # « 3 » (nombre de barres) et « 200 » ne sont pas des prix EURUSD
    rule = parse_invalidation("3 clôtures M15 sous 1.0950 ou volume < 200", Side.BUY, 1.1000)
    assert rule is not None and rule.level == pytest.approx(1.0950) and rule.zone is None


def test_regle_declenchee_uniquement_du_cote_defavorable():
    buy = parse_invalidation("clôture M15 sous 1.0950", Side.BUY, 1.1000)
    assert buy.triggered(Side.BUY, 1.0940) and not buy.triggered(Side.BUY, 1.0960)
    sell = parse_invalidation("clôture M15 au-delà de 1.1050", Side.SELL, 1.1000)
    assert sell.triggered(Side.SELL, 1.1060) and not sell.triggered(Side.SELL, 1.1040)
    assert not buy.triggered(Side.BUY, float("nan"))


def _frame(closes, ema50=None):
    df = pd.DataFrame({"open": closes, "high": [c + 0.001 for c in closes], "low": [c - 0.001 for c in closes], "close": closes})
    if ema50 is not None:
        df["ema50"] = ema50
    return df


def test_invalidation_hit_juge_sur_la_barre_cloturee_du_timeframe_cite():
    o = Orchestrator.__new__(Orchestrator)      # seule `_invalidation_hit` est exercée (pure)
    plan = _plan(1, "GBPUSD", "BUY", 1.6150, 1.6000, 100.0)
    plan.invalidation = "clôture M15 sous niveau retesté 1.6095"
    # M15 : avant-dernière barre (clôturée) sous 1.6095, barre en formation au-dessus → déclenché
    m15 = _frame([1.62, 1.61, 1.6080, 1.6120])
    h1 = _frame([1.62, 1.62, 1.62, 1.62])
    snap = SimpleNamespace(frames={"M15": m15, "H1": h1})
    hit, why = o._invalidation_hit(plan, Side.BUY, snap, h1)
    assert hit is True and "1.6095" in why
    # seule la barre EN FORMATION est sous le niveau : pas de sortie (décision sur barre clôturée)
    snap = SimpleNamespace(frames={"M15": _frame([1.62, 1.61, 1.6120, 1.6080]), "H1": h1})
    assert o._invalidation_hit(plan, Side.BUY, snap, h1)[0] is False


def test_invalidation_zone_retour_dans_le_range():
    o = Orchestrator.__new__(Orchestrator)
    plan = _plan(1, "EURUSD", "SELL", 1.0795, 1.0850, 100.0)
    plan.invalidation = "clôture M5 de retour à l'intérieur du range asiatique (1.0810-1.0835)"
    df = _frame([1.0790, 1.0800, 1.0815, 1.0790])          # clôturée 1.0815 : de retour dans le range
    assert o._invalidation_hit(plan, Side.SELL, SimpleNamespace(frames={"M5": df}), df)[0] is True
    df = _frame([1.0790, 1.0800, 1.0805, 1.0790])          # 1.0805 : encore sous la borne basse
    assert o._invalidation_hit(plan, Side.SELL, SimpleNamespace(frames={"M5": df}), df)[0] is False


def test_invalidation_ema50_reste_le_repli():
    o = Orchestrator.__new__(Orchestrator)
    plan = _plan(1, "EURUSD", "BUY", 1.1000, 1.0900, 100.0)
    plan.invalidation = "clôture H1 au-delà de l'EMA50 H1"
    df = _frame([1.11, 1.10, 1.095, 1.10], ema50=[1.10, 1.10, 1.10, 1.10])
    hit, why = o._invalidation_hit(plan, Side.BUY, SimpleNamespace(frames={"H1": df}), df)
    assert hit is True and "EMA50" in why
    plan.invalidation = "ADX H1 < 20 ou croisement inverse des DI"      # rien de mesurable → jamais de sortie
    assert o._invalidation_hit(plan, Side.BUY, SimpleNamespace(frames={"H1": df}), df) == (False, "")


def test_sortie_anticipee_journalise_la_regle(broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    acts = pm.manage(plan, pos, spec, MarketContext(atr=0.0012, invalidated=True, invalidation_reason="clôture M15 au-delà de 1.0800"))
    assert any("invalidation" in a for a in acts)
    assert broker.position(pos.ticket) is None
    events = [l for l in (tmp_path / "logs").glob("journal-*.jsonl")]
    assert events and "1.0800" in events[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------- 4. rule_compliance observé
def test_rule_compliance_observe_sl_manquant_et_elargi(broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012))
    assert plan.sl_missing_seen is False and plan.sl_widened_seen is False

    # élargissement externe du SL (terminal/tiers) : constaté, pas déduit
    broker._positions[pos.ticket].sl = plan.last_sl - 0.0020
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0012))
    assert plan.sl_widened_seen is True

    # SL retiré côté broker : remis par le bot et constaté
    broker._positions[pos.ticket].sl = 0.0
    acts = pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0012))
    assert plan.sl_missing_seen is True and any("SL manquant" in a for a in acts)

    broker.close_position(pos.ticket, comment="test")
    deals = broker.history_deals(FIXED_NOW.replace(year=2025), broker.now())
    rec = build_trade_record(plan, deals, None, FIXED_NOW.isoformat())
    assert rec.rule_compliance["sl_present"] is False
    assert rec.rule_compliance["sl_never_widened"] is False


def test_rule_compliance_reste_vrai_sans_incident(broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    broker.set_price("EURUSD", pos.price_open + 1.3 * dist)
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0012, structure_ok=True))
    assert plan.break_even_done
    # le break-even (resserrement) n'est pas un élargissement ; arrondi d'un tick toléré
    broker._positions[pos.ticket].sl -= spec.tick_size
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0012, structure_ok=True))
    assert plan.sl_widened_seen is False and plan.sl_missing_seen is False
    broker.close_position(pos.ticket, comment="test")
    rec = build_trade_record(plan, broker.history_deals(FIXED_NOW.replace(year=2025), broker.now()), None, FIXED_NOW.isoformat())
    assert rec.rule_compliance == {"sl_present": True, "sl_never_widened": True, "partials": [False, False], "break_even": True}


# ==========================================================================================================
# Couche LLM en production (première activation de la clé API, 17:47 locale) : réflexion par défaut des
# modèles Claude 5 → texte vide, revues séquentielles → cycle de 100 s. Corrigé le jour même.
# ==========================================================================================================
import threading
import time as _time
from datetime import datetime, timedelta, timezone

from tradinglab.agents.review import ARBITER_MAX_TOKENS, THESIS_MAX_TOKENS, AdversarialReview
from tradinglab.core.journal import Journal
from tradinglab.core.types import Verdict
from tradinglab.models.client import THINKING, LLMClient
from tradinglab.models.router import ModelRouter
from tradinglab.orchestration.orchestrator import MAX_LLM_REVIEWS_PER_CYCLE
from tests.test_review_market_agents_orchestration import FakeLLM, candidate, make_orch


class _RecordingMsgs:
    """SDK factice : mémorise les kwargs de chaque appel, répond par un JSON valide (ou tronqué)."""

    def __init__(self, stop_reason="end_turn", text='{"ok": true}', delay=0.0):
        self.kwargs: list[dict] = []
        self.stop_reason, self.text, self.delay = stop_reason, text, delay
        self._lock = threading.Lock()

    def create(self, **kw):
        with self._lock:
            self.kwargs.append(kw)
        if self.delay:
            _time.sleep(self.delay)
        return SimpleNamespace(content=[SimpleNamespace(text=self.text)], stop_reason=self.stop_reason,
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))


def _client(settings, home, msgs, ttl=240):
    router = ModelRouter(settings.models)
    router.available_models = ["claude-opus-5"]
    st = SystemState()
    return LLMClient(router, st, cache_ttl_sec=ttl, sdk_client=SimpleNamespace(messages=msgs), journal=Journal(home / "logs")), st


def test_llm_reflexion_desactivee_et_stop_reason_journalise(settings, home):
    msgs = _RecordingMsgs()
    client, _ = _client(settings, home, msgs)
    resp = client.complete("bull_thesis", "sys", "user", max_tokens=700)
    assert resp is not None and msgs.kwargs[0]["thinking"] == THINKING == {"type": "disabled"}
    assert msgs.kwargs[0]["max_tokens"] == 700 and "temperature" not in msgs.kwargs[0]
    ev = [e for e in client.journal.read_day(datetime.now(timezone.utc)) if e["kind"] == "llm_call"]
    assert ev and ev[-1]["stop_reason"] == "end_turn"


def test_llm_reponse_tronquee_est_signalee(settings, home):
    msgs = _RecordingMsgs(stop_reason="max_tokens", text="")
    client, _ = _client(settings, home, msgs)
    client.complete("bear_thesis", "sys", "user", max_tokens=500)
    warns = [e for e in client.journal.read_day(datetime.now(timezone.utc)) if str(e.get("message", "")).startswith("réponse LLM tronquée")]
    assert warns and warns[-1]["max_tokens"] == 500 and warns[-1]["role"] == "bear_thesis"


def test_cache_par_identite_declaree_ignore_le_prompt(settings, home):
    """Deux cycles sur la même barre : le prompt diffère (entrée/atr mis à jour) mais l'identité est la même → 0 appel."""
    msgs = _RecordingMsgs()
    client, st = _client(settings, home, msgs)
    a = client.complete("bull_thesis", "sys", "user v1", cache_key="EURUSD|BUY|B01|2026-09-21T15:45")
    b = client.complete("bull_thesis", "sys", "user v2 (prix rafraîchi)", cache_key="EURUSD|BUY|B01|2026-09-21T15:45")
    assert a is not None and b is not None and b.cached and b.cost_usd == 0.0 and len(msgs.kwargs) == 1
    # autre rôle, autre barre : nouveaux appels ; sans identité déclarée : clé = prompt complet (ancien comportement)
    client.complete("bear_thesis", "sys", "user v1", cache_key="EURUSD|BUY|B01|2026-09-21T15:45")
    client.complete("bull_thesis", "sys", "user v1", cache_key="EURUSD|BUY|B01|2026-09-21T16:00")
    assert len(msgs.kwargs) == 3
    client.complete("bull_thesis", "sys", "user v1")
    client.complete("bull_thesis", "sys", "user v2")
    assert len(msgs.kwargs) == 5
    assert st.model_budget.calls_by_tier_day.get("TIER_B") == 5


def test_appels_paralleles_comptes_sans_perte(settings, home):
    msgs = _RecordingMsgs(delay=0.01)
    client, st = _client(settings, home, msgs)
    threads = [threading.Thread(target=client.complete, args=("bull_thesis", "sys", f"u{i}"), kwargs={"cache_key": f"k{i}"}) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert client.calls == 12 and st.model_budget.calls_by_tier_day.get("TIER_B") == 12
    assert st.model_budget.spent_usd == pytest.approx(12 * client._cost("claude-opus-5", 100, 50))


def test_revue_theses_en_parallele_et_budgets_de_tokens():
    seen_threads: set[str] = set()
    started: dict[str, float] = {}

    def hook(fake, role):
        started[role] = _time.monotonic()
        seen_threads.add(threading.current_thread().name)
        _time.sleep(0.15)
    llm = FakeLLM(hook=hook)
    rv = AdversarialReview(llm, 65, 1.5)
    t0 = _time.monotonic()
    res = rv.review(candidate(bar_time="2026-09-21T15:45:00+00:00"))
    wall = _time.monotonic() - t0
    assert res.verdict is Verdict.APPROVE and res.arbiter.startswith("llm:") and llm.calls == 4
    # 3 thèses en parallèle (≈ 0,15 s) + arbitre (0,15 s) ≪ 4 × 0,15 s en séquentiel
    assert wall < 0.5 and any(n.startswith("review") for n in seen_threads)
    assert started["trade_arbiter"] >= max(started[r] for r in ("bull_thesis", "bear_thesis", "devil_advocate"))
    assert sorted(llm.max_tokens_seen) == sorted([THESIS_MAX_TOKENS] * 3 + [ARBITER_MAX_TOKENS])
    assert THESIS_MAX_TOKENS > 500 and 500 < ARBITER_MAX_TOKENS <= THESIS_MAX_TOKENS


def test_cle_de_cache_par_barre_et_prompt_sans_champs_volatils():
    c1 = candidate(bar_time="2026-09-21T15:45:00+00:00")
    c2 = candidate(bar_time="2026-09-21T15:45:00+00:00", entry=1.0801)
    assert AdversarialReview._cache_key(c1) == AdversarialReview._cache_key(c2) == "EURUSD|BUY|B01|2026-09-21T15:45:00+00:00"
    assert AdversarialReview._cache_key(candidate(bar_time="2026-09-21T16:00:00+00:00")) != AdversarialReview._cache_key(c1)
    prompts: list[str] = []
    systems: list[str] = []
    keys: list[str] = []
    inner = FakeLLM()

    class _Spy:
        def complete(self, role, system, user, **kw):
            prompts.append(user)
            systems.append(system)
            keys.append(kw.get("cache_key", ""))
            return inner.complete(role, system, user, **kw)
    AdversarialReview(_Spy(), 65, 1.5).review(c1)
    assert len(prompts) == 4 and all(c1.id not in p and "created_at" not in p for p in prompts)
    # dimensionnement / corrélation : calculés après la revue par le gate, jamais montrés au LLM (sinon « risk_percent=0.0 incohérent »)
    assert all('"risk_percent"' not in p and '"correlation_impact"' not in p for p in prompts)
    assert all("Risk Gate déterministe" in system for system in systems)
    assert set(keys) == {AdversarialReview._cache_key(c1)}


def test_orchestrateur_revues_llm_en_parallele_heartbeat_et_creneaux(settings, broker):
    """Les N revues LLM du cycle tournent en parallèle ; un rejet déterministe ne consomme pas un créneau."""
    o = make_orch(settings, broker)
    o.cycle()
    o.cycle()
    assert o.state.mode == "AUTO"
    threads: set[str] = set()

    def hook(fake, role):
        threads.add(threading.current_thread().name)
        _time.sleep(0.05)
    fake = FakeLLM(hook=hook)
    o.review.llm = fake
    broker.set_now(broker.now() + timedelta(minutes=5))
    # le journal n'écrit un candidat qu'une fois par nature (2026-09-25) : on vide cette mémoire pour que le cycle
    # mesuré journalise tous ses candidats, base du comptage ci-dessous
    o._journal_sig = {}
    t0 = _time.monotonic()
    s = o.cycle()
    wall = _time.monotonic() - t0
    cands = [e["candidate"] for e in o.journal.read_day(kinds={"candidate"}) if e["ts_utc"] >= s["ts"][:19]]
    consulted = [c for c in cands if c["review"].get("llm_consulted")]
    assert fake.calls == 4 * len(consulted) <= 4 * MAX_LLM_REVIEWS_PER_CYCLE
    if consulted:
        assert any(n.startswith("llm-review") for n in threads)
        # 4 appels × 0,05 s par candidat, candidats en parallèle : bien moins que le séquentiel
        assert wall < 0.05 * 4 * len(consulted) + 1.5
    # les candidats rejetés d'office ne sont pas comptés comme « au-delà des N meilleurs »
    for c in cands:
        if c["review"]["verdict"] == "REJECT" and not c["review"].get("llm_consulted"):
            assert "au-delà" not in str(c["review"].get("llm_skipped", ""))


def test_quota_horaire_strict_sous_parallelisme(settings, home):
    """La réservation se fait au routage : N appels en vol ne peuvent plus dépasser le plafond horaire
    (constaté avant correctif : TIER_C à 207/200, l'incrément n'arrivait qu'au retour de l'API)."""
    settings.models["max_opus_calls_per_hour"] = 5
    msgs = _RecordingMsgs(delay=0.05)
    client, st = _client(settings, home, msgs)
    threads = [threading.Thread(target=client.complete, args=("bull_thesis", "sys", f"u{i}"), kwargs={"cache_key": f"k{i}"}) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(msgs.kwargs) == 5 and st.model_budget.calls_by_tier_hour["TIER_B"] == 5
    assert client.calls == 5 and st.model_budget.spent_usd == pytest.approx(5 * client._cost("claude-opus-5", 100, 50))
    # les 7 refusés sont journalisés llm_skipped ; une réponse en cache ne consomme pas de créneau
    skips = [e for e in client.journal.read_day(datetime.now(timezone.utc)) if e["kind"] == "llm_skipped"]
    assert len(skips) == 7
    st.model_budget.calls_by_tier_hour["TIER_B"] = 4
    hit_key = next(k["messages"][0]["content"] for k in msgs.kwargs)
    n_before = len(msgs.kwargs)
    idx = hit_key[1:]
    assert client.complete("bull_thesis", "sys", "peu importe", cache_key=f"k{idx}").cached
    assert len(msgs.kwargs) == n_before and st.model_budget.calls_by_tier_hour["TIER_B"] == 4


def test_reflexion_par_modele_fable_adaptive_opus_disabled(settings, home):
    """Fable 5 refuse thinking=disabled (400) : adaptive + effort low pour lui, disabled pour les autres
    (constaté le 2026-09-21 : post_trade_root_cause → claude-fable-5 → BadRequestError en production)."""
    from tradinglab.models.client import thinking_kwargs
    assert thinking_kwargs("claude-fable-5") == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}
    assert thinking_kwargs("claude-opus-5") == {"thinking": THINKING}
    msgs = _RecordingMsgs()
    client, _ = _client(settings, home, msgs)
    # Fable ne figure plus dans aucun tier du dépôt (réservé aux conversations de l'utilisateur depuis le
    # 2026-09-23) : le test l'injecte lui-même pour vérifier la mécanique, sans dépendre de la config.
    client.router.cfg["tiers"]["TIER_A"] = {**client.router.cfg["tiers"]["TIER_A"], "candidates": ["claude-fable-5"]}
    client.router.available_models = ["claude-fable-5"]
    client.router.role_to_tier["bull_thesis"] = "TIER_A"
    assert client.complete("bull_thesis", "sys", "user", cache_key="k") is not None
    kw = msgs.kwargs[0]
    assert kw["model"] == "claude-fable-5" and kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "low"} and "temperature" not in kw


def test_post_trade_root_cause_budget_de_tokens_suffisant():
    """Le rôle post_trade_root_cause tourne sur Fable 5 (réflexion adaptive obligatoire) : 400 tokens
    tronquaient la réponse (constaté le 2026-09-22 00:47). Le budget doit rester >= 800 et la consigne compacte."""
    import inspect

    from tradinglab.learning import post_trade as pt
    src = inspect.getsource(pt.PostTradeAnalyzer)
    assert "max_tokens=800" in src and "max_tokens=400" not in src
    assert "compact" in src


def test_modele_adaptive_only_appris_automatiquement(settings, home):
    """Opus 5.5 (2026-09-22) refuse thinking=disabled comme Fable : le client l'apprend au premier 400 et
    réessaie en adaptive — aucune liste à maintenir au prochain modèle."""
    from tradinglab.models.client import ADAPTIVE_ONLY, thinking_kwargs

    assert thinking_kwargs("claude-opus-5-5") == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}
    assert thinking_kwargs("claude-opus-5") == {"thinking": THINKING}

    class _Refuse(_RecordingMsgs):
        def create(self, **kw):
            if kw.get("thinking", {}).get("type") == "disabled":
                raise RuntimeError('400 … "thinking.type.disabled" is not supported for this model …')
            return super().create(**kw)

    msgs = _Refuse()
    client, _ = _client(settings, home, msgs)
    client.router.available_models = ["claude-opus-5"]    # accepte normalement `disabled` : ici le faux SDK le refuse
    ADAPTIVE_ONLY.discard("claude-opus-5")
    assert client.complete("bull_thesis", "sys", "u", cache_key="k1") is not None
    assert "claude-opus-5" in ADAPTIVE_ONLY
    assert msgs.kwargs[-1]["thinking"] == {"type": "adaptive"} and msgs.kwargs[-1]["output_config"] == {"effort": "low"}
    # appris : l'appel suivant part directement en adaptive (un seul appel SDK)
    n = len(msgs.kwargs)
    assert client.complete("bull_thesis", "sys", "u2", cache_key="k2") is not None
    assert len(msgs.kwargs) == n + 1
    ADAPTIVE_ONLY.discard("claude-opus-5")


def test_retour_19_sept_seule_l_ema50_invalide():
    """2026-09-25, retour au 19/09 : `invalidation_levels: false` → le niveau lu dans le texte ne ferme plus rien."""
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(execution={"invalidation_levels": False})
    plan = _plan(1, "GBPUSD", "BUY", 1.6150, 1.6000, 100.0)
    plan.invalidation = "clôture M15 sous niveau retesté 1.6095"
    m15 = _frame([1.62, 1.61, 1.6080, 1.6120])
    assert o._invalidation_hit(plan, Side.BUY, SimpleNamespace(frames={"M15": m15}), m15) == (False, "")
