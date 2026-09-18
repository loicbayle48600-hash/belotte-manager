"""Chargement de la configuration YAML + variables d'environnement.

Les credentials (login/mot de passe broker, clés API) ne sont JAMAIS lus depuis
les YAML : uniquement depuis l'environnement (fichier .env chargé par l'appelant).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILES = ["system", "risk", "models", "markets", "prop_firms", "strategies", "news_sources"]


def project_home() -> Path:
    env = os.environ.get("TRADINGLAB_HOME")
    if env:
        return Path(env)
    # src/tradinglab/core/config.py -> racine projet
    return Path(__file__).resolve().parents[3]


def load_dotenv(path: Path | None = None) -> None:
    """Charge un .env minimal (KEY=VALUE) sans écraser l'environnement existant."""
    path = path or project_home() / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.split("  #")[0].strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def _deep_get(d: dict, path: str, default: Any = None) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


@dataclass
class Settings:
    home: Path
    raw: dict = field(default_factory=dict)

    # ---------- accès génériques ----------
    def get(self, path: str, default: Any = None) -> Any:
        return _deep_get(self.raw, path, default)

    def section(self, name: str) -> dict:
        return dict(self.raw.get(name, {}))

    # ---------- raccourcis ----------
    @property
    def system(self) -> dict:
        return self.raw.get("system", {})

    @property
    def risk(self) -> dict:
        return self.raw.get("risk", {})

    @property
    def correlation(self) -> dict:
        return self.raw.get("correlation", {})

    @property
    def profit_management(self) -> dict:
        return self.raw.get("profit_management", {})

    @property
    def daily_profit(self) -> dict:
        return self.raw.get("daily_profit", {})

    @property
    def models(self) -> dict:
        return self.raw.get("models", {})

    @property
    def markets(self) -> dict:
        return self.raw.get("markets", {})

    @property
    def prop(self) -> dict:
        return self.raw.get("prop", {})

    @property
    def learning(self) -> dict:
        return self.raw.get("learning", {})

    @property
    def backtest(self) -> dict:
        return self.raw.get("backtest", {})

    @property
    def news(self) -> dict:
        return self.raw.get("news", {})

    @property
    def scheduler(self) -> dict:
        return self.raw.get("scheduler", {})

    @property
    def execution(self) -> dict:
        return self.raw.get("execution", {})

    @property
    def magic(self) -> int:
        return int(self.system.get("magic_number", 51000))

    @property
    def autonomous_demo(self) -> bool:
        return bool(self.system.get("autonomous_demo", True))

    @property
    def autonomous_prop(self) -> bool:
        """Ne peut être vrai que si PROP_RULES_VERIFIED et autorisation explicite."""
        p = self.prop
        return (
            bool(self.system.get("autonomous_prop", False))
            and bool(p.get("prop_rules_verified", False))
            and bool(p.get("user_explicitly_authorized_prop_automation", False))
        )

    # ---------- chemins ----------
    def path(self, *parts: str) -> Path:
        p = self.home.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def state_dir(self) -> Path:
        d = self.home / "state"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def logs_dir(self) -> Path:
        d = self.home / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def data_dir(self) -> Path:
        d = self.home / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def reports_dir(self) -> Path:
        d = self.home / "reports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------- secrets (environnement uniquement) ----------
    @staticmethod
    def secret(name: str) -> str | None:
        v = os.environ.get(name)
        return v if v else None

    @property
    def broker_kind(self) -> str:
        return os.environ.get("TRADINGLAB_BROKER", "mt5").lower()


def load_settings(home: Path | None = None, overrides: dict | None = None) -> Settings:
    home = Path(home) if home else project_home()
    load_dotenv(home / ".env")
    raw: dict = {}
    cfg_dir = home / "config"
    for name in CONFIG_FILES:
        f = cfg_dir / f"{name}.yaml"
        if f.exists():
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            for k, v in data.items():
                if isinstance(v, dict) and isinstance(raw.get(k), dict):
                    raw[k].update(v)
                else:
                    raw[k] = v
    if overrides:
        for k, v in overrides.items():
            if isinstance(v, dict) and isinstance(raw.get(k), dict):
                raw[k] = {**raw[k], **v}
            else:
                raw[k] = v
    return Settings(home=home, raw=raw)
