"""État système persistant (JSON atomique) partagé entre orchestrateur, watchdog, CLI, MCP.

Le fichier state/system_state.json survit aux crashs et redémarrages. Il contient :
- mode (SAFE_MODE / AUTO / PAUSED / PANIC) et raisons ;
- verrous (NEW_TRADES_LOCKED, NEWS_DATA_DEGRADED…) ;
- suivi journalier (equity de départ, pic, P&L, pertes consécutives) ;
- positions du bot (ticket → plan de gestion : risque initial, SL initial, TP partiels faits…) ;
- clés d'idempotence des candidats exécutés (aucune ré-entrée après restart) ;
- compteurs de budget modèles ;
- heartbeat orchestrateur.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .trading_day import TradingDayCalendar, DEFAULT_CALENDAR
from .types import SystemMode, utcnow


def _replace_with_retry(src: str | Path, dst: str | Path, attempts: int = 6) -> None:
    """`os.replace` avec quelques tentatives : sous Windows, MoveFileEx(REPLACE_EXISTING) échoue par
    `PermissionError` (ERROR_SHARING_VIOLATION) si la cible est ouverte par un autre processus
    (watchdog/dashboard/CLI relisent le fichier en permanence)."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


def _parse_ts(ts: str) -> Optional[datetime]:
    """Horodatage ISO du fichier d'état → datetime UTC ; None si illisible (jamais d'exception ici)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class BotPositionPlan:
    ticket: int
    symbol: str
    side: str
    agent_id: str
    candidate_id: str
    entry: float
    initial_sl: float
    initial_volume: float
    initial_risk_money: float
    risk_percent: float
    regime: str = "UNCERTAIN"
    tp_plan: list[float] = field(default_factory=list)
    tp1_done: bool = False
    tp2_done: bool = False
    break_even_done: bool = False
    trailing_active: bool = False
    opened_at: str = ""
    max_r: float = 0.0
    min_r: float = 0.0
    last_sl: float = 0.0
    invalidation: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class DailyStats:
    day: str = ""                          # journée de trading prop (reset 17:00 America/New_York)
    starting_equity: float = 0.0
    starting_balance: float = 0.0
    # base du plancher journalier prop : max(solde, equity) constaté au reset (règle FOXX Funded)
    reference_equity: float = 0.0
    peak_equity: float = 0.0
    peak_daily_pnl: float = 0.0
    realized_pnl: float = 0.0
    trades_closed: int = 0
    wins: int = 0
    losses: int = 0


@dataclass
class TradeIdea:
    """Idée de trade au sens prop firm : plusieurs positions agrégées en une seule exposition.

    FOXX Funded agrège les positions prises dans le même sens sur le même instrument, et considère
    qu'une position fermée puis rouverte dans le même sens **sous 10 minutes** appartient encore à
    l'idée précédente. Le risque cumulé de l'idée est plafonné (2 % du solde initial) et le profit
    d'une idée ne doit pas dépasser 25 % du profit total au moment du paiement.
    """
    idea_id: str = ""
    symbol: str = ""
    side: str = ""
    opened_at: str = ""
    last_activity_at: str = ""
    risk_money: float = 0.0          # cumulé sur toute la vie de l'idée, jamais décrémenté
    realized_pnl: float = 0.0
    open_tickets: list[int] = field(default_factory=list)
    closed_tickets: list[int] = field(default_factory=list)
    entries: int = 0


@dataclass
class ModelBudget:
    day: str = ""
    spent_usd: float = 0.0
    hour: str = ""
    calls_by_tier_hour: dict = field(default_factory=dict)
    calls_by_tier_day: dict = field(default_factory=dict)


@dataclass
class SystemState:
    mode: str = SystemMode.SAFE_MODE.value
    mode_reasons: list[str] = field(default_factory=list)
    new_trades_locked: bool = False
    lock_reasons: list[str] = field(default_factory=list)
    news_data_degraded: bool = True
    calendar_data_degraded: bool = True
    mt5_connected: bool = False
    account_trade_mode: str = "UNKNOWN"
    account_login: int = 0
    account_server: str = ""
    equity: float = 0.0
    balance: float = 0.0
    currency: str = ""
    consecutive_losses: int = 0
    overall_peak_equity: float = 0.0
    initial_balance: float = 0.0        # solde de référence du compte (base des % prop, drawdown statique)
    daily: DailyStats = field(default_factory=DailyStats)
    bot_positions: dict[str, BotPositionPlan] = field(default_factory=dict)  # ticket(str) -> plan
    executed_keys: dict[str, str] = field(default_factory=dict)              # idempotency key -> ts
    trade_ideas: dict[str, TradeIdea] = field(default_factory=dict)          # idea_id -> idée agrégée
    trading_days: list[str] = field(default_factory=list)                    # journées prop avec au moins une entrée
    last_trade_at: str = ""                                                  # dernière entrée (règle d'activité minimale)
    model_budget: ModelBudget = field(default_factory=ModelBudget)
    orchestrator_heartbeat: str = ""
    watchdog_heartbeat: str = ""
    last_cycle: dict = field(default_factory=dict)
    active_agents: list[str] = field(default_factory=list)
    top_setups: list[dict] = field(default_factory=list)
    regimes: dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    restarts: int = 0
    load_warnings: list[str] = field(default_factory=list)   # sections ignorées au chargement (diagnostic)
    version: int = 1

    # ---------- helpers ----------
    @property
    def mode_enum(self) -> SystemMode:
        return SystemMode(self.mode)

    def entries_allowed(self) -> tuple[bool, str]:
        if self.mode != SystemMode.AUTO.value:
            return False, f"mode={self.mode}"
        if self.new_trades_locked:
            return False, "NEW_TRADES_LOCKED: " + ", ".join(self.lock_reasons)
        return True, "ok"

    def set_mode(self, mode: SystemMode, reason: str) -> None:
        self.mode = mode.value
        if reason and reason not in self.mode_reasons:
            self.mode_reasons.append(reason)
        if mode is SystemMode.AUTO:
            self.mode_reasons = []

    def lock_entries(self, reason: str) -> None:
        self.new_trades_locked = True
        if reason not in self.lock_reasons:
            self.lock_reasons.append(reason)

    def unlock_entries(self, reason: str | None = None) -> None:
        if reason is None:
            self.lock_reasons = []
        else:
            self.lock_reasons = [r for r in self.lock_reasons if r != reason]
        self.new_trades_locked = bool(self.lock_reasons)

    def daily_pnl(self) -> float:
        if not self.daily.starting_equity:
            return 0.0
        return self.equity - self.daily.starting_equity

    def daily_pnl_percent(self) -> float:
        if not self.daily.starting_equity:
            return 0.0
        return 100.0 * self.daily_pnl() / self.daily.starting_equity

    def daily_drawdown_percent(self) -> float:
        if not self.daily.starting_equity:
            return 0.0
        return max(0.0, 100.0 * (self.daily.starting_equity - self.equity) / self.daily.starting_equity)

    def overall_drawdown_percent(self) -> float:
        if not self.overall_peak_equity:
            return 0.0
        return max(0.0, 100.0 * (self.overall_peak_equity - self.equity) / self.overall_peak_equity)

    def open_risk_money(self) -> float:
        return sum(p.initial_risk_money for p in self.bot_positions.values())

    def open_risk_percent(self) -> float:
        if not self.equity:
            return 0.0
        return 100.0 * self.open_risk_money() / self.equity

    def has_executed(self, key: str) -> bool:
        return key in self.executed_keys

    def mark_executed(self, key: str) -> None:
        self.executed_keys[key] = utcnow().isoformat()
        if len(self.executed_keys) > 5000:
            for k in list(self.executed_keys)[: len(self.executed_keys) - 4000]:
                self.executed_keys.pop(k, None)

    def roll_day_if_needed(self, equity: float, balance: float,
                           now: Optional[date | datetime] = None,
                           calendar: Optional[TradingDayCalendar] = None) -> bool:
        """Bascule la journée de trading. `now` est un instant (datetime) converti en journée prop.

        La journée prop ne commence pas à minuit UTC mais au reset de la prop firm (17:00
        America/New_York chez FOXX Funded) : c'est `calendar` qui tranche. Un `date` est accepté pour
        compatibilité et pris tel quel. La référence du jour est `max(solde, equity)` au reset, base
        imposée par la prop firm pour le plancher de perte quotidienne.
        """
        cal = calendar or DEFAULT_CALENDAR
        if now is None:
            now = utcnow()
        d = (cal.day(now) if isinstance(now, datetime) else now).isoformat()
        reference = max(equity, balance)
        if self.daily.day == d:
            # journée déjà ouverte avec une equity nulle (compte pas encore synchronisé) : on fixe la
            # référence dès la première equity valide, sinon perte journalière/drawdown resteraient à 0 %.
            if self.daily.starting_equity <= 0 and equity > 0:
                self.daily.starting_equity = equity
                self.daily.starting_balance = balance
                self.daily.reference_equity = reference
                self.daily.peak_equity = max(self.daily.peak_equity, equity)
            elif self.daily.reference_equity <= 0 and reference > 0:
                # état écrit par une version antérieure : combler la base prop sans rouvrir la journée
                self.daily.reference_equity = max(self.daily.starting_equity, self.daily.starting_balance)
            return False
        if equity <= 0:
            # equity inconnue/nulle : ne pas figer starting_equity=0 pour toute la journée, réessayer au cycle suivant
            return False
        self.daily = DailyStats(day=d, starting_equity=equity, starting_balance=balance,
                                reference_equity=reference, peak_equity=equity)
        self.consecutive_losses = 0
        self.unlock_entries("DAILY_LOSS_LIMIT")
        self.unlock_entries("GIVEBACK_FLOOR")
        self.unlock_entries("MAX_CONSECUTIVE_LOSSES")
        return True

    def update_equity(self, equity: float, balance: float) -> None:
        self.equity = equity
        self.balance = balance
        if self.initial_balance <= 0 and balance > 0:
            # solde de référence : figé à la première synchronisation du compte, jamais réévalué ensuite
            # (le drawdown total prop est STATIQUE, calculé sur le solde initial et non sur un pic).
            self.initial_balance = balance
        if equity > self.daily.peak_equity:
            self.daily.peak_equity = equity
        pnl = self.daily_pnl()
        if pnl > self.daily.peak_daily_pnl:
            self.daily.peak_daily_pnl = pnl
        if equity > self.overall_peak_equity:
            self.overall_peak_equity = equity

    # ---------- bases de calcul imposées par la prop firm ----------
    def prop_reference_balance(self, account_size: float = 0.0) -> float:
        """Solde initial servant de dénominateur aux limites prop (4 % / 8 % / 2 % par idée)."""
        return self.initial_balance or float(account_size or 0.0) or self.balance or self.equity

    def prop_daily_floor(self, daily_loss_percent: float, account_size: float = 0.0) -> float:
        """Plancher d'equity du jour : max(solde, equity) au reset − X % du solde initial."""
        base = self.prop_reference_balance(account_size)
        ref = self.daily.reference_equity or max(self.daily.starting_equity, self.daily.starting_balance)
        return ref - base * daily_loss_percent / 100.0

    def prop_daily_loss_percent(self, account_size: float = 0.0) -> float:
        """Perte du jour en % du SOLDE INITIAL, mesurée depuis max(solde, equity) au reset."""
        base = self.prop_reference_balance(account_size)
        ref = self.daily.reference_equity or max(self.daily.starting_equity, self.daily.starting_balance)
        if base <= 0 or ref <= 0:
            return 0.0
        return max(0.0, 100.0 * (ref - self.equity) / base)

    def prop_overall_floor(self, overall_loss_percent: float, account_size: float = 0.0) -> float:
        base = self.prop_reference_balance(account_size)
        return base * (1.0 - overall_loss_percent / 100.0)

    def prop_overall_loss_percent(self, account_size: float = 0.0) -> float:
        """Perte totale en % du solde initial : drawdown STATIQUE, jamais mesuré depuis un pic d'equity."""
        base = self.prop_reference_balance(account_size)
        if base <= 0:
            return 0.0
        return max(0.0, 100.0 * (base - self.equity) / base)

    # ---------- idées de trade (agrégation prop) ----------
    def active_trade_idea(self, symbol: str, side: str, now: Optional[datetime] = None,
                          window_minutes: float = 10.0) -> Optional[TradeIdea]:
        """Idée en cours pour ce couple symbole/sens : positions encore ouvertes, ou dernière activité
        dans la fenêtre d'agrégation (rouvrir dans le même sens sous 10 min = même idée)."""
        now = now or utcnow()
        best: Optional[TradeIdea] = None
        best_ts: Optional[datetime] = None
        for idea in self.trade_ideas.values():
            if idea.symbol != symbol or idea.side != side:
                continue
            ts = _parse_ts(idea.last_activity_at)
            fresh = bool(idea.open_tickets)
            if not fresh:
                if ts is None:
                    continue
                fresh = (now - ts) <= timedelta(minutes=window_minutes)
            if not fresh:
                continue
            if best is None or (ts is not None and (best_ts is None or ts > best_ts)):
                best, best_ts = idea, ts
        return best

    def projected_trade_idea_risk(self, symbol: str, side: str, add_risk_money: float,
                                  now: Optional[datetime] = None, window_minutes: float = 10.0) -> float:
        idea = self.active_trade_idea(symbol, side, now, window_minutes)
        return (idea.risk_money if idea else 0.0) + max(0.0, add_risk_money)

    def register_trade_idea(self, symbol: str, side: str, risk_money: float, ticket: Optional[int] = None,
                            now: Optional[datetime] = None, window_minutes: float = 10.0) -> TradeIdea:
        """Rattache une entrée à l'idée active, ou en ouvre une nouvelle. Le risque est CUMULÉ :
        une perte déjà encaissée sur l'idée ne libère pas de budget pour la suivante."""
        now = now or utcnow()
        idea = self.active_trade_idea(symbol, side, now, window_minutes)
        if idea is None:
            idea = TradeIdea(idea_id=f"{symbol}|{side}|{now.isoformat()}", symbol=symbol, side=side,
                             opened_at=now.isoformat())
            self.trade_ideas[idea.idea_id] = idea
        idea.last_activity_at = now.isoformat()
        idea.risk_money += max(0.0, risk_money)
        idea.entries += 1
        if ticket is not None and int(ticket) not in idea.open_tickets:
            idea.open_tickets.append(int(ticket))
        self.prune_trade_ideas(now)
        return idea

    def close_trade_idea_position(self, ticket: int, pnl: float, now: Optional[datetime] = None) -> Optional[TradeIdea]:
        """Enregistre la fermeture d'une position dans son idée (P&L cumulé pour la règle de cohérence)."""
        now = now or utcnow()
        t = int(ticket)
        for idea in self.trade_ideas.values():
            if t in idea.open_tickets:
                idea.open_tickets.remove(t)
                if t not in idea.closed_tickets:
                    idea.closed_tickets.append(t)
                idea.realized_pnl += float(pnl)
                idea.last_activity_at = now.isoformat()
                return idea
        return None

    def record_trading_day(self, now: Optional[datetime] = None) -> None:
        """Compte la journée prop en cours comme journée de trading effective (minimum imposé par la
        prop firm) et note la dernière entrée (règle d'au moins une transaction par semaine)."""
        now = now or utcnow()
        self.last_trade_at = now.isoformat()
        if self.daily.day and self.daily.day not in self.trading_days:
            self.trading_days.append(self.daily.day)
            if len(self.trading_days) > 400:
                del self.trading_days[:-400]

    def days_since_last_trade(self, now: Optional[datetime] = None) -> Optional[float]:
        ts = _parse_ts(self.last_trade_at)
        if ts is None:
            return None
        return ((now or utcnow()) - ts).total_seconds() / 86400.0

    def consistency_share_percent(self) -> float:
        """Part du profit total détenue par la meilleure idée (règle de cohérence prop, 25 % chez FOXX).

        0 % si aucune idée gagnante : la règle ne s'applique qu'à un profit existant.
        """
        gains = [i.realized_pnl for i in self.trade_ideas.values() if i.realized_pnl > 0]
        total = sum(gains)
        if total <= 0:
            return 0.0
        return 100.0 * max(gains) / total

    def prune_trade_ideas(self, now: Optional[datetime] = None, keep_days: int = 120, max_ideas: int = 2000) -> None:
        """Purge les idées closes et anciennes. Une idée avec des positions ouvertes n'est jamais purgée."""
        now = now or utcnow()
        limit = now - timedelta(days=keep_days)
        for key, idea in list(self.trade_ideas.items()):
            if idea.open_tickets:
                continue
            ts = _parse_ts(idea.last_activity_at) or _parse_ts(idea.opened_at)
            if ts is not None and ts < limit:
                self.trade_ideas.pop(key, None)
        if len(self.trade_ideas) > max_ideas:
            closed = [(k, _parse_ts(i.last_activity_at) or datetime.min.replace(tzinfo=timezone.utc))
                      for k, i in self.trade_ideas.items() if not i.open_tickets]
            closed.sort(key=lambda kv: kv[1])
            for key, _ in closed[: len(self.trade_ideas) - max_ideas]:
                self.trade_ideas.pop(key, None)

    def public_dict(self) -> dict:
        d = asdict(self)
        d["daily_pnl"] = self.daily_pnl()
        d["daily_pnl_percent"] = self.daily_pnl_percent()
        d["daily_drawdown_percent"] = self.daily_drawdown_percent()
        d["overall_drawdown_percent"] = self.overall_drawdown_percent()
        d["open_risk_percent"] = self.open_risk_percent()
        d["open_risk_money"] = self.open_risk_money()
        d["prop_daily_loss_percent"] = self.prop_daily_loss_percent()
        d["prop_overall_loss_percent"] = self.prop_overall_loss_percent()
        d["consistency_share_percent"] = self.consistency_share_percent()
        return d


