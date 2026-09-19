"""Règles FOXX Funded relevées le 2026-09-19 (docs/prop/foxx_funded_regles.md).

Ces tests vérifient que le code applique les BASES DE CALCUL de la prop firm, et pas des
approximations : journée qui bascule à 17:00 America/New_York, plancher journalier assis sur
max(solde, equity) au reset moins 4 % du solde initial, drawdown total statique sur le solde initial,
agrégation des positions en « idées de trade » plafonnées à 2 %, règle de cohérence à 25 %.
"""
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from tradinglab.core.state import SystemState
from tradinglab.core.trading_day import TradingDayCalendar, prop_trading_day
from tradinglab.core.types import TradeMode
from tradinglab.risk.prop_guard import PropGuard, PropProfile

from tests.conftest import FIXED_NOW
from tests.test_risk_and_gate import ctx_for, gate_for, make_candidate


def profile(settings, **over) -> PropProfile:
    return PropProfile.from_config(dict(settings.prop, **over))


def guard(settings, **over) -> PropGuard:
    return PropGuard(profile(settings, **over), True, False, 1.0)


def state_at(equity: float, balance: float, now: datetime, cal: TradingDayCalendar | None = None) -> SystemState:
    st = SystemState()
    st.roll_day_if_needed(equity, balance, now, cal or TradingDayCalendar())
    st.update_equity(equity, balance)
    return st


# ---------------- configuration relevée ----------------
def test_config_reflete_les_regles_officielles():
    cfg = yaml.safe_load(open("config/prop_firms.yaml", encoding="utf-8"))["prop"]
    assert cfg["prop_firm"] == "FOXX_FUNDED"
    assert (cfg["profit_target_percent"], cfg["max_daily_loss_hard_percent"], cfg["max_overall_loss_hard_percent"]) == (7.0, 4.0, 8.0)
    assert cfg["max_risk_per_trade_idea_percent"] == 2.0 and cfg["trade_idea_aggregation_minutes"] == 10
    assert cfg["consistency_max_share_percent"] == 25.0 and cfg["min_trading_days"] == 5
    assert cfg["news_trading_window_minutes"] == 5 and cfg["news_trading_allowed_on_funded"] is False
    assert cfg["trading_day_reset_hour"] == 17 and cfg["trading_day_timezone"] == "America/New_York"
    assert cfg["drawdown_type"] == "STATIC" and cfg["loss_reference_balance"] == "INITIAL_BALANCE"
    assert cfg["weekend_trading_allowed"] == "CRYPTO_ONLY" and cfg["stop_loss_mandatory"] is True
    assert cfg["rules_source_url"].startswith("https://www.foxx-funded.com") and cfg["rules_verified_at"]
    # les deux verrous humains restent fermés : aucune exécution prop tant que l'utilisateur n'a pas tranché
    assert cfg["user_explicitly_authorized_prop_automation"] is False and cfg["ea_approval_obtained"] is False


def test_profil_charge_les_regles_et_bloque_sans_approbation_ea(settings):
    p = profile(settings)
    assert not p.ambiguous and p.trade_idea_aggregation_minutes == 10 and p.max_risk_per_trade_idea_percent == 2.0
    assert p.ea_approval_missing
    pg = PropGuard(p, True, True, 1.0)
    assert not pg.prop_automation_allowed
    assert not pg.authorization(TradeMode.REAL).ok and pg.authorization(TradeMode.DEMO).ok


