"""Champion / Challenger : pipeline de validation obligatoire et promotion contrôlée.

IDEA → BACKTEST → OUT_OF_SAMPLE → WALK_FORWARD → MONTE_CARLO → SHADOW → STATISTICAL_REVIEW → RISK_REVIEW → PROMOTION
Interdit : RESEARCH → LIVE direct. Chaque étape est persistée (data/research/<agent_id>.json) avec versioning et rollback.
"""
from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from ..agents.registry import AgentRegistry, AgentSpec
from ..backtest.engine import BTCosts, monte_carlo, parameter_sensitivity, run_backtest, split_in_out_of_sample, walk_forward
from ..core.types import AgentStatus, SymbolSpec, utcnow
from ..learning.store import LearningStore
from .adapters import make_signal_factory, make_signal_fn


class Stage(str, Enum):
    IDEA = "IDEA"
    BACKTEST = "BACKTEST"
    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"
    WALK_FORWARD = "WALK_FORWARD"
    MONTE_CARLO = "MONTE_CARLO"
    SHADOW = "SHADOW"
    STATISTICAL_REVIEW = "STATISTICAL_REVIEW"
    RISK_REVIEW = "RISK_REVIEW"
    PROMOTION = "PROMOTION"


STAGE_ORDER = [s for s in Stage]


class PromotionError(RuntimeError):
    """Levée quand une promotion tente de contourner le pipeline."""


@dataclass
class ValidationRecord:
    agent_id: str
    version: str = "1.0"
    stages: dict = field(default_factory=dict)      # stage -> {"passed": bool, "metrics": {...}, "ts": iso}
    history: list = field(default_factory=list)     # versions précédentes (rollback)

    def passed(self, stage: Stage) -> bool:
        return bool(self.stages.get(stage.value, {}).get("passed"))

    def next_stage(self) -> Optional[Stage]:
        for s in STAGE_ORDER[1:]:
            if not self.passed(s):
                return s
        return None

    def all_passed_until(self, stage: Stage) -> bool:
        for s in STAGE_ORDER[1:]:
            if s is stage:
                return True
            if not self.passed(s):
                return False
        return True

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "version": self.version, "stages": self.stages, "history": self.history}


DataProvider = Callable[[str, str, int], pd.DataFrame]   # (symbol, tf, bars) -> df