class StateStore:
    """Lecture/écriture atomique de SystemState (thread-safe dans un processus, sûr entre processus via rename)."""

    def __init__(self, state_dir: Path, filename: str = "system_state.json"):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / filename
        self._lock = threading.RLock()
        self.state: SystemState = self.load()

    @staticmethod
    def _from_dict(d: dict) -> SystemState:
        """Reconstruit l'état ; une sous-section invalide (daily/model_budget/une bot_position) est ignorée
        et consignée dans `load_warnings` plutôt que de jeter tout l'état (clés d'idempotence comprises)."""
        st = SystemState()
        if not isinstance(d, dict):
            raise TypeError("system_state.json : objet JSON attendu")
        for k, v in d.items():
            if k == "daily":
                if isinstance(v, dict):
                    st.daily = DailyStats(**{kk: vv for kk, vv in v.items() if kk in DailyStats.__dataclass_fields__})
                else:
                    st.load_warnings.append("daily invalide : valeur par défaut")
            elif k == "model_budget":
                if isinstance(v, dict):
                    st.model_budget = ModelBudget(**{kk: vv for kk, vv in v.items() if kk in ModelBudget.__dataclass_fields__})
                else:
                    st.load_warnings.append("model_budget invalide : valeur par défaut")
            elif k == "bot_positions":
                st.bot_positions = {}
                if not isinstance(v, dict):
                    st.load_warnings.append("bot_positions invalide : ignoré (ré-adoption par PositionManager.sync)")
                    continue
                for t, p in v.items():
                    try:
                        st.bot_positions[str(t)] = BotPositionPlan(
                            **{kk: vv for kk, vv in p.items() if kk in BotPositionPlan.__dataclass_fields__})
                    except (TypeError, ValueError, AttributeError, KeyError) as e:
                        st.load_warnings.append(f"bot_position {t} invalide ({type(e).__name__}) : ignorée")
            elif k == "trade_ideas":
                st.trade_ideas = {}
                if not isinstance(v, dict):
                    st.load_warnings.append("trade_ideas invalide : ignoré (agrégation prop repart à vide)")
                    continue
                for key, idea in v.items():
                    try:
                        st.trade_ideas[str(key)] = TradeIdea(
                            **{kk: vv for kk, vv in idea.items() if kk in TradeIdea.__dataclass_fields__})
                    except (TypeError, ValueError, AttributeError, KeyError) as e:
                        st.load_warnings.append(f"trade_idea {key} invalide ({type(e).__name__}) : ignorée")
            elif k == "load_warnings":
                continue  # diagnostic du chargement courant uniquement, jamais rechargé depuis le fichier
            elif k in SystemState.__dataclass_fields__:
                setattr(st, k, v)
        return st

    def load(self) -> SystemState:
        with self._lock:
            if not self.path.exists():
                return SystemState()
            try:
                text = self.path.read_text(encoding="utf-8")
            except OSError:
                # fichier momentanément verrouillé (Windows) : conserver le dernier état connu
                return getattr(self, "state", None) or SystemState()
            try:
                return self._from_dict(json.loads(text))
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError, KeyError):
                backup = self.path.with_suffix(".corrupt.json")
                try:
                    os.replace(self.path, backup)
                except OSError:
                    pass
                return SystemState()

    def reload(self) -> SystemState:
        with self._lock:
            self.state = self.load()
            return self.state

    def save(self, state: SystemState | None = None) -> None:
        with self._lock:
            if state is not None:
                self.state = state
            data = json.dumps(asdict(self.state), ensure_ascii=False, indent=1, default=str)
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                _replace_with_retry(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    # ---------- commandes inter-processus ----------
    @property
    def commands_path(self) -> Path:
        return self.dir / "commands.jsonl"

    def push_command(self, command: str, args: dict | None = None, source: str = "cli") -> dict:
        rec = {"ts": utcnow().isoformat(), "command": command.upper(), "args": args or {}, "source": source}
        with open(self.commands_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    @property
    def commands_processing_path(self) -> Path:
        return self.dir / "commands.processing.jsonl"

    @staticmethod
    def _read_jsonl(p: Path) -> list[dict]:
        out: list[dict] = []
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            return out
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def pop_commands(self) -> list[dict]:
        """Dépile les commandes CLI/MCP sans course inter-processus.

        Lecture+troncature laissait une fenêtre où une commande (PANIC compris) poussée par un autre
        processus était effacée sans être exécutée. On renomme atomiquement `commands.jsonl` en
        `commands.processing.jsonl` : les appends postérieurs recréent un nouveau `commands.jsonl`.
        Un fichier `processing` laissé par un crash (entre rename et exécution) est relu en premier.
        """
        p = self.commands_path
        work = self.commands_processing_path
        out: list[dict] = []
        with self._lock:
            if work.exists():
                out.extend(self._read_jsonl(work))
                try:
                    os.unlink(work)
                except OSError:
                    pass
            if p.exists():
                try:
                    _replace_with_retry(p, work, attempts=3)
                except FileNotFoundError:
                    return out
                except OSError:
                    # fichier encore ouvert par la CLI (Windows) : rien n'est perdu, on réessaie au cycle suivant
                    return out
                out.extend(self._read_jsonl(work))
                try:
                    os.unlink(work)
                except OSError:
                    pass
        return out

    def heartbeat_age(self, which: str = "orchestrator") -> float:
        ts = self.state.orchestrator_heartbeat if which == "orchestrator" else self.state.watchdog_heartbeat
        if not ts:
            return float("inf")
        try:
            return (utcnow() - datetime.fromisoformat(ts)).total_seconds()
        except ValueError:
            return float("inf")