# ---------------- journée de trading : reset 17:00 America/New_York ----------------
def test_journee_bascule_a_17h_new_york_pas_a_minuit_utc():
    cal = TradingDayCalendar()
    assert not cal.degraded, "base de fuseaux indisponible : installer tzdata"
    # été (EDT, UTC−4) : 20:59 UTC = 16:59 NY → encore la journée du 15
    assert cal.day(datetime(2026, 9, 15, 20, 59, tzinfo=timezone.utc)).isoformat() == "2026-09-15"
    assert cal.day(datetime(2026, 9, 15, 21, 0, tzinfo=timezone.utc)).isoformat() == "2026-09-16"
    # hiver (EST, UTC−5) : la bascule a lieu à 22:00 UTC
    assert cal.day(datetime(2026, 1, 20, 21, 59, tzinfo=timezone.utc)).isoformat() == "2026-01-20"
    assert cal.day(datetime(2026, 1, 20, 22, 0, tzinfo=timezone.utc)).isoformat() == "2026-01-21"
    # minuit UTC ne change rien : c'est le milieu de la journée prop
    assert cal.day(datetime(2026, 1, 20, 23, 30, tzinfo=timezone.utc)) == cal.day(datetime(2026, 1, 21, 3, 0, tzinfo=timezone.utc))
    assert prop_trading_day(FIXED_NOW) == cal.day(FIXED_NOW)


def test_reset_at_est_l_instant_utc_du_dernier_17h_new_york():
    cal = TradingDayCalendar()
    now = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
    assert cal.reset_at(now) == datetime(2026, 9, 15, 21, 0, tzinfo=timezone.utc)
    assert cal.next_reset(now) == datetime(2026, 9, 16, 21, 0, tzinfo=timezone.utc)


def test_roll_day_suit_le_reset_prop_et_non_la_date_utc():
    cal = TradingDayCalendar()
    st = SystemState()
    before = datetime(2026, 9, 15, 22, 0, tzinfo=timezone.utc)   # 18:00 NY : journée prop du 16
    assert st.roll_day_if_needed(100_000, 100_000, before, cal) is True
    day = st.daily.day
    assert day == "2026-09-16"
    # minuit UTC passé : toujours la même journée prop, la perte du jour n'est PAS remise à zéro
    assert st.roll_day_if_needed(99_000, 99_000, datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc), cal) is False
    assert st.daily.day == day and st.daily.reference_equity == 100_000
    # milieu de la journée prop (matin de Londres) : toujours pas de bascule
    assert st.roll_day_if_needed(99_000, 99_000, datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc), cal) is False
    # après 17:00 NY : nouvelle journée prop
    assert st.roll_day_if_needed(99_000, 99_000, datetime(2026, 9, 16, 21, 1, tzinfo=timezone.utc), cal) is True
    assert st.daily.day == "2026-09-17"


def test_reference_du_jour_est_le_max_solde_equity_au_reset():
    cal = TradingDayCalendar()
    # equity flottante inférieure au solde au moment du reset : la base retenue est le SOLDE
    st = state_at(equity=99_000, balance=100_000, now=FIXED_NOW, cal=cal)
    assert st.daily.reference_equity == 100_000
    assert st.prop_daily_loss_percent() == pytest.approx(1.0)
    # et inversement quand l'equity est au-dessus du solde
    st2 = state_at(equity=101_000, balance=100_000, now=FIXED_NOW, cal=cal)
    assert st2.daily.reference_equity == 101_000
    st2.update_equity(100_000, 100_000)
    assert st2.prop_daily_loss_percent() == pytest.approx(1.0)


def test_plancher_journalier_vaut_reference_moins_4_pourcent_du_solde_initial():
    st = state_at(equity=99_000, balance=100_000, now=FIXED_NOW)
    assert st.prop_reference_balance() == 100_000
    assert st.prop_daily_floor(4.0) == pytest.approx(96_000.0)
    st.update_equity(96_100, 100_000)
    assert st.prop_daily_loss_percent() == pytest.approx(3.9)


