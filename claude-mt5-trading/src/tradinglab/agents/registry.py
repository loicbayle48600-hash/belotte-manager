"""Registre extensible de 100+ agents (rôles + stratégies).

100 agents ≠ 100 appels LLM : c'est un POOL. Le Market Router n'active que les
agents compatibles avec le régime, la session et la classe d'actif courants.
Les statuts (RESEARCH → … → LIVE → DEGRADED → …) sont persistés dans
data/agent_status.json ; toute promotion passe par research.pipeline.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Optional

from ..core.types import AgentStatus, Regime

log = logging.getLogger(__name__)

ALL_REGIMES = [r.value for r in Regime if r not in (Regime.NEWS_SHOCK,)]
TREND_REGIMES = [Regime.TRENDING.value, Regime.RISK_ON.value, Regime.RISK_OFF.value, Regime.BREAKOUT.value]
RANGE_REGIMES = [Regime.RANGING.value, Regime.LOW_VOLATILITY.value]
BREAKOUT_REGIMES = [Regime.BREAKOUT.value, Regime.LOW_VOLATILITY.value, Regime.HIGH_VOLATILITY.value, Regime.TRENDING.value]
FOREX = ["forex"]
ALL_CLASSES = ["forex", "metals", "indices", "energies", "crypto"]
ALL_SESSIONS = ["ASIA", "LONDON", "NEWYORK", "OVERLAP_LDN_NY"]


@dataclass
class AgentSpec:
    agent_id: str
    family: str                      # A..J
    name: str
    strategy: Optional[str]          # clé du screener (None pour les rôles non générateurs)
    markets: list[str]               # classes d'actifs ou symboles racines
    sessions: list[str]
    timeframes: dict                 # {"entry": "M15", "trend": "H1"}
    regimes: list[str]
    params: dict = field(default_factory=dict)
    status: str = AgentStatus.LIVE.value
    version: str = "1.0"
    model_tier_role: str = "worker"  # rôle LLM pour l'analyse approfondie
    news_sensitive: bool = False
    cost_budget_usd: float = 0.5
    description: str = ""
    entry_rules: str = ""
    invalidation_rules: str = ""
    sl_logic: str = "structure/ATR"
    tp_logic: str = "ADAPTIVE_R_MANAGEMENT"
    filters: list[str] = field(default_factory=list)
    parent_id: Optional[str] = None  # pour les challengers générés
    created_by: str = "registry"
    base_strategy: Optional[str] = None  # screener générique de repli si la stratégie propre n'existe pas

    @property
    def generates_trades(self) -> bool:
        return self.strategy is not None

    def to_dict(self) -> dict:
        return asdict(self)


def _a(aid, fam, name, strat, markets, sessions, tfs, regimes, params=None, **kw) -> AgentSpec:
    return AgentSpec(agent_id=aid, family=fam, name=name, strategy=strat, markets=markets, sessions=sessions,
                     timeframes=tfs, regimes=regimes, params=params or {}, **kw)


def default_agents() -> list[AgentSpec]:  # noqa: C901 - registre déclaratif
    A: list[AgentSpec] = []
    tf = lambda e, t: {"entry": e, "trend": t}  # noqa: E731
    role = dict(status=AgentStatus.LIVE.value)
    # ---------------- A. MARKET SCANNERS (rôles Python déterministes) ----------------
    for i, (nm, mk) in enumerate([("universal_scanner", ALL_CLASSES), ("forex_majors_scanner", ["forex_majors"]),
                                  ("forex_minors_scanner", ["forex_minors"]), ("metals_scanner", ["metals"]),
                                  ("indices_scanner", ["indices"]), ("energies_scanner", ["energies"]),
                                  ("commodities_scanner", ["energies", "metals"]), ("crypto_scanner", ["crypto"]),
                                  ("volatility_scanner", ALL_CLASSES), ("spread_scanner", ALL_CLASSES), ("session_scanner", ALL_CLASSES)], 1):
        A.append(_a(f"A{i:02d}", "A", nm, None, mk, ALL_SESSIONS, tf("M5", "H1"), ALL_REGIMES, model_tier_role="scan",
                    description="Scanner déterministe (Python) : symboles, spread, session, volatilité"))
    # ---------------- B. TREND ----------------
    B = [("ema_trend_m15", "ema_trend", "M15", "H1", {"adx_min": 25, "sl_atr": 1.5, "rr": 2.0}),
         ("ema_trend_h1", "ema_trend", "H1", "H4", {"adx_min": 22, "sl_atr": 1.5, "rr": 2.0}),
         ("mtf_trend_m5_h1", "mtf_trend_pullback", "M5", "H1", {"rsi_lo": 40, "rsi_hi": 60, "sl_atr": 1.2, "rr": 2.0}),
         ("h4h1_trend_continuation", "mtf_trend_pullback", "H1", "H4", {"rsi_lo": 40, "rsi_hi": 65, "sl_atr": 1.5, "rr": 2.5}),
         ("trend_pullback_m15", "mtf_trend_pullback", "M15", "H1", {"rsi_lo": 38, "rsi_hi": 62, "sl_atr": 1.3, "rr": 2.0}),
         ("ma_structure_trend", "structure_bos", "H1", "H4", {"sl_atr": 1.5, "rr": 2.0, "require_ema": True}),
         ("adx_trend_h1", "ema_trend", "H1", "H4", {"adx_min": 30, "sl_atr": 2.0, "rr": 2.5}),
         ("vol_adjusted_trend", "ema_trend", "H1", "H4", {"adx_min": 25, "sl_atr": 2.0, "rr": 2.0, "vol_pct_max": 80}),
         ("macd_momentum_trend", "macd_momentum", "M15", "H1", {"sl_atr": 1.5, "rr": 2.0}),
         ("ema_trend_gold", "ema_trend", "M15", "H1", {"adx_min": 25, "sl_atr": 1.8, "rr": 2.0})]
    for i, (nm, st, e, t, p) in enumerate(B, 1):
        mk = ["metals"] if "gold" in nm else ALL_CLASSES
        A.append(_a(f"B{i:02d}", "B", nm, st, mk, ALL_SESSIONS, tf(e, t), TREND_REGIMES, p, model_tier_role="technical_analysis",
                    entry_rules="EMA20>50>200 (ou inverse), ADX>=min, clôture du bon côté", invalidation_rules="clôture au-delà de l'EMA50 du tf d'entrée"))
    # ---------------- C. BREAKOUT ----------------
    C = [("asian_range_breakout", "session_breakout", {"session": "ASIA", "start": "00:00", "end": "07:00", "sl_atr": 1.0, "rr": 2.0}, ["LONDON", "OVERLAP_LDN_NY"]),
         ("london_breakout", "session_breakout", {"session": "LONDON_OPEN", "start": "07:00", "end": "09:00", "sl_atr": 1.0, "rr": 2.0}, ["LONDON", "OVERLAP_LDN_NY"]),
         ("newyork_breakout", "session_breakout", {"session": "NY_OPEN", "start": "12:00", "end": "14:00", "sl_atr": 1.0, "rr": 2.0}, ["NEWYORK", "OVERLAP_LDN_NY"]),
         ("daily_high_low_breakout", "daily_hl_breakout", {"sl_atr": 1.2, "rr": 2.0}, ALL_SESSIONS),
         ("compression_expansion", "compression_expansion", {"sl_atr": 1.0, "rr": 2.5}, ALL_SESSIONS),
         ("volatility_breakout", "atr_expansion", {"atr_ratio": 1.3, "sl_atr": 1.2, "rr": 2.0}, ALL_SESSIONS),
         ("breakout_retest", "breakout_retest", {"sl_atr": 1.0, "rr": 2.5}, ALL_SESSIONS),
         ("asian_range_breakout_gold", "session_breakout", {"session": "ASIA", "start": "00:00", "end": "07:00", "sl_atr": 1.2, "rr": 2.0}, ["LONDON"]),
         ("index_open_breakout", "session_breakout", {"session": "NY_OPEN", "start": "13:30", "end": "14:30", "sl_atr": 1.0, "rr": 2.0}, ["NEWYORK", "OVERLAP_LDN_NY"])]
    for i, (nm, st, p, ss) in enumerate(C, 1):
        mk = ["metals"] if "gold" in nm else ["indices"] if "index" in nm else ALL_CLASSES
        A.append(_a(f"C{i:02d}", "C", nm, st, mk, ss, tf("M15", "H1"), BREAKOUT_REGIMES, p, model_tier_role="technical_analysis",
                    entry_rules="clôture hors du range de référence, volume/ATR confirmés", invalidation_rules="retour et clôture dans le range"))
    # ---------------- D. PULLBACK ----------------
    D = [("h1_trend_m15_pullback", "mtf_trend_pullback", "M15", "H1", {"rsi_lo": 35, "rsi_hi": 60, "sl_atr": 1.2, "rr": 2.0}),
         ("h4_trend_h1_pullback", "mtf_trend_pullback", "H1", "H4", {"rsi_lo": 35, "rsi_hi": 60, "sl_atr": 1.5, "rr": 2.5}),
         ("ema20_pullback", "ema_pullback", "M15", "H1", {"ema": "ema20", "sl_atr": 1.0, "rr": 2.0}),
         ("ema50_pullback", "ema_pullback", "H1", "H4", {"ema": "ema50", "sl_atr": 1.2, "rr": 2.0}),
         ("fibonacci_confluence", "fib_pullback", "H1", "H4", {"levels": [0.5, 0.618], "tol_atr": 0.3, "sl_atr": 1.0, "rr": 2.5}),
         ("sr_pullback", "sr_rejection", "M15", "H1", {"tol_atr": 0.3, "with_trend": True, "sl_atr": 1.0, "rr": 2.0}),
         ("liquidity_retest", "liquidity_sweep", "M15", "H1", {"with_trend": True, "sl_atr": 0.8, "rr": 2.5})]
    for i, (nm, st, e, t, p) in enumerate(D, 1):
        A.append(_a(f"D{i:02d}", "D", nm, st, ALL_CLASSES, ALL_SESSIONS, tf(e, t), TREND_REGIMES, p, model_tier_role="technical_analysis",
                    entry_rules="tendance tf supérieur + repli sur zone de valeur + rejet", invalidation_rules="clôture sous/au-dessus du swing du repli"))
    # ---------------- E. REVERSAL / MEAN REVERSION ----------------
    E = [("range_mean_reversion", "bollinger_mr", {"rsi_lo": 30, "rsi_hi": 70, "sl_atr": 0.8, "rr": 1.5}),
         ("rsi_divergence", "rsi_divergence", {"sl_atr": 1.0, "rr": 2.0}),
         ("exhaustion_reversal", "exhaustion", {"rsi_ext": 20, "sl_atr": 0.8, "rr": 1.8}),
         ("bollinger_mean_reversion", "bollinger_mr", {"rsi_lo": 25, "rsi_hi": 75, "sl_atr": 0.7, "rr": 1.5}),
         ("failed_breakout", "failed_breakout", {"sl_atr": 0.8, "rr": 2.0}),
         ("liquidity_sweep_reversal", "liquidity_sweep", {"with_trend": False, "sl_atr": 0.8, "rr": 2.0}),
         ("sr_rejection_reversal", "sr_rejection", {"tol_atr": 0.3, "with_trend": False, "sl_atr": 0.8, "rr": 1.8})]
    for i, (nm, st, p) in enumerate(E, 1):
        A.append(_a(f"E{i:02d}", "E", nm, st, ALL_CLASSES, ALL_SESSIONS, tf("M15", "H1"), RANGE_REGIMES + [Regime.HIGH_VOLATILITY.value], p,
                    model_tier_role="technical_analysis", entry_rules="excès + rejet dans un marché en range", invalidation_rules="clôture au-delà de l'extrême de rejet"))
    # ---------------- F. STRUCTURE / PRICE ACTION ----------------
    F = [("hh_hl_continuation", "structure_bos", {"direction": "UP", "sl_atr": 1.2, "rr": 2.0}),
         ("lh_ll_continuation", "structure_bos", {"direction": "DOWN", "sl_atr": 1.2, "rr": 2.0}),
         ("break_of_structure", "structure_bos", {"sl_atr": 1.0, "rr": 2.5}),
         ("change_of_character", "choch", {"sl_atr": 1.0, "rr": 2.0}),
         ("swing_rejection", "sr_rejection", {"tol_atr": 0.25, "with_trend": True, "sl_atr": 0.9, "rr": 2.0}),
         ("mtf_structure_alignment", "structure_bos", {"require_mtf": True, "sl_atr": 1.2, "rr": 2.5})]
    for i, (nm, st, p) in enumerate(F, 1):
        A.append(_a(f"F{i:02d}", "F", nm, st, ALL_CLASSES, ALL_SESSIONS, tf("M15", "H1"), TREND_REGIMES + [Regime.UNCERTAIN.value], p,
                    model_tier_role="technical_analysis", entry_rules="structure HH/HL ou LH/LL + cassure de swing", invalidation_rules="cassure du swing opposé"))
    # ---------------- G. VOLATILITY ----------------
    G = [("atr_expansion", "atr_expansion", {"atr_ratio": 1.4, "sl_atr": 1.2, "rr": 2.0}, BREAKOUT_REGIMES),
         ("volatility_contraction", "compression_expansion", {"sl_atr": 1.0, "rr": 2.5}, [Regime.LOW_VOLATILITY.value, Regime.RANGING.value]),
         ("opening_range", "session_breakout", {"session": "OPEN", "start": "07:00", "end": "08:00", "sl_atr": 1.0, "rr": 2.0}, BREAKOUT_REGIMES),
         ("session_volatility", "atr_expansion", {"atr_ratio": 1.2, "sl_atr": 1.5, "rr": 2.0}, BREAKOUT_REGIMES),
         ("abnormal_volatility_filter", None, {"vol_pct_block": 95}, ALL_REGIMES)]
    for i, (nm, st, p, rg) in enumerate(G, 1):
        A.append(_a(f"G{i:02d}", "G", nm, st, ALL_CLASSES, ALL_SESSIONS, tf("M15", "H1"), rg, p, model_tier_role="technical_analysis",
                    description="Filtre déterministe" if st is None else ""))
    # ---------------- H. MACRO / CROSS-ASSET (rôles) ----------------
    for i, nm in enumerate(["usd_regime", "rates_regime", "risk_on_off", "gold_macro", "index_macro", "currency_relative_strength", "cross_market_confirmation"], 1):
        A.append(_a(f"H{i:02d}", "H", nm, None, ALL_CLASSES, ALL_SESSIONS, tf("H4", "D1"), ALL_REGIMES, model_tier_role="macro_synthesis",
                    news_sensitive=True, description="Analyse macro / cross-asset (LLM senior si disponible, sinon UNKNOWN)"))
    # ---------------- I. NEWS-AWARE (rôles) ----------------
    for i, nm in enumerate(["pre_news_avoider", "post_news_stabilization", "macro_surprise_interpreter", "central_bank_interpreter", "earnings_event_filter"], 1):
        A.append(_a(f"I{i:02d}", "I", nm, None, ALL_CLASSES, ALL_SESSIONS, tf("M15", "H1"), ALL_REGIMES, model_tier_role="complex_news",
                    news_sensitive=True, description="Agent news : filtre/interprétation (jamais une donnée inventée)"))
    # ---------------- J. ADVERSARIAL / REVIEW (rôles) ----------------
    for i, (nm, rl) in enumerate([("bull_thesis", "bull_thesis"), ("bear_thesis", "bear_thesis"), ("devils_advocate", "devil_advocate"),
                                  ("data_quality_auditor", "adversarial_review"), ("false_breakout_critic", "adversarial_review"),
                                  ("overfitting_critic", "champion_adjudication"), ("correlation_critic", "adversarial_review"),
                                  ("execution_critic", "adversarial_review"), ("trade_arbiter", "trade_arbiter"),
                                  ("post_trade_root_cause", "post_trade_root_cause")], 1):
        A.append(_a(f"J{i:02d}", "J", nm, None, ALL_CLASSES, ALL_SESSIONS, tf("M15", "H1"), ALL_REGIMES, model_tier_role=rl,
                    description="Revue adversariale des meilleurs candidats"))
    # ---------------- K. NEWS-AWARE STRATÉGIES (générateurs, news-sensibles) ----------------
    K = [("post_news_stabilization_entry", "mtf_trend_pullback", {"rsi_lo": 40, "rsi_hi": 60, "sl_atr": 1.5, "rr": 2.0}),
         ("macro_surprise_momentum", "atr_expansion", {"atr_ratio": 1.5, "sl_atr": 1.5, "rr": 2.0})]
    for i, (nm, st, p) in enumerate(K, 1):
        A.append(_a(f"K{i:02d}", "I", nm, st, ALL_CLASSES, ALL_SESSIONS, tf("M15", "H1"), ALL_REGIMES, p, news_sensitive=True,
                    model_tier_role="complex_news", status=AgentStatus.SHADOW.value))
    # ---------------- L. Variantes par classe d'actif (métaux / indices / crypto) ----------------
    L = [("gold_london_pullback", "mtf_trend_pullback", ["metals"], ["LONDON", "OVERLAP_LDN_NY"], {"rsi_lo": 38, "rsi_hi": 62, "sl_atr": 1.5, "rr": 2.0}),
         ("gold_mean_reversion_asia", "bollinger_mr", ["metals"], ["ASIA"], {"rsi_lo": 30, "rsi_hi": 70, "sl_atr": 0.8, "rr": 1.5}),
         ("index_trend_ny", "ema_trend", ["indices"], ["NEWYORK", "OVERLAP_LDN_NY"], {"adx_min": 25, "sl_atr": 1.5, "rr": 2.0}),
         ("index_pullback_ny", "ema_pullback", ["indices"], ["NEWYORK", "OVERLAP_LDN_NY"], {"ema": "ema20", "sl_atr": 1.0, "rr": 2.0}),
         ("index_failed_breakout", "failed_breakout", ["indices"], ["NEWYORK"], {"sl_atr": 0.8, "rr": 2.0}),
         ("energy_trend_h1", "ema_trend", ["energies"], ["LONDON", "NEWYORK", "OVERLAP_LDN_NY"], {"adx_min": 25, "sl_atr": 1.8, "rr": 2.0}),
         ("crypto_trend_h4", "ema_trend", ["crypto"], ALL_SESSIONS, {"adx_min": 25, "sl_atr": 2.0, "rr": 2.5}),
         ("crypto_bollinger_mr", "bollinger_mr", ["crypto"], ALL_SESSIONS, {"rsi_lo": 25, "rsi_hi": 75, "sl_atr": 0.8, "rr": 1.5}),
         ("jpy_cross_trend", "ema_trend", ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY"], ["ASIA", "LONDON"], {"adx_min": 25, "sl_atr": 1.5, "rr": 2.0}),
         ("eur_cross_range", "bollinger_mr", ["EURGBP", "EURCHF", "EURAUD", "EURCAD"], ALL_SESSIONS, {"rsi_lo": 30, "rsi_hi": 70, "sl_atr": 0.8, "rr": 1.5}),
         ("majors_london_bos", "structure_bos", ["forex_majors"], ["LONDON", "OVERLAP_LDN_NY"], {"sl_atr": 1.0, "rr": 2.5}),
         ("minors_h4_pullback", "mtf_trend_pullback", ["forex_minors"], ALL_SESSIONS, {"rsi_lo": 35, "rsi_hi": 60, "sl_atr": 1.5, "rr": 2.5})]
    for i, (nm, st, mk, ss, p) in enumerate(L, 1):
        rg = RANGE_REGIMES if st == "bollinger_mr" or "failed" in st else TREND_REGIMES
        A.append(_a(f"L{i:02d}", "L", nm, st, mk, ss, tf("H4" if "h4" in nm else "M15", "D1" if "h4" in nm else "H1"), rg, p,
                    model_tier_role="technical_analysis"))
    # ---------------- M. Spécialistes par symbole (variantes de paramètres) ----------------
    M = [("eurusd_london_bos", "structure_bos", ["EURUSD"], ["LONDON", "OVERLAP_LDN_NY"], {"sl_atr": 1.0, "rr": 2.5}, TREND_REGIMES),
         ("eurusd_ny_pullback", "mtf_trend_pullback", ["EURUSD"], ["NEWYORK", "OVERLAP_LDN_NY"], {"rsi_lo": 38, "rsi_hi": 62, "sl_atr": 1.2, "rr": 2.0}, TREND_REGIMES),
         ("gbpusd_london_breakout", "session_breakout", ["GBPUSD"], ["LONDON"], {"session": "ASIA", "start": "00:00", "end": "07:00", "sl_atr": 1.0, "rr": 2.0}, BREAKOUT_REGIMES),
         ("gbpusd_ny_reversal", "liquidity_sweep", ["GBPUSD"], ["NEWYORK", "OVERLAP_LDN_NY"], {"with_trend": False, "sl_atr": 0.8, "rr": 2.0}, RANGE_REGIMES + [Regime.HIGH_VOLATILITY.value]),
         ("usdjpy_asia_range", "bollinger_mr", ["USDJPY"], ["ASIA"], {"rsi_lo": 30, "rsi_hi": 70, "sl_atr": 0.8, "rr": 1.5}, RANGE_REGIMES),
         ("usdjpy_tokyo_trend", "ema_trend", ["USDJPY"], ["ASIA", "LONDON"], {"adx_min": 25, "sl_atr": 1.5, "rr": 2.0}, TREND_REGIMES),
         ("xauusd_ny_trend", "ema_trend", ["XAUUSD"], ["NEWYORK", "OVERLAP_LDN_NY"], {"adx_min": 25, "sl_atr": 1.8, "rr": 2.0}, TREND_REGIMES),
         ("xauusd_sweep_reversal", "liquidity_sweep", ["XAUUSD"], ["LONDON", "NEWYORK", "OVERLAP_LDN_NY"], {"with_trend": False, "sl_atr": 0.8, "rr": 2.0}, RANGE_REGIMES + [Regime.HIGH_VOLATILITY.value]),
         ("nas100_open_range", "session_breakout", ["NAS100"], ["NEWYORK", "OVERLAP_LDN_NY"], {"session": "NY_OPEN", "start": "13:30", "end": "14:00", "sl_atr": 1.0, "rr": 2.0}, BREAKOUT_REGIMES),
         ("us500_trend_pullback", "mtf_trend_pullback", ["US500"], ["NEWYORK", "OVERLAP_LDN_NY"], {"rsi_lo": 40, "rsi_hi": 60, "sl_atr": 1.2, "rr": 2.0}, TREND_REGIMES),
         ("ger40_london_open", "session_breakout", ["GER40"], ["LONDON"], {"session": "OPEN", "start": "07:00", "end": "08:00", "sl_atr": 1.0, "rr": 2.0}, BREAKOUT_REGIMES),
         ("audusd_asia_breakout", "session_breakout", ["AUDUSD"], ["ASIA", "LONDON"], {"session": "ASIA", "start": "22:00", "end": "02:00", "sl_atr": 1.0, "rr": 2.0}, BREAKOUT_REGIMES),
         ("usdcad_ny_trend", "ema_trend", ["USDCAD"], ["NEWYORK", "OVERLAP_LDN_NY"], {"adx_min": 25, "sl_atr": 1.5, "rr": 2.0}, TREND_REGIMES),
         ("usoil_ny_momentum", "atr_expansion", ["USOIL", "XTIUSD"], ["NEWYORK", "OVERLAP_LDN_NY"], {"atr_ratio": 1.3, "sl_atr": 1.5, "rr": 2.0}, BREAKOUT_REGIMES)]
    for i, (nm, st, mk, ss, p, rg) in enumerate(M, 1):
        A.append(_a(f"M{i:02d}", "M", nm, st, mk, ss, tf("M15", "H1"), rg, p, model_tier_role="technical_analysis"))
    # Chaque agent générateur possède SA PROPRE stratégie (clé = agent_id, module agents/strategies/*) ;
    # le screener générique historique reste en repli (base_strategy) tant que la stratégie propre n'existe pas.
    for a in A:
        if a.strategy is not None:
            a.base_strategy = a.strategy
            a.strategy = a.agent_id
    return A


class AgentRegistry:
    def __init__(self, agents: Optional[Iterable[AgentSpec]] = None, status_file: Optional[Path] = None, journal=None):
        # verrou réentrant : le thread de recherche (add/set_status/save) et le thread principal (generators…) partagent le registre
        self._lock = threading.RLock()
        self.journal = journal
        self.agents: dict[str, AgentSpec] = {a.agent_id: a for a in (agents or default_agents())}
        self.status_file = status_file
        self._load_overrides()

    def _warn(self, msg: str, **data) -> None:
        if self.journal is not None:
            self.journal.warn(msg, **data)
        else:
            log.warning("%s %s", msg, data)

    # ---------- persistance des statuts / challengers ----------
    def _load_overrides(self) -> None:
        if not self.status_file or not self.status_file.exists():
            return
        try:
            data = json.loads(self.status_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("contenu attendu : objet JSON")
        except (OSError, json.JSONDecodeError, ValueError) as e:
            # fichier illisible/tronqué : mis de côté et journalisé (les statuts repartent des valeurs par défaut,
            # jamais silencieusement) ; le processus ne doit pas mourir en boucle sous l'autostart
            corrupt = self.status_file.with_name("agent_status.corrupt.json")
            try:
                os.replace(self.status_file, corrupt)
            except OSError:
                corrupt = None
            self._warn("agent_status.json illisible : statuts par défaut", error=f"{type(e).__name__}: {e}",
                       renamed_to=str(corrupt) if corrupt else None)
            return
        valid = {st.value for st in AgentStatus}
        status = data.get("status", {})
        if isinstance(status, dict):
            for aid, st in status.items():
                if aid in self.agents and st in valid:
                    self.agents[aid].status = st
                elif aid in self.agents:
                    self._warn("agent_status.json : statut invalide ignoré", agent_id=aid, status=str(st))
        for spec in data.get("challengers", []) or []:
            try:
                if not isinstance(spec, dict):
                    raise TypeError("challenger non-objet")
                a = AgentSpec(**{k: v for k, v in spec.items() if k in AgentSpec.__dataclass_fields__})
            except TypeError as e:
                self._warn("agent_status.json : challenger ignoré", error=f"{type(e).__name__}: {e}",
                           agent_id=spec.get("agent_id") if isinstance(spec, dict) else None)
                continue
            if a.status not in valid:
                a.status = AgentStatus.RESEARCH.value
            self.agents.setdefault(a.agent_id, a)

    def save(self) -> None:
        if not self.status_file:
            return
        with self._lock:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            data = {"status": {a.agent_id: a.status for a in list(self.agents.values())},
                    "challengers": [a.to_dict() for a in list(self.agents.values()) if a.created_by != "registry"]}
            # écriture atomique (tempfile + os.replace) : jamais de fichier tronqué/entrelacé entre threads ou après un crash
            fd, tmp = tempfile.mkstemp(dir=self.status_file.parent, prefix=".agent_status-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False, indent=1))
                os.replace(tmp, self.status_file)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    # ---------- accès ----------
    def __len__(self) -> int:
        return len(self.agents)

    def get(self, agent_id: str) -> Optional[AgentSpec]:
        return self.agents.get(agent_id)

    def by_status(self, *statuses: AgentStatus | str) -> list[AgentSpec]:
        ss = {s.value if isinstance(s, AgentStatus) else s for s in statuses}
        return [a for a in list(self.agents.values()) if a.status in ss]   # copie : insertion concurrente possible

    def generators(self, statuses: Iterable[str] = (AgentStatus.LIVE.value,)) -> list[AgentSpec]:
        ss = set(statuses)
        return [a for a in list(self.agents.values()) if a.generates_trades and a.status in ss]

    def set_status(self, agent_id: str, status: AgentStatus, reason: str = "") -> None:
        with self._lock:
            a = self.agents[agent_id]
            a.status = status.value
            self.save()

    def add(self, spec: AgentSpec) -> None:
        with self._lock:
            self.agents[spec.agent_id] = spec
            self.save()

    def next_challenger_id(self, parent: AgentSpec) -> str:
        with self._lock:
            n = 101
            while f"CH{n}" in self.agents:
                n += 1
            return f"CH{n}"

    @staticmethod
    def matches_market(a: AgentSpec, asset_class: str, root: str, group: Optional[str] = None) -> bool:
        return any(m in (asset_class, root, group) for m in a.markets) or ("forex_majors" in a.markets and group == "forex_majors") \
            or ("forex_minors" in a.markets and group == "forex_minors")

    def active_for(self, regime: str, session: str, asset_class: str, root: str, group: Optional[str] = None,
                   statuses: Iterable[str] = (AgentStatus.LIVE.value,)) -> list[AgentSpec]:
        out = []
        for a in self.generators(statuses):
            if regime not in a.regimes or session not in a.sessions:
                continue
            if not self.matches_market(a, asset_class, root, group):
                continue
            out.append(a)
        return out

    def summary(self) -> dict:
        by = {}
        agents = list(self.agents.values())
        for a in agents:
            by[a.status] = by.get(a.status, 0) + 1
        return {"total": len(agents), "generators": len([a for a in agents if a.generates_trades]), "by_status": by}
