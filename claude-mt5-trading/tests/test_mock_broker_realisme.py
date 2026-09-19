"""Le broker simulé doit rester un banc d'essai crédible.

Ces propriétés ne sont pas cosmétiques : trois d'entre elles ont rendu la moitié des stratégies muettes
sans qu'aucun test ne s'en aperçoive.

1. **Décorrélation** : `advance_bars` tirait ses incréments avec une graine indépendante du symbole ;
   tous les instruments recevaient donc la même marche aléatoire (un seul marché décliné en 18 échelles
   de prix). Stratégies et garde de corrélation étaient testés sur un marché fictif dégénéré.
2. **Couverture de l'univers** : les cryptomonnaies de `config/markets.yaml` manquaient, donc les agents
   crypto et la règle prop « week-end crypto uniquement » n'étaient jamais exercés.
3. **Spreads plausibles** : un spread proche de l'ATR rend un instrument intradable pour toute stratégie,
   quelle que soit sa qualité.
4. **Reproductibilité** : même graine ⇒ mêmes barres, sinon aucun test n'est stable.
"""
from __future__ import annotations

import numpy as np
import pytest
import yaml

from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.mt5.mock_adapter import DEFAULT_SPECS, MockBroker

TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1"]
#: un spread qui dépasse ce multiple de l'ATR M15 rend l'instrument intradable ; les paires
#: les moins liquides (NZDUSD) tournent autour de 0,20, le WTI mal paramétré atteignait 0,82.
MAX_SPREAD_ATR = 0.30


@pytest.fixture(scope="module")
def advanced():
    b = MockBroker(seed=7)
    b.connect()
    b.advance_bars(96)
    return b


def _returns(broker, symbol, n=200):
    r = broker.rates(symbol, "M5", n)["close"].pct_change().dropna().to_numpy(dtype=float)
    return r[np.isfinite(r)]


def test_univers_simule_couvre_les_classes_d_actifs_configurees():
    markets = yaml.safe_load(open("config/markets.yaml", encoding="utf-8"))["markets"]
    b = MockBroker(seed=7)
    b.connect()
    classes = {b.symbol_info(s).asset_class for s in b.symbols()}
    attendues = {"forex", "metals", "indices", "energies", "crypto"}
    assert attendues <= classes, f"classes absentes du broker simulé : {attendues - classes}"
    # au moins un symbole de chaque groupe de markets.yaml doit exister, sinon les agents dédiés sont morts
    for groupe, symboles in markets.items():
        assert any(s in DEFAULT_SPECS for s in symboles), f"aucun symbole simulé pour le groupe {groupe}"


def test_les_symboles_ne_partagent_pas_la_meme_marche_aleatoire(advanced):
    """Un seul marché décliné en 18 échelles de prix ne teste rien : les rendements doivent différer."""
    syms = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US500", "BTCUSD"]
    series = {s: _returns(advanced, s) for s in syms}
    for i, a in enumerate(syms):
        for b_ in syms[i + 1:]:
            x, y = series[a], series[b_]
            n = min(len(x), len(y))
            assert n > 50
            corr = float(np.corrcoef(x[-n:], y[-n:])[0, 1])
            assert abs(corr) < 0.9, f"{a}/{b_} quasi identiques (corrélation {corr:.2f})"


def test_les_barres_ajoutees_different_d_un_symbole_a_l_autre():
    b = MockBroker(seed=7)
    b.connect()
    avant = {s: float(b.rates(s, "M5", 1)["close"].iloc[-1]) for s in ("EURUSD", "GBPUSD")}
    b.advance_bars(24)
    apres = {s: b.rates(s, "M5", 24)["close"].to_numpy(dtype=float) for s in ("EURUSD", "GBPUSD")}
    # variations normalisées par le prix de départ : identiques ⇒ même tirage aléatoire
    va = (apres["EURUSD"] - avant["EURUSD"]) / avant["EURUSD"]
    vb = (apres["GBPUSD"] - avant["GBPUSD"]) / avant["GBPUSD"]
    assert not np.allclose(va, vb, atol=1e-9), "les deux symboles reçoivent les mêmes incréments"


def test_spread_credible_par_rapport_a_la_volatilite(advanced):
    """Aucun instrument ne doit coûter une fraction absurde de son ATR à l'entrée."""
    feed = MarketDataFeed(advanced, TIMEFRAMES)
    trop_chers = {}
    for sym in advanced.symbols():
        snap = feed.snapshot(sym, now=advanced.now())
        m15 = snap.frames.get("M15")
        if m15 is None or len(m15) < 3:
            continue
        atr = float(m15["atr14"].iloc[-2])
        tick = advanced.tick(sym)
        if atr <= 0 or tick is None:
            continue
        ratio = (tick.ask - tick.bid) / atr
        if ratio > MAX_SPREAD_ATR:
            trop_chers[sym] = round(ratio, 3)
    assert not trop_chers, f"spread/ATR M15 excessif (> {MAX_SPREAD_ATR}) : {trop_chers}"


def test_broker_simule_reproductible():
    a = MockBroker(seed=7)
    a.connect()
    a.advance_bars(36)
    b = MockBroker(seed=7)
    b.connect()
    b.advance_bars(36)
    for sym in ("EURUSD", "XAUUSD", "BTCUSD"):
        assert np.allclose(a.rates(sym, "M5", 120)["close"].to_numpy(dtype=float),
                           b.rates(sym, "M5", 120)["close"].to_numpy(dtype=float))


def test_historique_plus_long_donne_plus_de_barres_hautes_unites():
    """Les stratégies H4/D1 ont besoin de profondeur : `bars` doit réellement la fournir."""
    court = MockBroker(seed=7, bars=4000)
    court.connect()
    long_ = MockBroker(seed=7, bars=20000)
    long_.connect()
    assert len(court.rates("EURUSD", "D1", 300)) < len(long_.rates("EURUSD", "D1", 300))
    assert len(long_.rates("EURUSD", "H4", 300)) >= 250
    # la découpe en amont du ré-échantillonnage ne doit pas tronquer le nombre demandé
    assert len(long_.rates("EURUSD", "H1", 300)) == 300
