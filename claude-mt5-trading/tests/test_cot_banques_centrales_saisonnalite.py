"""Ajouts du 2026-09-21 soir (décision utilisateur « tout ce qui peut être utile ») :

1. COT (CFTC) en filtre de contexte : avertissement quand le trade suit un positionnement
   spéculatif déjà extrême — jamais bloquant, jamais de donnée inventée.
2. Fenêtres news DOUBLÉES autour des banques centrales (60/30 min au lieu de 30/15).
3. Famille N (saisonnalité) : N01 biais du jour de semaine, N02 retournement de mois — SHADOW.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from tradinglab.agents import screeners
from tradinglab.agents.registry import default_agents
from tradinglab.agents.strategies import n_seasonality  # noqa: F401 - l'import enregistre N01/N02
from tradinglab.core.types import Side, TradeCandidate
from tradinglab.macro.cot import MARKETS, COTClient
from tradinglab.news.hub import CalendarEvent, NewsHub, is_central_bank_event


# ---------------------------------------------------------------- 1. COT
def _cot_rows(code: str, weeks: int, net_fn, end="2026-09-15"):
    end_d = datetime.strptime(end, "%Y-%m-%d")
    rows = []
    for i in range(weeks):
        d = end_d - timedelta(weeks=weeks - 1 - i)
        net = net_fn(i)
        rows.append({"cftc_contract_market_code": code, "report_date_as_yyyy_mm_dd": d.strftime("%Y-%m-%d"),
                     "noncomm_positions_long_all": str(max(net, 0) + 100000), "noncomm_positions_short_all": str(max(-net, 0) + 100000)})
    return rows


def _client(tmp_path, rows):
    return COTClient(tmp_path / "cot.json", http_get=lambda url: json.dumps(rows).encode())


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def test_cot_index_extreme_ajoute_un_argument_contre(tmp_path):
    """Net spéculatif au plus haut de 3 ans sur l'euro → un BUY EURUSD reçoit l'avertissement, un SELL non."""
    rows = _cot_rows("099741", 156, lambda i: i * 1000)          # net croissant : dernier = max de la fenêtre
    cl = _client(tmp_path, rows)
    assert cl.maybe_refresh(NOW)
    ctx = cl.context("EURUSD", NOW)
    assert ctx is not None and ctx["cot_index"] == 100.0 and ctx["market"] == "Euro FX"
    buy = TradeCandidate("EURUSD", Side.BUY, 1.1, 1.09, [1.12], ["M15"], regime=None, agent_id="B01")
    sell = TradeCandidate("EURUSD", Side.SELL, 1.1, 1.11, [1.08], ["M15"], regime=None, agent_id="B01")
    cl.annotate(buy)
    cl.annotate(sell)
    assert any("COT" in a and "extrême acheteur" in a for a in buy.arguments_against)
    assert not sell.arguments_against


def test_cot_sens_inverse_pour_les_paires_usd_base(tmp_path):
    """USDJPY BUY = short Yen futures : l'extrême VENDEUR sur le yen avertit l'ACHAT d'USDJPY."""
    rows = _cot_rows("097741", 156, lambda i: -i * 1000)         # net yen au plus vendeur en fin de fenêtre
    cl = _client(tmp_path, rows)
    cl.maybe_refresh(NOW)
    buy = TradeCandidate("USDJPY", Side.BUY, 150.0, 149.0, [152.0], ["M15"], regime=None, agent_id="B01")
    cl.annotate(buy)
    assert any("extrême vendeur" in a for a in buy.arguments_against)


def test_cot_jamais_de_donnee_inventee(tmp_path):
    """Marché non couvert, historique court ou rapport trop vieux → aucun contexte, aucune annotation."""
    cl = _client(tmp_path, _cot_rows("099741", 30, lambda i: i))            # < min_weeks
    cl.maybe_refresh(NOW)
    assert cl.context("EURUSD", NOW) is None and cl.context("EURJPY", NOW) is None
    old = _client(tmp_path, _cot_rows("099741", 156, lambda i: i, end="2026-01-06"))
    old.maybe_refresh(NOW)
    assert old.context("EURUSD", NOW) is None                               # rapport de plus de 21 jours
    c = TradeCandidate("EURUSD", Side.BUY, 1.1, 1.09, [1.12], ["M15"], regime=None, agent_id="B01")
    old.annotate(c)
    assert c.arguments_against == []