class ResearchPipeline:
    def __init__(self, registry: AgentRegistry, store: LearningStore, learning_cfg: dict, backtest_cfg: dict,
                 data_provider: DataProvider, symbol_specs: dict[str, SymbolSpec], research_dir: Path, journal=None,
                 entry_tf: str = "M15", bars: int = 3000):
        self.registry = registry
        self.store = store
        self.cfg = learning_cfg
        self.bt_cfg = backtest_cfg
        self.data = data_provider
        self.specs = symbol_specs
        self.dir = Path(research_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.journal = journal
        self.entry_tf = entry_tf
        self.bars = bars

    # ---------- persistance ----------
    def record(self, agent_id: str) -> ValidationRecord:
        f = self.dir / f"{agent_id}.json"
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                return ValidationRecord(agent_id=agent_id, version=d.get("version", "1.0"), stages=d.get("stages", {}), history=d.get("history", []))
            except json.JSONDecodeError:
                pass
        return ValidationRecord(agent_id=agent_id)

    def save(self, rec: ValidationRecord) -> None:
        (self.dir / f"{rec.agent_id}.json").write_text(json.dumps(rec.to_dict(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    def _set(self, rec: ValidationRecord, stage: Stage, passed: bool, metrics: dict) -> None:
        rec.stages[stage.value] = {"passed": bool(passed), "metrics": metrics, "ts": utcnow().isoformat()}
        self.save(rec)
        self.store.agent_event(rec.agent_id, f"stage_{stage.value}", {"passed": passed, **{k: v for k, v in metrics.items() if not isinstance(v, (list, dict))}})
        if self.journal:
            self.journal.event("research_stage", agent_id=rec.agent_id, stage=stage.value, passed=passed)

    # ---------- outils ----------
    def _symbol_for(self, spec: AgentSpec) -> Optional[str]:
        for m in spec.markets:
            for s, ss in self.specs.items():
                if m in (ss.root, ss.asset_class, s):
                    return s
        cls = {"forex_majors": "forex", "forex_minors": "forex"}
        for m in spec.markets:
            for s, ss in self.specs.items():
                if cls.get(m) == ss.asset_class:
                    return s
        return next(iter(self.specs), None)

    def _costs(self, ss: SymbolSpec) -> BTCosts:
        return BTCosts(spread_points=int(self.bt_cfg.get("default_spread_points", ss.spread_points or 12)),
                       commission_per_lot=float(self.bt_cfg.get("commission_per_lot", 0.0)),
                       slippage_points=int(self.bt_cfg.get("slippage_points", 3)), point=ss.point, tick_value=ss.tick_value, tick_size=ss.tick_size)

    def _thresholds(self) -> tuple[int, float, float, float]:
        return (int(self.cfg.get("min_sample_size", 40)), float(self.cfg.get("min_profit_factor", 1.2)),
                float(self.cfg.get("min_expectancy_r", 0.10)), float(self.cfg.get("max_drawdown_r", 15.0)))

    # ---------- étapes ----------
    def stage_backtest(self, spec: AgentSpec) -> ValidationRecord:
        rec = self.record(spec.agent_id)
        sym = self._symbol_for(spec)
        ss = self.specs[sym]
        df = self.data(sym, self.entry_tf, self.bars)
        df_is, df_oos = split_in_out_of_sample(df, 0.3)
        fn = make_signal_fn(spec, ss, self.entry_tf)
        res = run_backtest(df_is, fn, self._costs(ss))
        n, pf, ex, dd = self._thresholds()
        m = res.metrics
        # en backtest on exige la moitié de l'échantillon minimal (le reste vient du shadow)
        passed = m.sample_size >= max(10, n // 2) and m.profit_factor >= pf and m.expectancy_r >= ex and m.max_drawdown_r <= dd
        self._set(rec, Stage.BACKTEST, passed, {"symbol": sym, **m.to_dict()})
        if passed and spec.status == AgentStatus.RESEARCH.value:
            self.registry.set_status(spec.agent_id, AgentStatus.BACKTEST)
        return rec

    def stage_out_of_sample(self, spec: AgentSpec) -> ValidationRecord:
        rec = self.record(spec.agent_id)
        if not rec.passed(Stage.BACKTEST):
            self._set(rec, Stage.OUT_OF_SAMPLE, False, {"error": "BACKTEST non validé"})
            return rec
        sym = rec.stages[Stage.BACKTEST.value]["metrics"]["symbol"]
        ss = self.specs[sym]
        df = self.data(sym, self.entry_tf, self.bars)
        _, df_oos = split_in_out_of_sample(df, 0.3)
        res = run_backtest(df_oos, make_signal_fn(spec, ss, self.entry_tf), self._costs(ss))
        n, pf, ex, dd = self._thresholds()
        m = res.metrics
        passed = m.sample_size >= 5 and m.expectancy_r > 0 and m.profit_factor >= 1.0
        self._set(rec, Stage.OUT_OF_SAMPLE, passed, m.to_dict())
        return rec

    def stage_walk_forward(self, spec: AgentSpec) -> ValidationRecord:
        rec = self.record(spec.agent_id)
        if not rec.passed(Stage.OUT_OF_SAMPLE):
            self._set(rec, Stage.WALK_FORWARD, False, {"error": "OUT_OF_SAMPLE non validé"})
            return rec
        sym = rec.stages[Stage.BACKTEST.value]["metrics"]["symbol"]
        ss = self.specs[sym]
        df = self.data(sym, self.entry_tf, self.bars)
        grid = [dict(spec.params)]
        jit = float(self.cfg.get("challenger_generation", {}).get("parameter_jitter_percent", 20)) / 100
        for k, v in spec.params.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                grid.append({**spec.params, k: v * (1 + jit)})
                grid.append({**spec.params, k: v * (1 - jit)})
        wf = walk_forward(df, make_signal_factory(spec, ss, self.entry_tf), grid, self._costs(ss), folds=int(self.bt_cfg.get("walk_forward_folds", 4)))
        sens = parameter_sensitivity(df.iloc[: len(df) // 2], make_signal_factory(spec, ss, self.entry_tf), spec.params, jit * 100, self._costs(ss))
        passed = wf.robustness_ratio >= 0.5 and wf.oos_metrics.expectancy_r > 0 and bool(sens.get("stable", False))
        self._set(rec, Stage.WALK_FORWARD, passed, {"robustness_ratio": wf.robustness_ratio, "oos": wf.oos_metrics.to_dict(),
                                                    "sensitivity_stable": sens.get("stable"), "folds": len(wf.folds)})
        return rec

    def stage_monte_carlo(self, spec: AgentSpec) -> ValidationRecord:
        rec = self.record(spec.agent_id)
        if not rec.passed(Stage.WALK_FORWARD):
            self._set(rec, Stage.MONTE_CARLO, False, {"error": "WALK_FORWARD non validé"})
            return rec
        sym = rec.stages[Stage.BACKTEST.value]["metrics"]["symbol"]
        ss = self.specs[sym]
        df = self.data(sym, self.entry_tf, self.bars)
        res = run_backtest(df, make_signal_fn(spec, ss, self.entry_tf), self._costs(ss))
        rs = [t.r_multiple for t in res.trades]
        mc = monte_carlo(rs, runs=int(self.bt_cfg.get("monte_carlo_runs", 500)))
        n, pf, ex, dd = self._thresholds()
        passed = bool(rs) and mc["p95_max_dd_r"] <= dd and mc["prob_negative"] < 0.35 and mc["p05_total_r"] > -dd
        self._set(rec, Stage.MONTE_CARLO, passed, mc)
        if passed and spec.status in (AgentStatus.RESEARCH.value, AgentStatus.BACKTEST.value):
            self.registry.set_status(spec.agent_id, AgentStatus.SHADOW)
        return rec

    def stage_shadow(self, spec: AgentSpec) -> ValidationRecord:
        rec = self.record(spec.agent_id)
        if not rec.passed(Stage.MONTE_CARLO):
            self._set(rec, Stage.SHADOW, False, {"error": "MONTE_CARLO non validé"})
            return rec
        st = self.store.agent_stats(spec.agent_id, mode="shadow")
        min_shadow = int(self.cfg.get("min_shadow_trades", max(20, int(self.cfg.get("min_sample_size", 40)) // 2)))
        passed = st.sample_size >= min_shadow and st.expectancy_r > 0 and st.profit_factor >= 1.0
        self._set(rec, Stage.SHADOW, passed, {"shadow_sample": st.sample_size, "expectancy_r": st.expectancy_r, "profit_factor": st.profit_factor, "required": min_shadow})
        return rec

    def stage_statistical_review(self, spec: AgentSpec) -> ValidationRecord:
        rec = self.record(spec.agent_id)
        if not rec.passed(Stage.SHADOW):
            self._set(rec, Stage.STATISTICAL_REVIEW, False, {"error": "SHADOW non validé"})
            return rec
        cmp = self.compare_champion_vs_challenger(spec)
        bt = rec.stages[Stage.BACKTEST.value]["metrics"]
        n, pf, ex, dd = self._thresholds()
        total = bt.get("sample_size", 0) + rec.stages[Stage.SHADOW.value]["metrics"].get("shadow_sample", 0)
        passed = total >= n and cmp.get("challenger_better_or_equal", False)
        self._set(rec, Stage.STATISTICAL_REVIEW, passed, {"total_sample": total, "required": n, **cmp})
        if passed:
            self.registry.set_status(spec.agent_id, AgentStatus.CANDIDATE)
        return rec

    def stage_risk_review(self, spec: AgentSpec) -> ValidationRecord:
        rec = self.record(spec.agent_id)
        if not rec.passed(Stage.STATISTICAL_REVIEW):
            self._set(rec, Stage.RISK_REVIEW, False, {"error": "STATISTICAL_REVIEW non validé"})
            return rec
        n, pf, ex, dd = self._thresholds()
        mc = rec.stages[Stage.MONTE_CARLO.value]["metrics"]
        bt = rec.stages[Stage.BACKTEST.value]["metrics"]
        passed = mc.get("p95_max_dd_r", 99) <= dd and bt.get("max_drawdown_r", 99) <= dd
        self._set(rec, Stage.RISK_REVIEW, passed, {"p95_max_dd_r": mc.get("p95_max_dd_r"), "bt_max_dd_r": bt.get("max_drawdown_r"), "limit": dd})
        return rec

    def promote(self, agent_id: str) -> ValidationRecord:
        """Promotion LIVE uniquement si TOUTES les étapes précédentes sont validées. Sinon PromotionError."""
        spec = self.registry.get(agent_id)
        if spec is None:
            raise PromotionError(f"agent {agent_id} inconnu")
        rec = self.record(agent_id)
        missing = [s.value for s in STAGE_ORDER[1:-1] if not rec.passed(s)]
        if missing:
            self._set(rec, Stage.PROMOTION, False, {"missing": missing})
            raise PromotionError(f"promotion refusée pour {agent_id} : étapes manquantes {missing}")
        rec.history.append({"version": rec.version, "status_before": spec.status, "ts": utcnow().isoformat()})
        self._set(rec, Stage.PROMOTION, True, {"from": spec.status, "to": AgentStatus.LIVE.value})
        self.registry.set_status(agent_id, AgentStatus.LIVE)
        return rec

    def rollback(self, agent_id: str, to_status: AgentStatus = AgentStatus.SHADOW) -> None:
        rec = self.record(agent_id)
        rec.stages.pop(Stage.PROMOTION.value, None)
        rec.history.append({"rollback_to": to_status.value, "ts": utcnow().isoformat()})
        self.save(rec)
        self.registry.set_status(agent_id, to_status)
        self.store.agent_event(agent_id, "rollback", {"to": to_status.value})

    def advance(self, agent_id: str) -> ValidationRecord:
        """Exécute la prochaine étape non validée (sans jamais sauter d'étape)."""
        spec = self.registry.get(agent_id)
        rec = self.record(agent_id)
        nxt = rec.next_stage()
        if nxt is None or spec is None:
            return rec
        fn = {Stage.BACKTEST: self.stage_backtest, Stage.OUT_OF_SAMPLE: self.stage_out_of_sample, Stage.WALK_FORWARD: self.stage_walk_forward,
              Stage.MONTE_CARLO: self.stage_monte_carlo, Stage.SHADOW: self.stage_shadow,
              Stage.STATISTICAL_REVIEW: self.stage_statistical_review, Stage.RISK_REVIEW: self.stage_risk_review}
        if nxt is Stage.PROMOTION:
            try:
                return self.promote(agent_id)
            except PromotionError:
                return self.record(agent_id)
        return fn[nxt](spec)

    # ---------- champion / challenger ----------
    def champion_of(self, family: str) -> Optional[AgentSpec]:
        best, best_ex = None, float("-inf")
        for a in self.registry.by_status(AgentStatus.LIVE):
            if a.family != family or not a.generates_trades:
                continue
            st = self.store.agent_stats(a.agent_id)
            ex = st.expectancy_r if st.sample_size >= 10 else 0.0
            if ex > best_ex:
                best, best_ex = a, ex
        return best

    def compare_champion_vs_challenger(self, challenger: AgentSpec) -> dict:
        champ = self.champion_of(challenger.family)
        ch_live = self.store.agent_stats(challenger.agent_id, mode="shadow")
        rec = self.record(challenger.agent_id)
        ch_bt = rec.stages.get(Stage.BACKTEST.value, {}).get("metrics", {})
        ch_ex = ch_live.expectancy_r if ch_live.sample_size >= 10 else ch_bt.get("expectancy_r", 0.0)
        if champ is None:
            return {"champion": None, "challenger_expectancy_r": ch_ex, "challenger_better_or_equal": ch_ex > 0}
        cs = self.store.agent_stats(champ.agent_id)
        champ_ex = cs.expectancy_r if cs.sample_size >= 10 else 0.0
        return {"champion": champ.agent_id, "champion_expectancy_r": champ_ex, "champion_sample": cs.sample_size,
                "challenger_expectancy_r": ch_ex, "challenger_sample": ch_live.sample_size,
                "champion_dd_r": cs.max_drawdown_r, "challenger_dd_r": ch_live.max_drawdown_r,
                "challenger_better_or_equal": ch_ex >= champ_ex and ch_ex > 0}

    def generate_challengers(self, parent: AgentSpec, n: Optional[int] = None, seed: Optional[int] = None) -> list[AgentSpec]:
        cfg = self.cfg.get("challenger_generation", {})
        n = n or int(cfg.get("max_new_per_cycle", 3))
        jit = float(cfg.get("parameter_jitter_percent", 20)) / 100
        rng = random.Random(seed if seed is not None else f"{parent.agent_id}{utcnow().date()}")
        out = []
        for _ in range(n):
            params = copy.deepcopy(parent.params)
            for k, v in params.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    params[k] = round(v * (1 + rng.uniform(-jit, jit)), 4)
            cid = self.registry.next_challenger_id(parent)
            spec = AgentSpec(**{**parent.to_dict(), "agent_id": cid, "name": f"{parent.name}_ch", "params": params,
                                "status": AgentStatus.RESEARCH.value, "version": "1.0", "parent_id": parent.agent_id, "created_by": "research"})
            self.registry.add(spec)
            self.store.agent_event(cid, "challenger_created", {"parent": parent.agent_id, "params": params})
            out.append(spec)
        return out


class DegradationManager:
    """LIVE → DEGRADED → SUSPENDED sur dérive statistique. Jamais d'augmentation de risque pour 'récupérer'."""

    def __init__(self, registry: AgentRegistry, store: LearningStore, learning_cfg: dict, journal=None):
        self.registry = registry
        self.store = store
        self.cfg = learning_cfg.get("degradation", {})
        self.auto_suspend = bool(self.cfg.get("auto_suspend_degraded", True))
        self.journal = journal

    def run(self) -> list[dict]:
        changes = []
        for a in list(self.registry.agents.values()):
            if not a.generates_trades or a.status not in (AgentStatus.LIVE.value, AgentStatus.DEGRADED.value):
                continue
            st = self.store.agent_stats(a.agent_id, windows=tuple(self.cfg.get("windows", [20, 30, 50])),
                                        min_history=int(self.cfg.get("min_history", 60)),
                                        pf_drop_ratio=float(self.cfg.get("pf_drop_ratio", 0.6)),
                                        expectancy_drop_r=float(self.cfg.get("expectancy_drop_r", 0.15)))
            if st.sample_size < int(self.cfg.get("min_history", 60)):
                continue
            if a.status == AgentStatus.LIVE.value and st.degradation_score >= 0.5:
                self.registry.set_status(a.agent_id, AgentStatus.DEGRADED)
                changes.append({"agent_id": a.agent_id, "from": "LIVE", "to": "DEGRADED", "score": st.degradation_score})
            elif a.status == AgentStatus.DEGRADED.value:
                if st.degradation_score >= 0.75 and self.auto_suspend:
                    self.registry.set_status(a.agent_id, AgentStatus.SUSPENDED)
                    changes.append({"agent_id": a.agent_id, "from": "DEGRADED", "to": "SUSPENDED", "score": st.degradation_score})
                elif st.degradation_score < 0.25:
                    self.registry.set_status(a.agent_id, AgentStatus.LIVE)
                    changes.append({"agent_id": a.agent_id, "from": "DEGRADED", "to": "LIVE", "score": st.degradation_score})
        for c in changes:
            self.store.agent_event(c["agent_id"], "status_change", c)
            if self.journal:
                self.journal.event("agent_status_change", **c)
        return changes
