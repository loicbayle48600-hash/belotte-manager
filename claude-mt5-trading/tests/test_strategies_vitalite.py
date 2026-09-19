"""Vitalité des stratégies : un agent qui ne peut JAMAIS produire de candidat est du code mort.

Deux garde-fous complémentaires :

1. **Balayage** — sur un marché simulé couvrant toutes les sessions, on compte les agents qui
   déclenchent au moins une fois. Ce test aurait détecté immédiatement les défauts du banc d'essai
   (symboles tous identiques, cryptos absentes, spread du WTI à 82 % de l'ATR) qui rendaient muets
   48 agents sur 71 sans qu'aucun test ne le signale.
2. **Scénarios fabriqués** — pour les stratégies dont la thèse décrit un événement que la marche
   aléatoire simulée ne produit pratiquement jamais (cascade de liquidation, compression de volatilité
   dans une tendance journalière), on construit le marché correspondant et on vérifie que l'agent
   déclenche. C'est la seule preuve valable qu'une telle stratégie est atteignable, et elle vaut mieux
   qu'un seuil relâché jusqu'à ce qu'elle passe sur des données aléatoires.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from tradinglab.agents import screeners
from tradinglab.agents.registry import AgentRegistry
from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.mt5.mock_adapter import MockBroker

TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1"]
H4_BARS = 48                    # barres M5 dans une barre H4

# Paramètres du balayage : déterministes, donc reproductibles d'une exécution à l'autre.
SWEEP_SEEDS = (7, 19, 31)
SWEEP_STEPS = 30
SWEEP_BARS = 12000              # ≈ 41 jours : assez de barres D1 pour les stratégies à tendance journalière
SWEEP_STEP_BARS = 12            # 1 heure de M5 par pas

#: Agents qui ne déclenchent pas sur ce balayage précis. Leur thèse décrit un évènement rare (cascade,
#: compression dans une tendance D1) ou vise un ou deux symboles seulement : la marche aléatoire simulée
#: ne le produit pas en 1 620 snapshots. Ce n'est PAS un permis d'être muet : L08 et L12 sont démontrés
#: atteignables par les scénarios ci-dessous. La liste ne doit que RÉTRÉCIR.
SILENCIEUX_TOLERES = {"C08", "E03", "L07", "L08", "L09", "L10", "L12", "M04", "M09", "M10", "M14"}

#: Plancher d'agents vivants sur le balayage (mesuré : 60).
MIN_VIVANTS = 58


def injecter(broker: MockBroker, symbol: str, closes, wick: float = 0.4,
             start: datetime = datetime(2025, 1, 6, tzinfo=timezone.utc)) -> None:
    """Remplace la série M5 d'un symbole par un chemin de prix fabriqué.

    Les indicateurs restent calculés par le flux de données normal : on ne fabrique que les prix, jamais
    les indicateurs, sinon le test ne prouverait rien sur la chaîne réelle.
    """
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    amp = np.maximum(np.abs(closes - opens), np.abs(closes) * 2e-6)
    times = pd.to_datetime([start + timedelta(minutes=5 * i) for i in range(len(closes))], utc=True)
    broker.series[symbol] = pd.DataFrame({
        "time": times, "open": opens,
        "high": np.maximum(opens, closes) + wick * amp,
        "low": np.minimum(opens, closes) - wick * amp,
        "close": closes,
        "tick_volume": np.full(len(closes), 300.0),
        "spread": np.full(len(closes), 14),
    })
    broker._cursor[symbol] = len(closes) - 1


def snapshot_de(broker: MockBroker, symbol: str):
    return MarketDataFeed(broker, TIMEFRAMES).snapshot(symbol, now=broker.now())


@pytest.fixture(scope="module")
def agents():
    reg = AgentRegistry()
    screeners._load_own_strategies()
    return {a.agent_id: a for a in reg.agents.values()
            if a.generates_trades and a.agent_id in screeners.SCREENERS}


# ------------------------------------------------------------------ balayage
@pytest.fixture(scope="module")
def declenchements(agents) -> dict[str, int]:
    counts = {aid: 0 for aid in agents}
    for seed in SWEEP_SEEDS:
        broker = MockBroker(seed=seed, bars=SWEEP_BARS)
        broker.connect()
        for _ in range(SWEEP_STEPS):
            broker.advance_bars(SWEEP_STEP_BARS)
            feed = MarketDataFeed(broker, TIMEFRAMES)
            for snap in feed.snapshots(broker.symbols(), now=broker.now()).values():
                for aid, spec in agents.items():
                    if screeners.run_screener(spec, snap) is not None:
                        counts[aid] += 1
    return counts


def test_la_grande_majorite_des_agents_declenche(declenchements):
    vivants = [a for a, n in declenchements.items() if n]
    assert len(vivants) >= MIN_VIVANTS, (
        f"seulement {len(vivants)}/{len(declenchements)} agents déclenchent ; "
        f"muets : {sorted(a for a, n in declenchements.items() if not n)}")


def test_aucun_nouvel_agent_muet(declenchements):
    """La liste des muets tolérés ne doit que rétrécir : un agent qui devient muet est une régression."""
    muets = {a for a, n in declenchements.items() if not n}
    nouveaux = muets - SILENCIEUX_TOLERES
    assert not nouveaux, f"agents devenus muets : {sorted(nouveaux)}"


def test_la_liste_des_tolerances_reste_justifiee(declenchements):
    """Si un agent toléré déclenche désormais, il doit sortir de la liste (sinon elle se périme)."""
    obsoletes = {a for a in SILENCIEUX_TOLERES if declenchements.get(a, 0)}
    assert not obsoletes, f"à retirer de SILENCIEUX_TOLERES (ils déclenchent) : {sorted(obsoletes)}"


# ------------------------------------------------------------------ scénarios fabriqués
def chemin_cascade(n: int = 2600, base: float = 64000.0, pas: float = 95.0,
                   z_choc: float = -6.0, z_rebond: float = 0.9, seed: int = 5):
    """Marché crypto calme, puis cascade de liquidation sur une barre M15, puis barre de stabilisation.

    L'amplitude du choc est exprimée en écarts-types du pas M5 : c'est ce que mesure L08 (z-score des
    rendements), et c'est ce qu'une marche gaussienne ne produit jamais spontanément.
    """
    rng = np.random.default_rng(seed)
    c = base + np.cumsum(rng.normal(0, pas, n))
    sigma_m15 = pas * np.sqrt(3.0)
    i_signal = n - 6           # les 3 dernières barres M5 forment la barre en cours
    i_choc = i_signal - 3
    depart = c[i_choc - 1]
    for k in range(3):
        c[i_choc + k] = depart + z_choc * sigma_m15 * (k + 1) / 3.0
    bas = c[i_choc + 2]
    for k in range(3):
        c[i_signal + k] = bas + z_rebond * sigma_m15 * (k + 1) / 3.0
    c[i_signal + 3:] = c[i_signal + 2]
    return c


def chemin_compression(k_h4: int = 1600, formation: int = 24, base: float = 0.8560, derive: float = 1.1e-6,
                       sigma: float = 3.0e-4, calme_h4: int = 45, facteur: float = 0.5,
                       poussee: float = 0.05, derive_calme: float = 0.2, seed: int = 7):
    """Tendance D1 haussière de longue durée, puis compression de volatilité sur 45 barres H4, puis
    reprise sur la plus haute clôture H4 des 5 dernières barres — le cas décrit par L12."""
    n = H4_BARS * k_h4 + formation
    rng = np.random.default_rng(seed)
    sig = np.full(n, sigma)
    d = np.full(n, derive)
    calme = calme_h4 * H4_BARS + formation
    sig[-calme:] = sigma * facteur
    d[-calme:] = derive * derive_calme
    c = base + np.cumsum(d + rng.normal(0, 1, n) * sig)
    fin = n - formation
    debut = fin - H4_BARS
    cinq_dernieres = [c[fin - H4_BARS * (i + 1) - 1] for i in range(1, 6)]
    cible = max(cinq_dernieres) + poussee * sigma * np.sqrt(H4_BARS)
    depart = c[debut - 1]
    c[debut:fin] = depart + (cible - depart) * np.linspace(1 / H4_BARS, 1.0, H4_BARS)
    c[fin:] = c[fin - 1]
    return c


def test_l08_declenche_sur_une_cascade_de_liquidation(agents):
    broker = MockBroker(seed=7, bars=2600)
    broker.connect()
    injecter(broker, "BTCUSD", chemin_cascade(), wick=0.35)
    snap = snapshot_de(broker, "BTCUSD")
    m15 = snap.frames["M15"]
    choc = m15.iloc[-3]
    rendements = m15["ret1"].iloc[-53:-2].to_numpy(dtype=float)
    ecart = float(np.nanstd(rendements[np.isfinite(rendements)], ddof=1))
    assert abs(float(choc["ret1"]) / ecart) >= 2.5, "le scénario ne produit pas le choc attendu"
    cand = screeners.run_screener(agents["L08"], snap)
    assert cand is not None, "L08 ne déclenche pas sur la cascade qu'il décrit"
    assert cand.side.value == "BUY" and cand.sl < cand.entry
    assert cand.agent_id == "L08"


def test_l08_ignore_une_baisse_ordinaire(agents):
    """Contre-épreuve : sans choc, la même construction ne doit rien produire."""
    broker = MockBroker(seed=7, bars=2600)
    broker.connect()
    injecter(broker, "BTCUSD", chemin_cascade(z_choc=-1.0), wick=0.35)
    assert screeners.run_screener(agents["L08"], snapshot_de(broker, "BTCUSD")) is None


def test_l12_declenche_sur_une_compression_dans_une_tendance_d1(agents):
    broker = MockBroker(seed=7, bars=600)
    broker.connect()
    injecter(broker, "EURGBP", chemin_compression())
    snap = snapshot_de(broker, "EURGBP")
    h4 = snap.frames["H4"]
    atr = float(h4["atr14"].iloc[-2])
    assert atr / float(h4["atr14"].iloc[-51:-1].mean()) <= 0.9, "le scénario n'est pas comprimé"
    cand = screeners.run_screener(agents["L12"], snap)
    assert cand is not None, "L12 ne déclenche pas sur la compression qu'il décrit"
    assert cand.side.value == "BUY" and cand.sl < cand.entry and cand.agent_id == "L12"


def test_l12_ignore_un_marche_sans_compression(agents):
    """Contre-épreuve : même tendance, volatilité non comprimée → aucun signal."""
    broker = MockBroker(seed=7, bars=600)
    broker.connect()
    injecter(broker, "EURGBP", chemin_compression(facteur=1.0, calme_h4=45))
    assert screeners.run_screener(agents["L12"], snapshot_de(broker, "EURGBP")) is None
