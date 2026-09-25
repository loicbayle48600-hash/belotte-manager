"""Correctifs de l'audit complet du 2026-09-23 (journal des 4 derniers jours, compte DEMO 53060800).

**1. Watchdog : une coupure MT5 transitoire ne doit pas faire basculer le bot.**

Le watchdog demandait SAFE_MODE dès le premier `connect()` en échec. Or « Authorization failed (-6) »
et « IPC timeout (-10005) » durent 1 à 3 contrôles (≤ 10 s) : 6 demandes de SAFE_MODE en 4 jours
pour des coupures sans conséquence, et 111 changements de mode « AUTO : cycles sains après démarrage »
pour 56 démarrages — le bot passait son temps à re-qualifier des cycles sains. Une coupure n'est
signalée qu'après 2 contrôles consécutifs (≈ 6 s) ; une coupure réelle l'est toujours.

**2. Cache LLM : mesurer ce qu'il économise.**

Un succès de cache renvoyait la réponse en silence (`cached=True`, ni journal ni compteur). Avec
5 643 appels `llm_call` sur 4 jours, impossible de dire combien de revues le cache évitait.
`ModelBudget.cache_hits_day` compte les réponses servies à 0 $ et suit le jour du budget.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.journal import Journal  # noqa: E402
from tradinglab.core.state import StateStore, SystemState  # noqa: E402
from tradinglab.models.client import LLMClient  # noqa: E402
from tradinglab.models.router import ModelRouter  # noqa: E402
from tradinglab.monitoring.watchdog import DISCONNECT_CHECKS, Watchdog  # noqa: E402

from test_watchdog_horloge import MAINTENANT, _BrokerTicks  # noqa: E402


class _BrokerCoupures(_BrokerTicks):
    """Connexion qui échoue `pannes` fois de suite avant de revenir."""

    def __init__(self, pannes: int):
        super().__init__(["EURUSD", "BTCUSD"], {"EURUSD": 2.0, "BTCUSD": 2.0})
        self.pannes = pannes
        self.tentatives = 0

    def is_connected(self) -> bool:
        return self.tentatives > self.pannes

    def connect(self) -> bool:
        self.tentatives += 1
        return self.is_connected()

    def last_error(self) -> str:
        return "IPC timeout (-10005)"


def _wd(settings, home, broker):
    wd = Watchdog(settings, broker, StateStore(settings.state_dir), Journal(home / "logs", component="wd_test"),
                  reference_symbol="EURUSD")
    wd._ancrer_horloge()
    return wd


def test_une_coupure_d_un_seul_controle_ne_demande_pas_safe_mode(settings, home, monkeypatch):
    monkeypatch.setitem(settings.raw["markets"], "crypto", ["BTCUSD"])
    wd = _wd(settings, home, _BrokerCoupures(pannes=1))
    rep = wd.check_once()
    assert rep.mt5_connected is False and rep.safe_mode_request is False, "premier échec : on attend le contrôle suivant"
    rep = wd.check_once()
    assert rep.mt5_connected is True and rep.safe_mode_request is False
    assert wd._disconnect_streak == 0, "la reconnexion remet le compteur à zéro"


def test_une_coupure_persistante_demande_toujours_safe_mode(settings, home, monkeypatch):
    monkeypatch.setitem(settings.raw["markets"], "crypto", ["BTCUSD"])
    wd = _wd(settings, home, _BrokerCoupures(pannes=10))
    reps = [wd.check_once() for _ in range(DISCONNECT_CHECKS)]
    assert reps[-1].safe_mode_request is True and any("MT5 déconnecté" in r for r in reps[-1].reasons)
    assert all(r.safe_mode_request is False for r in reps[:-1])
    assert DISCONNECT_CHECKS == 2, "≈ 6 s à 3 s d'intervalle : au-delà, une vraie coupure serait vue trop tard"


class _Msgs:
    def __init__(self):
        self.n = 0

    def create(self, **kw):
        self.n += 1
        return SimpleNamespace(content=[SimpleNamespace(text='{"ok": true}')], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))


def test_les_succes_de_cache_sont_comptes_et_suivent_le_jour(settings, home):
    router = ModelRouter(settings.models)
    router.available_models = ["claude-opus-5"]
    st = SystemState()
    msgs = _Msgs()
    client = LLMClient(router, st, cache_ttl_sec=240, sdk_client=SimpleNamespace(messages=msgs), journal=Journal(home / "logs"))
    for _ in range(3):
        client.complete("bull_thesis", "sys", "u", cache_key="EURUSD|BUY|B01|bar")
    assert msgs.n == 1 and st.model_budget.cache_hits_day == 2
    st.model_budget.day = "1999-01-01"                       # nouveau jour côté routeur : compteurs remis à zéro
    client.complete("bull_thesis", "sys", "u", cache_key="EURUSD|BUY|B01|bar")
    assert st.model_budget.cache_hits_day == 1 and st.model_budget.spent_usd == 0.0


def test_leaderboard_lisible_statut_trades_et_pf_sans_perte(tmp_path):
    """Dashboard 2026-09-23 (signalé par l'utilisateur) : colonnes « Statut » et « Trades » en
    UNKNOWN / UNAVAILABLE (le fichier exportait `sample_size`, jamais `status`), et un agent à UN trade
    gagnant affichait un profit factor de 99,00 (sentinelle interne pour « infini ») en tête du
    classement devant un agent à 14 trades. Désormais : `trades`, `wins`, `losses`, `total_r`,
    `no_losses` (PF non défini → « aucune perte »), échantillons ≥ 5 classés d'abord."""
    from tradinglab.learning.store import LearningStore, TradeRecord

    store = LearningStore(tmp_path / "learning.db")

    def rec(aid, r, i):
        return TradeRecord(ticket=i, agent_id=aid, symbol="EURUSD", side="BUY", entry=1.0, sl=0.99, risk_money=100.0,
                           risk_percent=0.05, result_r=r, pnl=100 * r, opened_at="2026-09-23T00:00:00+00:00",
                           closed_at="2026-09-23T01:00:00+00:00", exit_reason="tp" if r > 0 else "sl")

    i = 0
    for r in (2.29,):                                        # E01 : 1 trade gagnant
        i += 1; store.record_trade(rec("E01", r, i))
    for r in (1.5, -1.0, 2.0, -1.0, 1.2, 0.8):               # C06 : 6 trades, PF défini
        i += 1; store.record_trade(rec("C06", r, i))
    lb = store.leaderboard()
    assert [x["agent_id"] for x in lb] == ["C06", "E01"], "l'échantillon suffisant passe devant le coup unique"
    e01 = lb[1]
    assert e01["trades"] == 1 and e01["wins"] == 1 and e01["losses"] == 0
    assert e01["no_losses"] is True and e01["profit_factor"] is None, "pas de 99.0 affiché"
    c06 = lb[0]
    assert c06["trades"] == 6 and c06["wins"] == 4 and c06["losses"] == 2 and c06["no_losses"] is False
    assert c06["profit_factor"] == round(5.5 / 2.0, 2) and c06["total_r"] == 3.5


def test_symbole_deja_porte_ne_consomme_pas_de_revue_llm(settings, broker):
    """Audit 2026-09-23 : 246 revues LLM (≈ 1 000 appels) en 4 jours pour des candidats que le gate
    refusait ensuite pour « position déjà ouverte sur le symbole » (1 par symbole). Ces candidats ne
    passent plus par le LLM (`llm_skipped` explicite) et laissent leur créneau aux autres ; le verdict
    déterministe et le gate restent inchangés."""
    from datetime import timedelta

    from tradinglab.core.state import SystemMode

    from test_review_market_agents_orchestration import FakeLLM, make_orch

    o = make_orch(settings, broker)
    o.cycle(); o.cycle()
    assert o.state.mode == SystemMode.AUTO.value
    fake = FakeLLM()
    o.review.llm = fake
    broker.set_now(broker.now() + timedelta(minutes=5))
    # tous les symboles de l'univers sont réellement portés par le bot (positions broker au magic du bot,
    # adoptées par `pm.sync`) : aucune revue LLM ne doit partir. SL très éloigné : rien ne se ferme pendant le test.
    from tradinglab.core.types import OrderKind, OrderRequest, Side

    for sym in list(o.snapshots):
        spec, tick = broker.symbol_info(sym), broker.tick(sym)
        if spec is None or tick is None:
            continue
        broker.order_send(OrderRequest(symbol=sym, side=Side.BUY, volume=spec.volume_min, sl=round(tick.ask * 0.5, spec.digits),
                                       tp=0.0, kind=OrderKind.MARKET, magic=settings.magic, comment="TLAB:X"))

    def deverrouiller():
        # le broker simulé enchaîne des pertes : on lève le verrou pour tester le pré-filtre, pas le verrou
        o.state.consecutive_losses, o.state.new_trades_locked, o.state.lock_reasons = 0, False, []

    deverrouiller()
    s3 = o.cycle()
    assert s3.get("candidates", 0) >= 1 and fake.calls == 0, s3
    skipped = [e["candidate"]["review"].get("llm_skipped", "") for e in o.journal.read_day(kinds={"candidate"})]
    assert any("déjà ouverte" in x for x in skipped), skipped[-5:]
    # symboles libérés : les revues repartent
    for pos in broker.positions(magic=settings.magic):
        broker.close_position(pos.ticket)
    deverrouiller()
    broker.set_now(broker.now() + timedelta(minutes=5))
    s4 = o.cycle()
    n4 = int(s4.get("candidates", 0))
    derniers = [e["candidate"]["review"].get("llm_skipped", "") for e in o.journal.read_day(kinds={"candidate"})][-n4:]
    assert n4 >= 1 and not any("déjà ouverte" in x for x in derniers), derniers


def test_candidat_persiste_avec_le_plan_et_repris_apres_redemarrage(settings, broker, tmp_path):
    """Audit 2026-09-23 : 32 trades sur 69 sans verdict de revue dans `learning.db`. Le candidat d'entrée
    ne vivait qu'en mémoire (`candidates_cache`) : après un redémarrage, la clôture était enregistrée sans
    revue ni session. Le plan de position porte désormais le candidat, survit à la sérialisation de
    l'état, et `_on_position_closed` s'en sert quand le cache mémoire est vide."""
    import json

    from tradinglab.core.state import BotPositionPlan, StateStore

    from test_review_market_agents_orchestration import make_orch

    plan = BotPositionPlan(ticket=4242, symbol="EURUSD", side="BUY", agent_id="B01", candidate_id="cand-1", entry=1.1,
                           initial_sl=1.09, initial_volume=0.1, initial_risk_money=100.0, risk_percent=0.05,
                           candidate={"session": "LONDON", "review": {"verdict": "APPROVE", "rationale": "ok"}})
    store = StateStore(tmp_path / "state")
    store.state.bot_positions["4242"] = plan
    store.save()
    relu = StateStore(tmp_path / "state").state
    assert relu.bot_positions["4242"].candidate["review"]["verdict"] == "APPROVE"
    # clôture après redémarrage : cache mémoire vide, le candidat du plan alimente l'enregistrement
    o = make_orch(settings, broker)
    o.candidates_cache.clear()
    o._on_position_closed(plan, broker.now())
    row = o.learning.conn.execute("SELECT review, session FROM trades WHERE ticket=4242").fetchone()
    assert row is not None and json.loads(row[0])["verdict"] == "APPROVE" and row[1] == "LONDON"


def test_heartbeat_rafraichi_pendant_un_chargement_long(settings, broker, monkeypatch):
    """Audit 2026-09-23 : premier cycle de 159 s (chargement des barres de tout l'univers) sans heartbeat,
    le watchdog déclarait l'orchestrateur mort (45 s). Le heartbeat est re-persisté toutes les 15 s
    pendant la boucle de snapshots."""
    from tradinglab.orchestration import orchestrator as orch_mod

    from test_review_market_agents_orchestration import make_orch

    o = make_orch(settings, broker)
    o.cycle()
    horloge = {"t": 1000.0}

    def monotonic_lent():
        horloge["t"] += 6.0                       # chaque appel : 6 s de plus (≈ 1 symbole = 6 s)
        return horloge["t"]

    monkeypatch.setattr(orch_mod.time, "monotonic", monotonic_lent)
    saves = {"n": 0}
    original = o.store.save

    def save_compte():
        saves["n"] += 1
        return original()

    monkeypatch.setattr(o.store, "save", save_compte)
    n_sym = len(o.universe)
    o.cycle()
    # au moins un rafraîchissement toutes les 15 s « d'horloge » sur ~6 s par symbole : ≥ n_sym*6/15 - 1 sauvegardes
    assert saves["n"] >= max(1, int(n_sym * 6 / orch_mod.HEARTBEAT_REFRESH_SEC) - 1), saves


def test_coherence_de_l_etat_sur_le_profit_net_du_cycle():
    """Audit dashboard 2026-09-23 : l'accueil affichait 6,6 % (meilleure idée / somme des gains bruts) là où le
    cycle de paiement montrait 45,6 % (meilleure idée / profit NET du cycle, la règle FOXX). Une seule définition."""
    from datetime import timedelta

    from tradinglab.core.state import SystemState

    from test_risk_and_gate import FIXED_NOW

    st = SystemState()
    st.update_equity(100_000.0, 100_000.0)
    for i, pnl in enumerate([1_000.0, 1_000.0, -1_000.0], start=1):
        st.register_trade_idea(f"P{i}", "BUY", 100.0, ticket=i, now=FIXED_NOW + timedelta(hours=i))
        st.close_trade_idea_position(i, pnl, FIXED_NOW + timedelta(hours=i, minutes=30))
    assert st.consistency_share_percent() == 100.0, "1 000 / profit net 1 000 (et non 1 000 / 2 000 de gains bruts)"
    st.record_payout(500.0, FIXED_NOW + timedelta(days=1))
    assert st.consistency_share_percent() == 0.0, "nouveau cycle : les idées d'avant ne comptent plus"


def test_store_en_lecture_seule_ne_met_jamais_l_etat_de_cote(tmp_path):
    """Audit dashboard 2026-09-23 : `StateStore.load()` renommait `system_state.json` en `.corrupt.json` sur toute
    erreur de parse — le dashboard (lecture toutes les 5 s) pouvait donc détruire l'état de l'orchestrateur sur une
    lecture déchirée pendant l'écriture. En mode `readonly`, on garde le dernier état connu et on ne renomme rien."""
    from tradinglab.core.state import StateStore

    d = tmp_path / "state"
    w = StateStore(d)
    w.state.restarts = 7
    w.save()
    r = StateStore(d, readonly=True)
    assert r.state.restarts == 7
    (d / "system_state.json").write_text("{tronqu", encoding="utf-8")   # écriture en cours vue à moitié
    assert r.reload().restarts == 7 and (d / "system_state.json").exists()
    assert not (d / "system_state.corrupt.json").exists()
    # l'écrivain, lui, garde le comportement historique (fichier mis de côté, état neuf)
    assert w.reload().restarts == 0 and (d / "system_state.corrupt.json").exists()


def test_reset_losses_leve_seulement_le_verrou_pertes_consecutives(settings, broker):
    """Demande utilisateur 2026-09-24 : lever le verrou MAX_CONSECUTIVE_LOSSES. RESET_LOSSES remet le compteur à zéro
    (le Daily Guard ne réarme donc plus le verrou) sans toucher aux autres verrous."""
    from test_review_market_agents_orchestration import make_orch
    from tradinglab.api.commands import ACTION_COMMANDS
    from tradinglab.dashboards.server import PANEL_COMMANDS

    assert "RESET_LOSSES" in ACTION_COMMANDS and "RESET_LOSSES" in PANEL_COMMANDS
    o = make_orch(settings, broker)
    st = o.state
    st.consecutive_losses = 3
    st.lock_entries("MAX_CONSECUTIVE_LOSSES")
    st.lock_entries("GIVEBACK_FLOOR")
    res = o.handle_command("RESET_LOSSES", {}, "test")
    assert res["ok"] and res["consecutive_losses_before"] == 3 and st.consecutive_losses == 0
    assert st.lock_reasons == ["GIVEBACK_FLOOR"], "les autres verrous restent en place"
    o.daily.evaluate(st)
    assert "MAX_CONSECUTIVE_LOSSES" not in st.lock_reasons


def test_break_even_sans_ticket_arme_toutes_les_positions_possibles(settings, broker):
    """Capture utilisateur 2026-09-24 : le bouton « Armer partout » envoyait BREAK_EVEN sans ticket → « ticket
    invalide ». Sans ticket, le break-even est tenté sur chaque position du bot ; celles pas assez en profit restent."""
    from datetime import timedelta

    from tradinglab.core.types import OrderKind, OrderRequest, Side

    from test_review_market_agents_orchestration import make_orch

    o = make_orch(settings, broker)
    for p in broker.positions(magic=settings.magic):
        broker.close_position(p.ticket)
    o.pm.sync()
    o.state.bot_positions.clear()
    t = broker.tick("EURUSD")
    gagnante = broker.order_send(OrderRequest(symbol="EURUSD", side=Side.BUY, volume=0.1, sl=round(t.ask - 0.0050, 5), tp=0.0,
                                              kind=OrderKind.MARKET, magic=settings.magic, comment="TLAB:X"))
    t2 = broker.tick("GBPUSD")
    perdante = broker.order_send(OrderRequest(symbol="GBPUSD", side=Side.BUY, volume=0.1, sl=round(t2.ask - 0.0050, 5), tp=0.0,
                                              kind=OrderKind.MARKET, magic=settings.magic, comment="TLAB:Y"))
    o.pm.sync()
    broker.set_price("EURUSD", t.bid + 0.0060)          # +1,2 R : break-even possible
    res = o.handle_command("BREAK_EVEN", {}, "dashboard")
    assert res["ok"] and [a["ticket"] for a in res["armed"]] == [gagnante.ticket]
    assert [s_["ticket"] for s_ in res["skipped"]] == [perdante.ticket]
    assert o.state.bot_positions[str(gagnante.ticket)].break_even_done
    assert broker.position(gagnante.ticket).sl >= o.state.bot_positions[str(gagnante.ticket)].entry
    again = o.handle_command("BREAK_EVEN", {"target": "ALL"}, "dashboard")
    assert again["armed"] == [] and any(s_["raison"] == "déjà au break-even" for s_ in again["skipped"])
    assert o.handle_command("BREAK_EVEN", {"target": "abc"}, "cli")["ok"] is False


def test_encaisser_les_gagnantes_laisse_les_perdantes(settings, broker):
    """Demande utilisateur 2026-09-24 : un bouton qui ferme immédiatement toutes les positions en positif et laisse
    les positions en négatif actives."""
    from tradinglab.api.commands import ACTION_COMMANDS
    from tradinglab.core.types import OrderKind, OrderRequest, Side
    from tradinglab.dashboards.server import CONTROL_HTML, PANEL_COMMANDS

    from test_review_market_agents_orchestration import make_orch

    assert "CLOSE_WINNERS" in ACTION_COMMANDS and "CLOSE_WINNERS" in PANEL_COMMANDS
    assert "ask('CLOSE_WINNERS',this)" in CONTROL_HTML and "CLOSE_WINNERS:\"Fermer maintenant" in CONTROL_HTML
    o = make_orch(settings, broker)
    for p in broker.positions(magic=settings.magic):
        broker.close_position(p.ticket)
    t, t2 = broker.tick("EURUSD"), broker.tick("GBPUSD")
    g = broker.order_send(OrderRequest(symbol="EURUSD", side=Side.BUY, volume=0.1, sl=round(t.ask - 0.01, 5), tp=0.0,
                                       kind=OrderKind.MARKET, magic=settings.magic, comment="TLAB:G"))
    l_ = broker.order_send(OrderRequest(symbol="GBPUSD", side=Side.BUY, volume=0.1, sl=round(t2.ask - 0.01, 5), tp=0.0,
                                        kind=OrderKind.MARKET, magic=settings.magic, comment="TLAB:L"))
    o.pm.sync()
    broker.set_price("EURUSD", t.bid + 0.0030)
    broker.set_price("GBPUSD", t2.bid - 0.0030)
    res = o.handle_command("CLOSE_WINNERS", {}, "dashboard")
    assert res["ok"] and [c["ticket"] for c in res["closed"]] == [g.ticket] and res["closed"][0]["ok"]
    assert [k["ticket"] for k in res["kept"]] == [l_.ticket] and res["gain_total"] > 0
    assert broker.position(g.ticket) is None and broker.position(l_.ticket) is not None