# ---------------- perte totale : drawdown STATIQUE ----------------
def test_perte_totale_mesuree_sur_le_solde_initial_jamais_sur_un_pic():
    st = state_at(equity=100_000, balance=100_000, now=FIXED_NOW)
    st.update_equity(120_000, 120_000)          # gros gain : le pic monte
    st.update_equity(100_000, 100_000)          # retour au point de départ
    assert st.initial_balance == 100_000
    assert st.prop_overall_loss_percent() == pytest.approx(0.0)   # règle prop : aucune perte
    assert st.overall_drawdown_percent() > 15.0                    # mesure interne (pic), volontairement plus stricte
    st.update_equity(92_000, 92_000)
    assert st.prop_overall_loss_percent() == pytest.approx(8.0)    # exactement la limite de rupture
    assert st.prop_overall_floor(8.0) == pytest.approx(92_000.0)


def test_solde_initial_fige_a_la_premiere_synchronisation():
    st = state_at(equity=100_000, balance=100_000, now=FIXED_NOW)
    st.update_equity(150_000, 150_000)
    assert st.initial_balance == 100_000, "le solde de référence ne suit pas les profits (drawdown statique)"


def test_limites_prop_sont_exprimees_en_pourcentage_du_solde_initial(settings):
    pg = guard(settings, account_size=100_000)
    st = state_at(equity=100_000, balance=100_000, now=FIXED_NOW)
    st.update_equity(120_000, 120_000)
    st.update_equity(97_500, 97_500)
    checks = {c.name: c for c in pg.limits(st)}
    # 2 500 € perdus sur 100 000 € de solde initial = 2,5 % — et non 2,08 % rapportés à l'equity du pic
    assert "2.500%" in checks["prop_hard_daily"].detail
    assert checks["prop_hard_daily"].ok and checks["prop_hard_overall"].ok
    st.update_equity(96_900, 96_900)            # 3,1 % > marge de sécurité (4 % × 0,75)
    assert not {c.name: c for c in pg.limits(st)}["prop_hard_daily"].ok


# ---------------- idée de trade : agrégation et plafond 2 % ----------------
def test_positions_dans_le_meme_sens_forment_une_seule_idee():
    st = state_at(equity=100_000, balance=100_000, now=FIXED_NOW)
    i1 = st.register_trade_idea("EURUSD", "BUY", 250.0, ticket=1, now=FIXED_NOW)
    i2 = st.register_trade_idea("EURUSD", "BUY", 250.0, ticket=2, now=FIXED_NOW + timedelta(minutes=3))
    assert i1.idea_id == i2.idea_id and i2.risk_money == 500.0 and i2.entries == 2
    # sens opposé : idée distincte
    i3 = st.register_trade_idea("EURUSD", "SELL", 250.0, ticket=3, now=FIXED_NOW + timedelta(minutes=3))
    assert i3.idea_id != i1.idea_id
    # autre symbole : idée distincte
    i4 = st.register_trade_idea("GBPUSD", "BUY", 250.0, ticket=4, now=FIXED_NOW + timedelta(minutes=3))
    assert i4.idea_id != i1.idea_id


def test_reouverture_sous_10_minutes_reste_la_meme_idee():
    st = state_at(equity=100_000, balance=100_000, now=FIXED_NOW)
    i1 = st.register_trade_idea("EURUSD", "BUY", 250.0, ticket=1, now=FIXED_NOW)
    st.close_trade_idea_position(1, -250.0, FIXED_NOW + timedelta(minutes=2))
    same = st.register_trade_idea("EURUSD", "BUY", 250.0, ticket=2, now=FIXED_NOW + timedelta(minutes=9))
    assert same.idea_id == i1.idea_id, "rouvrir dans le même sens sous 10 min reste la même idée"
    assert same.risk_money == 500.0, "le risque déjà encaissé ne libère pas de budget"
    st.close_trade_idea_position(2, -250.0, FIXED_NOW + timedelta(minutes=10))
    later = st.register_trade_idea("EURUSD", "BUY", 250.0, ticket=3, now=FIXED_NOW + timedelta(minutes=30))
    assert later.idea_id != i1.idea_id and later.risk_money == 250.0


