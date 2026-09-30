"""Shadow trading : les agents SHADOW/CANDIDATE tradent virtuellement (entry/sl/tp/timestamp) sans aucun ordre réel."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from ..core.clock import TF_SECONDS
from ..core.types import AgentStatus, Side, TradeCandidate, utcnow
from ..learning.store import LearningStore, TradeRecord
from ..market_data.feed import MarketSnapshot

MAX_EXECUTED_KEYS = 2000


@dataclass
class ShadowPosition:
    id: str
    agent_id: str
    symbol: str
    side: str
    entry: float
    sl: float
    tp: float
    opened_at: str
    regime: str
    session: str
    setup_score: float
    max_r: float = 0.0
    min_r: float = 0.0
    bar_time: str = ""
    mode: str = "shadow"       # "shadow" (agents SHADOW/CANDIDATE) ou "paper" (signal d'un agent LIVE non exécuté, 2026-09-28)
    reason: str = ""           # mode "paper" : pourquoi le signal n'a pas été exécuté (verrou, plafond, créneaux…)
    key: str = ""              # clé d'idempotence du candidat : retirée si le même signal part finalement en réel
    entry_kind: str = "MARKET"  # 2026-09-28 : "LIMIT" / "STOP" = ordre en attente simulé (rempli si une barre touche le prix)
    order_price: float = 0.0
    pending: bool = False      # ordre non encore rempli
    expires_at: str = ""       # annulation de l'ordre après `expiry_bars` barres de l'unité d'entrée
    cost_r: float = 0.0        # 2026-09-29 : spread d'entrée en R, déduit du résultat (entrées M1 : le spread y pèse lourd)
    entry_tf: str = ""         # 2026-10-01 : unité d'entrée de l'agent (durée maximale de la position, voir `delai_max_heures`)


#: 2026-10-01 (« trades semi-longs ») : une position shadow d'un agent H4 / D1 / W1 était coupée à 72 h comme un trade
#: intraday ; elle garde désormais le temps de son unité de temps. Intraday (M1 … H2) : `max_hours` (72 h), inchangé.
DELAI_MAX_HEURES = {"H4": 240.0, "D1": 480.0, "W1": 1440.0}


def delai_max_heures(entry_tf: str, defaut: float = 72.0) -> float:
    return max(float(defaut), DELAI_MAX_HEURES.get(str(entry_tf or "").upper(), float(defaut)))


class ShadowTrader:
    def __init__(self, store: LearningStore, state_file: Path, risk_money: float = 100.0, max_open: int = 50,
                 max_per_agent: int = 0):
        self.store = store
        self.file = Path(state_file)
        self.risk_money = risk_money
        self.max_open = max_open
        # 2026-09-25 : 48 des 50 places étaient tenues par les 6 agents de cycle long (positions H4 gardées des jours) ;
        # les 100 nouveaux agents SHADOW ne pouvaient rien ouvrir. 0 = pas de plafond par agent.
        self.max_per_agent = int(max_per_agent or 0)
        self.positions: dict[str, ShadowPosition] = {}
        # dict ordonné par ancienneté (insertion) : la troncature garde les clés les plus RÉCENTES, tous symboles confondus
        self.executed: dict[str, None] = {}
        self._load()

    def _load(self) -> None:
        if self.file.exists():
            try:
                d = json.loads(self.file.read_text(encoding="utf-8"))
                self.positions = {k: ShadowPosition(**v) for k, v in d.get("positions", {}).items()}
                self.executed = dict.fromkeys(list(d.get("executed", []))[-MAX_EXECUTED_KEYS:])
            except (json.JSONDecodeError, TypeError):
                pass

    def _save(self) -> None:
        """Écriture atomique (tempfile + os.replace) : un crash pendant l'écriture ne rend jamais l'état illisible."""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.executed = dict.fromkeys(list(self.executed)[-MAX_EXECUTED_KEYS:])
        payload = json.dumps({"positions": {k: asdict(v) for k, v in self.positions.items()},
                              "executed": list(self.executed)}, ensure_ascii=False, indent=1)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.file)

    def open_from_candidates(self, cands: list[TradeCandidate], now: Optional[datetime] = None, mode: str = "shadow",
                             reason: str = "") -> list[ShadowPosition]:
        """``mode="paper"`` (2026-09-28, décision utilisateur) : signal d'un agent LIVE que le compte n'a pas pu prendre
        (verrou de pertes, plafond de concentration, symbole déjà porté, créneaux de revue…), suivi en papier pour que
        l'agent accumule son échantillon au rythme de ses signaux et non de la capacité du compte."""
        now = now or utcnow()
        out = []
        for c in cands:
            if len(self.positions) >= self.max_open or c.idempotency_key in self.executed:
                continue
            if any(p.symbol == c.symbol and p.agent_id == c.agent_id for p in self.positions.values()):
                continue
            if self.max_per_agent and sum(1 for p in self.positions.values() if p.agent_id == c.agent_id) >= self.max_per_agent:
                continue
            kind = str(getattr(c, "entry_kind", "MARKET") or "MARKET").upper()
            attente = kind in ("LIMIT", "STOP") and float(getattr(c, "order_price", 0.0) or 0.0) > 0
            expire = ""
            if attente:
                tf = str((getattr(c, "timeframes", None) or ["H1"])[0]).upper()
                expire = (now + timedelta(seconds=TF_SECONDS.get(tf, 3600) * int(getattr(c, "expiry_bars", 3) or 3))).isoformat()
            p = ShadowPosition(id=f"sh_{c.id}", agent_id=c.agent_id, symbol=c.symbol, side=c.side.value, entry=c.entry, sl=c.sl,
                               tp=c.tp_plan[-1] if c.tp_plan else c.entry + c.side.sign * 2.5 * c.sl_distance,
                               opened_at=now.isoformat(), regime=c.regime.value, session=c.session, setup_score=c.setup_score, bar_time=c.bar_time,
                               mode=mode, reason=reason, key=c.idempotency_key,
                               entry_kind=kind if attente else "MARKET", order_price=float(getattr(c, "order_price", 0.0) or 0.0) if attente else 0.0,
                               pending=attente, expires_at=expire,
                               cost_r=self._cost_r(c),
                               entry_tf=str(list(getattr(c, "timeframes", None) or [""])[0] or "").upper())
            self.positions[p.id] = p
            self.executed[c.idempotency_key] = None
            out.append(p)
        if out:
            self._save()
        return out

    @staticmethod
    def _cost_r(c) -> float:
        """Spread à l'entrée en fraction du risque, pour les entrées M1 seulement (l'historique des autres unités de temps
        reste comparable) : sans cela un scalping M1 paraîtrait gagnant en ombre et perdant en réel."""
        tfs = list(getattr(c, "timeframes", None) or [])
        if not tfs or str(tfs[0]).upper() != "M1":
            return 0.0
        dist = abs(float(c.entry) - float(c.sl))
        pt = float(getattr(c, "point", 0.0) or 0.0) or float(getattr(c, "spread_price", 0.0) or 0.0)
        sp = float(getattr(c, "spread_points", 0) or 0)
        if dist <= 0 or sp <= 0:
            return 0.0
        prix = float(getattr(c, "spread_price", 0.0) or 0.0) or sp * (pt or 0.0)
        return round(prix / dist, 4) if prix > 0 else 0.0

    def forget(self, key: str) -> int:
        """Retire les positions d'ombre nées de ce signal (2026-09-28 : un signal mis en papier au cycle N — « au-delà des
        revues IA », « plafond d'entrées » — peut être exécuté en réel au cycle N+1 sur la même barre ; sans ceci il
        comptait deux fois, papier et live)."""
        morts = [pid for pid, p in self.positions.items() if p.key == key]
        for pid in morts:
            del self.positions[pid]
        if morts:
            self._save()
        return len(morts)

    def update(self, snapshots: dict[str, MarketSnapshot], now: Optional[datetime] = None, max_hours: float = 72.0) -> list[TradeRecord]:
        """Vérifie SL/TP sur les barres M5 récentes (high/low), SL prioritaire. Enregistre les trades fermés en mode 'shadow'.

        Une position dont le symbole n'a plus de snapshot ni de barres est abandonnée après ``max_hours`` (événement
        ``shadow_timeout_no_data``, aucun résultat inventé) pour ne pas occuper un slot indéfiniment.
        """
        now = now or utcnow()
        closed: list[TradeRecord] = []
        dirty = False
        for pid, p in list(self.positions.items()):
            opened = datetime.fromisoformat(p.opened_at)
            timed_out = (now - opened).total_seconds() > delai_max_heures(p.entry_tf, max_hours) * 3600
            snap = snapshots.get(p.symbol)
            df = None
            if snap is not None:
                df = snap.frames.get("M5")
                if df is None:
                    df = snap.frames.get("M15")
            if df is None or len(df) < 3:
                if timed_out:
                    self.store.agent_event(p.agent_id, "shadow_timeout_no_data", {"position": pid, "symbol": p.symbol, "opened_at": p.opened_at})
                    del self.positions[pid]
                    dirty = True
                continue
            bars = df[df["time"] > opened].iloc[:-1]
            side = Side(p.side)
            if p.pending:
                # ordre en attente (2026-09-28) : rempli au prix de l'ordre dès qu'une barre clôturée le touche, sinon
                # annulé à l'expiration ; la position remplie est suivie à partir de la barre suivante
                px = float(p.order_price)
                rempli = None
                for b in bars.itertuples():
                    hi, lo = float(b.high), float(b.low)
                    if p.entry_kind == "LIMIT":
                        touche = (lo <= px) if side is Side.BUY else (hi >= px)
                    else:
                        touche = (hi >= px) if side is Side.BUY else (lo <= px)
                    if touche:
                        rempli = b
                        break
                if rempli is not None:
                    p.pending, p.entry = False, px
                    p.opened_at = pd.Timestamp(rempli.time).isoformat()
                    self.store.agent_event(p.agent_id, "pending_filled", {"position": pid, "symbol": p.symbol, "order_price": px, "kind": p.entry_kind})
                    dirty = True
                elif p.expires_at and now >= datetime.fromisoformat(p.expires_at):
                    self.store.agent_event(p.agent_id, "pending_expired", {"position": pid, "symbol": p.symbol, "order_price": px, "kind": p.entry_kind})
                    del self.positions[pid]
                    dirty = True
                continue
            dist = abs(p.entry - p.sl)
            exit_px, reason = None, ""
            for b in bars.itertuples():
                hi, lo = float(b.high), float(b.low)
                r_hi = side.sign * (hi - p.entry) / dist
                r_lo = side.sign * (lo - p.entry) / dist
                p.max_r = max(p.max_r, r_hi, r_lo)
                p.min_r = min(p.min_r, r_hi, r_lo)
                if (side is Side.BUY and lo <= p.sl) or (side is Side.SELL and hi >= p.sl):
                    exit_px, reason = p.sl, "sl"
                    break
                if (side is Side.BUY and hi >= p.tp) or (side is Side.SELL and lo <= p.tp):
                    exit_px, reason = p.tp, "tp"
                    break
            if exit_px is None and timed_out:
                exit_px, reason = float(df["close"].iloc[-2]), "timeout"
            if exit_px is None:
                continue
            r = side.sign * (exit_px - p.entry) / dist - float(p.cost_r or 0.0)
            rec = TradeRecord(ticket=0, agent_id=p.agent_id, symbol=p.symbol, side=p.side, entry=p.entry, sl=p.sl, risk_money=self.risk_money,
                              risk_percent=0.0, result_r=round(r, 4), pnl=round(r * self.risk_money, 2), opened_at=p.opened_at,
                              closed_at=now.isoformat(), mode=p.mode or "shadow", regime=p.regime, session=p.session, tp=[p.tp],
                              features={"setup_score": p.setup_score, **({"paper_reason": p.reason} if p.reason else {})},
                              mae_r=round(-min(0.0, p.min_r), 3), mfe_r=round(max(0.0, p.max_r), 3),
                              exit_reason=reason, candidate_id=p.id, duration_sec=round((now - opened).total_seconds(), 1))
            self.store.record_trade(rec)
            closed.append(rec)
            del self.positions[pid]
            dirty = True
        if dirty:
            self._save()
        return closed

    def summary(self) -> dict:
        return {"open": len(self.positions), "by_agent": {a: sum(1 for p in self.positions.values() if p.agent_id == a) for a in {p.agent_id for p in self.positions.values()}}}