def test_cot_echec_reseau_conserve_le_cache(tmp_path):
    rows = _cot_rows("099741", 156, lambda i: i * 1000)
    path = tmp_path / "cot.json"
    ok = COTClient(path, http_get=lambda url: json.dumps(rows).encode())
    assert ok.maybe_refresh(NOW)
    def boom(url):
        raise OSError("réseau coupé")
    cl = COTClient(path, http_get=boom)
    assert cl.context("EURUSD", NOW) is not None                            # cache disque rechargé
    assert cl.maybe_refresh(NOW) is False
    assert cl.context("EURUSD", NOW) is not None                            # cache conservé après l'échec
    assert cl.maybe_refresh(NOW + timedelta(minutes=5)) is False            # cooldown : pas de retry immédiat


def test_cot_couverture_des_marches():
    """Le mapping ne couvre que des sous-jacents à future CFTC clair ; les crypto/croisés n'y sont pas."""
    assert {"EURUSD", "USDJPY", "XAUUSD", "XTIUSD"} <= set(MARKETS)
    assert "EURJPY" not in MARKETS and "BTCUSD" not in MARKETS and "XBRUSD" not in MARKETS


# ---------------------------------------------------------------- 2. fenêtres banques centrales
def _hub(tmp_path, events):
    from tests.test_news import CFG, FakeProvider

    hub = NewsHub([FakeProvider(kind="economic_calendar", events=events, name="cal")], CFG)
    hub.refresh_calendar(NOW)
    return hub


def _event(title, minutes_from_now, currency="USD"):
    return CalendarEvent(timestamp=NOW + timedelta(minutes=minutes_from_now), currency=currency,
                         title=title, importance="HIGH", source="test")


def test_is_central_bank_event():
    assert is_central_bank_event("Fed Interest Rate Decision")
    assert is_central_bank_event("ECB Press Conference")
    assert is_central_bank_event("FOMC Statement")
    assert not is_central_bank_event("Nonfarm Payrolls")
    assert not is_central_bank_event("CPI y/y")


def test_fenetre_banque_centrale_doublee(tmp_path):
    """Une décision de taux à 45 min bloque (fenêtre 60) alors qu'un NFP à 45 min ne bloque pas (fenêtre 30)."""
    hub = _hub(tmp_path, [_event("Fed Interest Rate Decision", 45)])
    chk = hub.check(["USD"], NOW)
    assert not chk.ok and chk.state == "BLOCKED_PRE_NEWS" and "banque centrale" in chk.reason
    hub2 = _hub(tmp_path, [_event("Nonfarm Payrolls", 45)])
    assert hub2.check(["USD"], NOW).ok


def test_fenetre_standard_inchangee(tmp_path):
    """Un NFP à 20 min bloque toujours (30/15 min inchangés) ; après l'événement, 30 min pour une BC, 15 sinon."""
    assert not _hub(tmp_path, [_event("Nonfarm Payrolls", 20)]).check(["USD"], NOW).ok
    chk = _hub(tmp_path, [_event("ECB Press Conference", -25, "EUR")]).check(["EUR"], NOW)
    assert not chk.ok and chk.state == "BLOCKED_POST_NEWS"
    assert _hub(tmp_path, [_event("Nonfarm Payrolls", -25)]).check(["USD"], NOW).ok


def test_autre_devise_non_bloquee(tmp_path):
    hub = _hub(tmp_path, [_event("Fed Interest Rate Decision", 45)])
    assert hub.check(["EUR", "JPY"], NOW).ok


# ---------------------------------------------------------------- 3. famille N (saisonnalité)
@pytest.fixture(scope="module")
def n_specs():
    return {a.agent_id: a for a in default_agents() if a.family == "N"}


def test_famille_n_enregistree_en_shadow(n_specs):
    assert set(n_specs) == {"N01", "N02"}
    for a in n_specs.values():
        assert a.status == "SHADOW", "la saisonnalité n'entre jamais LIVE sans passer le pipeline"
        assert a.agent_id in screeners.SCREENERS
        assert a.timeframes == {"entry": "M15", "trend": "D1"}
    assert n_specs["N02"].markets == ["indices"]


def _d1_frame(days, weekday_bias=None, month_turn_bias=None, start="2025-06-02", price=100.0):
    """Frame D1 synthétique enrichissable : biais optionnel sur un jour de semaine ou en fenêtre de fin/début de mois."""
    import calendar as _cal

    from tradinglab.market_data.indicators import enrich
    times, closes = [], []
    d = datetime.strptime(start, "%Y-%m-%d")
    px = price
    made = 0
    prev_month_days: dict = {}
    while made < days:
        if d.weekday() < 5:
            r = 0.0002 * ((made % 7) - 3) / 3          # bruit déterministe minuscule
            if weekday_bias is not None and d.weekday() == weekday_bias[0]:
                r += weekday_bias[1]
            if month_turn_bias is not None:
                dim = _cal.monthrange(d.year, d.month)[1]
                td = prev_month_days.get((d.year, d.month), 0) + 1
                if td <= 3 or (dim - d.day) < 2:
                    r += month_turn_bias
                prev_month_days[(d.year, d.month)] = td
            px *= 1 + r
            times.append(d)
            closes.append(px)
            made += 1
        d += timedelta(days=1)
    df = pd.DataFrame({"time": times, "open": closes, "high": [c * 1.004 for c in closes],
                       "low": [c * 0.996 for c in closes], "close": closes,
                       "tick_volume": [1000] * days, "spread": [10] * days})
    return enrich(df)