def test_idee_reste_ouverte_tant_qu_une_position_vit():
    st = state_at(equity=100_000, balance=100_000, now=FIXED_NOW)
    i1 = st.register_trade_idea("EURUSD", "BUY", 250.0, ticket=1, now=FIXED_NOW)
    late = st.register_trade_idea("EURUSD", "BUY", 250.0, ticket=2, now=FIXED_NOW + timedelta(hours=5))
    assert late.idea_id == i1.idea_id, "des positions simultanées dans le même sens sont une seule idée"


def test_plafond_de_risque_par_idee(settings):
    pg = guard(settings, account_size=100_000)
    st = state_at(equity=100_000, balance=100_000, now=FIXED_NOW)
    st.register_trade_idea("EURUSD", "BUY", 1_400.0, ticket=1, now=FIXED_NOW)
    ok = {c.name: c for c in pg.limits(st, 50.0, symbol="EURUSD", side="BUY", now=FIXED_NOW)}
    assert ok["prop_trade_idea_risk"].ok                      # 1,45 % < 1,5 % (2 % × 0,75)
    ko = {c.name: c for c in pg.limits(st, 200.0, symbol="EURUSD", side="BUY", now=FIXED_NOW)}
    assert not ko["prop_trade_idea_risk"].ok                  # 1,60 % ≥ marge
    autre = {c.name: c for c in pg.limits(st, 200.0, symbol="EURUSD", side="SELL", now=FIXED_NOW)}
    assert autre["prop_trade_idea_risk"].ok                   # le sens opposé est une autre idée


def test_gate_applique_le_plafond_par_idee(settings, broker):
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    acc = broker.account_info()
    st = state_at(equity=acc.equity, balance=acc.balance, now=FIXED_NOW)
    from tradinglab.core.state import SystemMode
    st.set_mode(SystemMode.AUTO, "")
    st.account_trade_mode = "DEMO"
    res, _req = gate.evaluate(ctx_for(broker, c, st, atr))
    names = [ch.name for ch in res.checks]
    assert {n for n in names if n.startswith("19_")} == {
        "19_prop_hard_daily", "19_prop_hard_overall", "19_prop_trade_idea", "19_prop_weekend"}
    assert dict((ch.name, ch.ok) for ch in res.checks)["19_prop_trade_idea"] is True
    # idée déjà chargée à 1,9 % du solde initial : le gate refuse l'entrée supplémentaire
    st.register_trade_idea(c.symbol, c.side.value, 0.019 * st.prop_reference_balance(), ticket=99, now=FIXED_NOW)
    res2, req2 = gate.evaluate(ctx_for(broker, c, st, atr))
    assert not res2.approved and req2 is None
    assert dict((ch.name, ch.ok) for ch in res2.checks)["19_prop_trade_idea"] is False


# ---------------- cohérence 25 % ----------------
def test_regle_de_coherence_25_pourcent(settings):
    pg = guard(settings)
    st = state_at(equity=100_000, balance=100_000, now=FIXED_NOW)
    assert pg.consistency_status(st)["share_percent"] == 0.0        # aucun profit : rien à contrôler
    for i, pnl in enumerate([1_000.0, 1_000.0, 1_000.0, 1_000.0], start=1):
        st.register_trade_idea("EURUSD", "BUY", 250.0, ticket=i, now=FIXED_NOW + timedelta(hours=i))
        st.close_trade_idea_position(i, pnl, FIXED_NOW + timedelta(hours=i, minutes=30))
    status = pg.consistency_status(st)
    assert status["share_percent"] == pytest.approx(25.0) and status["within_limit"]
    st.register_trade_idea("GBPUSD", "BUY", 250.0, ticket=9, now=FIXED_NOW + timedelta(hours=9))
    st.close_trade_idea_position(9, 6_000.0, FIXED_NOW + timedelta(hours=10))
    status = pg.consistency_status(st)
    assert status["share_percent"] == pytest.approx(60.0) and not status["within_limit"]
    # non bloquante : chez FOXX elle est contrôlée au paiement et n'élimine pas le compte
    assert status["blocking"] is False
    assert all(c.ok for c in pg.limits(st) if c.name.startswith("prop_"))


