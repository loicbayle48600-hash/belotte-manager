"""Lance le copieur pour UN compte suiveur : ``python -m tradinglab.copy --follower <nom>``.

Le compte est décrit dans ``config/copy_trading.yaml`` ; ses identifiants sont lus dans
l'environnement (``<PREFIX>_MT5_LOGIN`` / ``_PASSWORD`` / ``_SERVER`` / ``_TERMINAL_PATH``,
chargés depuis ``.env``). Chaque suiveur exige sa PROPRE installation portable de MT5 :
le package MetaTrader5 ne tient qu'une connexion par processus.
"""
from __future__ import annotations

import argparse
import os
import sys

import yaml

from ..core.config import load_settings, project_home
from ..core.journal import Journal
from .copier import DEFAULT_MAGIC, DEFAULT_STALE_SEC, CopyTrader
from .registry import follower_size_factor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copieur de positions Trading Lab (un compte suiveur)")
    parser.add_argument("--follower", default=None, help="nom du suiveur dans config/copy_trading.yaml")
    parser.add_argument("--prefix", default=None, help="préfixe .env du suiveur (ex. COPY2) — robuste aux noms avec espaces")
    parser.add_argument("--home", default=None)
    args = parser.parse_args(argv)
    home = args.home or project_home()
    cfg_file = home / "config" / "copy_trading.yaml"
    cfg = (yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}).get("copy_trading", {})
    if not cfg.get("enabled", False):
        print("copy_trading.enabled est false : rien à faire", file=sys.stderr)
        return 1
    followers = cfg.get("followers", []) or []
    if args.prefix:
        follower = next((f for f in followers if str(f.get("env_prefix", "")) == args.prefix), None)
    elif args.follower:
        follower = next((f for f in followers if f.get("name") == args.follower), None)
    else:
        parser.error("--follower <nom> ou --prefix <COPYn> requis")
    if follower is None or not follower.get("enabled", False):
        print(f"suiveur inconnu ou désactivé: {args.prefix or args.follower}", file=sys.stderr)
        return 1
    args.follower = str(follower.get("name"))
    prefix = str(follower.get("env_prefix", "")).strip()
    missing = [f"{prefix}_MT5_{k}" for k in ("LOGIN", "PASSWORD", "SERVER", "TERMINAL_PATH")
               if not os.environ.get(f"{prefix}_MT5_{k}")]
    if not prefix or missing:
        print(f"identifiants absents de l'environnement (.env): {missing or 'env_prefix manquant'}", file=sys.stderr)
        return 1
    # le processus copieur se connecte au terminal du SUIVEUR : on remappe les variables standard
    for k in ("LOGIN", "PASSWORD", "SERVER", "TERMINAL_PATH"):
        os.environ[f"MT5_{k}"] = os.environ[f"{prefix}_MT5_{k}"]
    settings = load_settings(home)
    # Verrou anti-doublon (2026-09-22) : deux copieurs sur le même compte (deux redémarrages concurrents) ont
    # ouvert des positions en double. Un seul processus vivant par préfixe.
    lock = settings.state_dir / f"copy_{prefix}.lock"
    try:
        other = int(lock.read_text(encoding="utf-8").strip()) if lock.exists() else 0
    except ValueError:
        other = 0
    if other and other != os.getpid():
        try:
            os.kill(other, 0)
            alive = True
        except OSError:
            alive = False
        if alive:
            print(f"un copieur {prefix} tourne déjà (PID {other}) : arrêt de ce doublon", file=sys.stderr)
            return 4
    lock.write_text(str(os.getpid()), encoding="utf-8")
    from ..mt5.mock_adapter import make_broker

    broker = make_broker(os.environ.get("TRADINGLAB_BROKER", "mt5"), settings)
    if not broker.connect():
        print(f"connexion au compte suiveur impossible: {broker.last_error()}", file=sys.stderr)
        return 2
    journal = Journal(settings.logs_dir, settings.system.get("timezone_local", "UTC"),
                      component=f"copy-{args.follower}")
    # Garde-fou (2026-09-22) : un compte suiveur déclaré REAL/CONTEST par le broker (cas des prop firms) n'est
    # répliqué que si l'utilisateur l'a autorisé EXPLICITEMENT (`allow_real: true` sur ce suiveur). Même
    # principe que le maître : jamais d'argent réel sans accord explicite.
    from ..core.types import TradeMode

    acc = broker.account_info()
    mode = getattr(acc, "trade_mode", None)
    if mode is not TradeMode.DEMO and not bool(follower.get("allow_real", False)):
        journal.warn("copy trading refusé : compte suiveur non-DEMO sans allow_real", follower=args.follower,
                     trade_mode=str(getattr(mode, "value", mode)))
        print(f"compte suiveur {args.follower} déclaré {getattr(mode, 'value', mode)} par le broker : "
              f"ajoute `allow_real: true` à ce suiveur dans config/copy_trading.yaml pour autoriser la réplication",
              file=sys.stderr)
        return 3
    trader = CopyTrader(broker, settings.state_dir / "master_positions.json", journal=journal,
                        size_factor=float(follower.get("size_factor", 1.0)),
                        magic=int(cfg.get("magic_number", DEFAULT_MAGIC)),
                        stale_after_sec=float(cfg.get("stale_after_sec", DEFAULT_STALE_SEC)),
                        name=args.follower,
                        status_file=settings.state_dir / f"copy_status_{follower.get('env_prefix', 'COPY')}.json",
                        factor_source=lambda: follower_size_factor(home, args.follower, float(follower.get("size_factor", 1.0))))
    trader.stats_since = str(settings.system.get("accounts_stats_since") or "") or None
    journal.event("copy_start", follower=args.follower, size_factor=trader.size_factor, magic=trader.magic)
    trader.run_forever(float(cfg.get("poll_interval_sec", 5)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