def _snap_with_d1(base_snap, d1):
    snap = copy.deepcopy(base_snap)
    snap.frames["D1"] = d1
    return snap


@pytest.fixture(scope="module")
def base_snap():
    from tradinglab.market_data.feed import MarketDataFeed
    from tradinglab.mt5.mock_adapter import MockBroker

    b = MockBroker(seed=7)
    b.connect()
    b.set_now(datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc))   # mercredi
    feed = MarketDataFeed(b, ["M5", "M15", "H1", "H4", "D1"])
    return feed.snapshot("EURUSD", now=b.now())


def _m15_frame(end: str, bars: int = 320, price: float = 1.08):
    """M15 synthétique en tendance douce : la barre clôturée finale est haussière au-dessus de l'EMA20,
    RSI modéré (2 hausses de 0,08 % pour 1 baisse de 0,10 %). Seuls les PRIX sont fabriqués (enrich réel)."""
    from tradinglab.market_data.indicators import enrich
    times = pd.date_range(end=end, periods=bars, freq="15min", tz="UTC")
    closes, px = [], price
    for i in range(bars):
        px *= (1 - 0.0010) if i % 3 == 2 else (1 + 0.0008)
        closes.append(px)
    closes[-2] = closes[-3] * 1.0012                    # barre de signal : clôture haussière
    closes[-1] = closes[-2]
    opens = [closes[0]] + closes[:-1]
    df = pd.DataFrame({"time": times, "open": opens,
                       "high": [max(o, c) * 1.0003 for o, c in zip(opens, closes)],
                       "low": [min(o, c) * 0.9997 for o, c in zip(opens, closes)],
                       "close": closes, "tick_volume": [500] * bars, "spread": [10] * bars})
    return enrich(df)


def _scenario(base_snap, d1, end_m15: str):
    """Snapshot au prix et à l'ATR cohérents avec les frames fabriqués (spec/spread du snapshot mock conservés)."""
    snap = copy.deepcopy(base_snap)
    snap.frames["D1"] = d1
    snap.frames["M15"] = _m15_frame(end_m15)
    snap.atr_h1 = float(snap.frames["M15"].iloc[-2]["atr14"]) * 2.0
    return snap


def test_n01_scenario_fabrique_produit_un_candidat(n_specs, base_snap):
    """Preuve d'atteignabilité (contrat de test_strategies_vitalite) : biais du mercredi injecté dans le D1
    + M15 en tendance douce → candidat BUY déterministe un mercredi."""
    fn = screeners.SCREENERS["N01"]
    end = "2026-09-16 14:00"                            # mercredi
    d1 = _d1_frame(260, weekday_bias=(2, 0.004))
    c = fn(n_specs["N01"], _scenario(base_snap, d1, end))
    assert c is not None, "N01 ne déclenche pas sur un biais construit"
    assert c.side is Side.BUY and c.agent_id == "N01" and c.rr >= 1.5
    assert any("biais" in a for a in c.arguments_for)
    assert any("saisonnalité" in a for a in c.arguments_against)
    # sans biais : jamais de candidat, même M15 favorable
    assert fn(n_specs["N01"], _scenario(base_snap, _d1_frame(260), end)) is None


def test_n02_scenario_fabrique_produit_un_candidat(n_specs, base_snap):
    """Premier jour de Bourse du mois + biais de retournement injecté dans le D1 → candidat BUY déterministe."""
    fn = screeners.SCREENERS["N02"]
    end = "2026-10-01 14:00"                            # 1er octobre : jour de Bourse 1 du mois
    d1 = _d1_frame(260, month_turn_bias=0.004)
    c = fn(n_specs["N02"], _scenario(base_snap, d1, end))
    assert c is not None, "N02 ne déclenche pas sur un biais construit en fenêtre"
    assert c.side is Side.BUY and c.agent_id == "N02"
    assert any("retournement de mois" in a for a in c.arguments_for)
    # même jour, sans biais dans l'historique : rien
    assert fn(n_specs["N02"], _scenario(base_snap, _d1_frame(260), end)) is None