def test_rapport_de_conformite_expose_les_bases_prop(settings):
    pg = guard(settings, account_size=100_000)
    st = state_at(equity=99_000, balance=100_000, now=FIXED_NOW)
    rep = pg.compliance_report(st)
    assert rep["prop_automation_allowed"] is False and rep["blocking_reasons"]
    assert rep["reference_balance"] == 100_000 and rep["prop_daily_floor"] == pytest.approx(96_000.0)
    assert rep["prop_overall_floor"] == pytest.approx(92_000.0)
    assert rep["trading_day"] == "reset 17:00 America/New_York"
    assert rep["rules_source_url"].startswith("https://www.foxx-funded.com")
    assert rep["consistency"]["max_share_percent"] == 25.0


# ---------------- persistance ----------------
def test_idees_de_trade_survivent_au_redemarrage(tmp_path):
    from tradinglab.core.state import StateStore
    store = StateStore(tmp_path)
    store.state.roll_day_if_needed(100_000, 100_000, FIXED_NOW, TradingDayCalendar())
    store.state.update_equity(100_000, 100_000)
    idea = store.state.register_trade_idea("EURUSD", "BUY", 250.0, ticket=1, now=FIXED_NOW)
    store.save()
    again = StateStore(tmp_path).state
    assert again.initial_balance == 100_000 and again.daily.reference_equity == 100_000
    assert again.trade_ideas[idea.idea_id].risk_money == 250.0
    assert again.trade_ideas[idea.idea_id].open_tickets == [1]
    # le risque de l'idée reste opposable après restart
    assert again.projected_trade_idea_risk("EURUSD", "BUY", 100.0, FIXED_NOW) == 350.0


def test_etat_ancien_sans_champs_prop_reste_lisible(tmp_path):
    import json
    from tradinglab.core.state import StateStore
    (tmp_path / "system_state.json").write_text(json.dumps({
        "equity": 99_000.0, "balance": 100_000.0, "trade_ideas": "corrompu",
        "daily": {"day": "2026-01-20", "starting_equity": 100_000.0, "starting_balance": 100_000.0},
    }), encoding="utf-8")
    st = StateStore(tmp_path).state
    assert st.trade_ideas == {} and any("trade_ideas" in w for w in st.load_warnings)
    # la base prop manquante est reconstruite à partir des champs existants, sans rouvrir la journée
    assert st.roll_day_if_needed(99_000.0, 100_000.0, datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc),
                                 TradingDayCalendar()) is False
    assert st.daily.reference_equity == 100_000.0


# ---------------- week-end : cryptomonnaies uniquement ----------------
SAMEDI = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)      # samedi, journée prop du 19
VENDREDI = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)    # vendredi 08:00 NY


def test_weekend_reserve_aux_cryptos(settings):
    pg = guard(settings)
    assert pg.weekend_check("forex", VENDREDI).ok
    assert pg.weekend_check("crypto", VENDREDI).ok
    assert pg.weekend_check("crypto", SAMEDI).ok
    for cls in ("forex", "indices", "metals", ""):
        assert not pg.weekend_check(cls, SAMEDI).ok, cls
    # le week-end du marché se ferme le vendredi à 17:00 New York, pas à minuit UTC
    vendredi_soir = datetime(2026, 9, 18, 21, 30, tzinfo=timezone.utc)
    assert not pg.weekend_check("forex", vendredi_soir).ok
    dimanche_soir = datetime(2026, 9, 20, 21, 30, tzinfo=timezone.utc)   # réouverture lundi
    assert pg.weekend_check("forex", dimanche_soir).ok


