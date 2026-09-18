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
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional

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
    day: str = ""
    starting_equity: float = 0.0
    starting_balance: float = 0.0
    peak_equity: float = 0.0
    peak_daily_pnl: float = 0.0
    realized_pnl: float = 0.0
    trades_closed: int = 0
    wins: int = 0
    losses: int = 0


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
    daily: DailyStats = field(default_factory=DailyStats)
    bot_positions: dict[str, BotPositionPlan] = field(default_factory=dict)  # ticket(str) -> plan
    executed_keys: dict[str, str] = field(default_factory=dict)              # idempotency key -> ts
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

    def roll_day_if_needed(self, equity: float, balance: float, today: Optional[date] = None) -> bool:
        today = today or utcnow().date()
        d = today.isoformat()
        if self.daily.day == d:
            # journée déjà ouverte avec une equity nulle (compte pas encore synchronisé) : on fixe la
            # référence dès la première equity valide, sinon perte journalière/drawdown resteraient à 0 %.
            if self.daily.starting_equity <= 0 and equity > 0:
                self.daily.starting_equity = equity
                self.daily.starting_balance = balance
                self.daily.peak_equity = max(self.daily.peak_equity, equity)
            return False
        if equity <= 0:
            # equity inconnue/nulle : ne pas figer starting_equity=0 pour toute la journée, réessayer au cycle suivant
            return False
        self.daily = DailyStats(day=d, starting_equity=equity, starting_balance=balance, peak_equity=equity)
        self.consecutive_losses = 0
        self.unlock_entries("DAILY_LOSS_LIMIT")
        self.unlock_entries("GIVEBACK_FLOOR")
        self.unlock_entries("MAX_CONSECUTIVE_LOSSES")
        return True

    def update_equity(self, equity: float, balance: float) -> None:
        self.equity = equity
        self.balance = balance
        if equity > self.daily.peak_equity:
            self.daily.peak_equity = equity
        pnl = self.daily_pnl()
        if pnl > self.daily.peak_daily_pnl:
            self.daily.peak_daily_pnl = pnl
        if equity > self.overall_peak_equity:
            self.overall_peak_equity = equity

    def public_dict(self) -> dict:
        d = asdict(self)
        d["daily_pnl"] = self.daily_pnl()
        d["daily_pnl_percent"] = self.daily_pnl_percent()
        d["daily_drawdown_percent"] = self.daily_drawdown_percent()
        d["overall_drawdown_percent"] = self.overall_drawdown_percent()
        d["open_risk_percent"] = self.open_risk_percent()
        d["open_risk_money"] = self.open_risk_money()
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