def test_n01_refuse_historique_court_et_biais_faible(n_specs, base_snap):
    fn = screeners.SCREENERS["N01"]
    wd = pd.to_datetime(str(base_snap.frames["M15"].iloc[-2]["time"])).weekday()
    assert fn(n_specs["N01"], _snap_with_d1(base_snap, _d1_frame(100, weekday_bias=(wd, 0.004)))) is None
    assert fn(n_specs["N01"], _snap_with_d1(base_snap, _d1_frame(260, weekday_bias=(wd, 0.0002)))) is None


def test_n02_exige_fenetre_et_biais(n_specs, base_snap):
    """N02 : rien hors fenêtre de retournement de mois, rien sans biais mesuré dans l'historique."""
    fn = screeners.SCREENERS["N02"]
    today = pd.to_datetime(str(base_snap.frames["M15"].iloc[-2]["time"])).date()
    d1 = _d1_frame(260, month_turn_bias=0.004)
    c = fn(n_specs["N02"], _snap_with_d1(base_snap, d1))
    import calendar as _cal
    dim = _cal.monthrange(today.year, today.month)[1]
    in_window = today.day <= 5 or (dim - today.day) < 2   # borne large : 3 jours de bourse <= 5 jours civils
    if not in_window:
        assert c is None                                   # hors fenêtre : jamais de candidat (16 sept.)
    assert fn(n_specs["N02"], _snap_with_d1(base_snap, _d1_frame(260))) is None


def test_n_no_lookahead(n_specs, base_snap):
    """La barre en formation (dernière ligne de chaque frame) ne change pas la décision."""
    wd = pd.to_datetime(str(base_snap.frames["M15"].iloc[-2]["time"])).weekday()
    for aid, d1 in (("N01", _d1_frame(260, weekday_bias=(wd, 0.004))), ("N02", _d1_frame(260, month_turn_bias=0.004))):
        fn = screeners.SCREENERS[aid]
        snap = _snap_with_d1(base_snap, d1)
        def sig(c):
            return None if c is None else (c.symbol, c.side, c.entry, c.sl, tuple(c.tp_plan), c.setup_score, c.bar_time)
        before = sig(fn(n_specs[aid], snap))
        for tf, df in snap.frames.items():
            if len(df) >= 2:
                last = len(df) - 1
                df.loc[last, "close"] = float(df["close"].iloc[-1]) * 1.05
                df.loc[last, "high"] = float(df["high"].iloc[-1]) * 1.06
        assert sig(fn(n_specs[aid], snap)) == before, aid


# ---------------------------------------------------------------- 4. famille O (cycle long, 2026-09-22)
def test_famille_o_cycle_long_en_shadow():
    """« On peut mettre des cycles longs » (utilisateur, 2026-09-22) : screeners génériques éprouvés rejoués
    en entrée H4 / tendance D1, seedés SHADOW — la promotion passe par le pipeline, jamais direct LIVE."""
    os_ = {a.agent_id: a for a in default_agents() if a.family == "O"}
    assert len(os_) == 6
    for a in os_.values():
        assert a.status == "SHADOW"
        assert a.timeframes == {"entry": "H4", "trend": "D1"}
        assert a.base_strategy in screeners.SCREENERS, a.base_strategy   # repli générique existant
        assert screeners.resolve_screener(a) is not None                          # résolvable même sans stratégie propre


# ---------------------------------------------------------------- 5. seuils approuvés le 2026-09-22
def test_seuils_approuves_2026_09_22():
    """Deux seuils changés sur accord explicite de l'utilisateur (« Fait le », 2026-09-22) :
    - data_max_age_sec 30→60 : le flux DEMO laisse EURUSD 31-43 s sans tick la nuit, le watchdog battait ;
    - break_even_r 1.2→1.0 : trois trades de la nuit du 21-22 ont dépassé +0,9 R puis fini au stop plein."""
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sysc = yaml.safe_load((root / "config" / "system.yaml").read_text(encoding="utf-8"))["system"]
    risk = yaml.safe_load((root / "config" / "risk.yaml").read_text(encoding="utf-8"))["profit_management"]
    assert sysc["data_max_age_sec"] == 60
    assert sysc["heartbeat_max_age_sec"] == 45          # inchangé : la fraîcheur des ticks ne couvre pas le heartbeat
    assert risk["break_even_r"] == 1.0 and risk["break_even_offset_r"] == 0.05
    assert risk["tp1_r"] == 1.5                          # le TP1 n'a pas bougé : seul le break-even avance