def test_limits_inclut_la_regle_de_week_end(settings):
    pg = guard(settings)
    st = state_at(equity=100_000, balance=100_000, now=VENDREDI)
    checks = {c.name: c.ok for c in pg.limits(st, 0.0, symbol="EURUSD", side="BUY", now=SAMEDI, asset_class="forex")}
    assert checks["prop_weekend_sessions"] is False
    checks = {c.name: c.ok for c in pg.limits(st, 0.0, symbol="BTCUSD", side="BUY", now=SAMEDI, asset_class="crypto")}
    assert checks["prop_weekend_sessions"] is True
    # sans instant fourni, la règle n'est pas devinée : elle ne bloque pas mais le dit
    assert {c.name: c.ok for c in pg.limits(st)}["prop_weekend_sessions"] is True


# ---------------- activité minimale ----------------
def test_jours_de_trading_et_activite_minimale(settings):
    pg = guard(settings)
    st = state_at(equity=100_000, balance=100_000, now=FIXED_NOW)
    act = pg.activity_status(st)
    assert act["trading_days"] == 0 and act["min_trading_days"] == 5 and not act["min_trading_days_reached"]
    assert act["days_since_last_trade"] is None
    cal = TradingDayCalendar()
    for d in range(5):
        now = FIXED_NOW + timedelta(days=d)
        st.roll_day_if_needed(100_000, 100_000, now, cal)
        st.record_trading_day(now)
        st.record_trading_day(now)          # deux entrées le même jour = un seul jour de trading
    assert pg.activity_status(st)["trading_days"] == 5
    assert pg.activity_status(st)["min_trading_days_reached"]
    assert pg.activity_status(st, FIXED_NOW + timedelta(days=4, hours=2))["weekly_activity_at_risk"] is False
    tardif = pg.activity_status(st, FIXED_NOW + timedelta(days=11))
    assert tardif["days_since_last_trade"] == pytest.approx(7.0) and tardif["weekly_activity_at_risk"]


# ---------------- fenêtre news : l'interne ne doit jamais être plus laxiste que la prop ----------------
def test_fenetre_news_interne_plus_large_que_la_regle_prop():
    news = yaml.safe_load(open("config/news_sources.yaml", encoding="utf-8"))["news"]
    prop = yaml.safe_load(open("config/prop_firms.yaml", encoding="utf-8"))["prop"]
    window = prop["news_trading_window_minutes"]
    assert news["block_minutes_before_high_impact"] >= window
    assert news["block_minutes_after_high_impact"] >= window


# ---------------- watchdog : alerte sur la base prop, pas sur un pic d'equity ----------------
def test_watchdog_alerte_sur_la_base_prop(settings, broker, tmp_path):
    from tradinglab.core.journal import Journal
    from tradinglab.core.state import StateStore
    from tradinglab.monitoring.watchdog import Watchdog

    store = StateStore(tmp_path / "state")
    acc = broker.account_info()
    store.state.roll_day_if_needed(acc.equity, acc.balance, FIXED_NOW, TradingDayCalendar())
    store.state.update_equity(acc.equity, acc.balance)
    store.save()
    wd = Watchdog(settings, broker, store, Journal(tmp_path / "logs", component="wd"))
    rep = wd.check_once()
    assert rep.prop_daily_loss_percent == pytest.approx(0.0)
    assert rep.prop_overall_loss_percent == pytest.approx(0.0)
    assert not any("hard limits" in r for r in rep.reasons)
    # un gros gain puis un retour au point de départ ne doit PAS déclencher l'alerte prop
    store.state.update_equity(acc.equity * 1.2, acc.balance * 1.2)
    store.state.update_equity(acc.equity, acc.balance)
    store.save()
    rep = wd.check_once()
    assert rep.overall_dd_percent > 15.0 and rep.prop_overall_loss_percent == pytest.approx(0.0)
    assert not any("hard limits" in r for r in rep.reasons)
