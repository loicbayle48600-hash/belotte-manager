"""Smoke test DEMO (section 34) : à lancer sur le PC Windows connecté à MT5.

1. Connexion, affichage ACCOUNT / SERVER / TRADE_MODE / BALANCE / EQUITY / CURRENCY.
2. Refus immédiat si le compte n'est pas DEMO ou ne correspond pas à account_expected.
3. Ticks + OHLC + positions.
4. Ordre de test au volume minimum acceptable par le Risk Gate, SL obligatoire.
5. Vérification SL après fill, modification SL (resserrement), fermeture, vérification disparition.
6. Aucune position test ne reste ouverte. Marché fermé → reporté.

Utilisable aussi avec --broker mock (Linux) pour valider la procédure.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradinglab.core.clock import forex_market_open  # noqa: E402
from tradinglab.core.config import load_settings  # noqa: E402
from tradinglab.core.journal import Journal  # noqa: E402
from tradinglab.core.types import OrderRequest, Side, TradeMode  # noqa: E402
from tradinglab.mt5.mock_adapter import make_broker  # noqa: E402
from tradinglab.mt5.symbols import resolve_symbols  # noqa: E402
from tradinglab.risk.risk_manager import RiskLimits, RiskManager  # noqa: E402
from tradinglab.risk.stop_loss import normalize_price, validate_stop_loss  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=None)
    ap.add_argument("--broker", default=None)
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--yes", action="store_true", help="confirme l'envoi de l'ordre de test")
    a = ap.parse_args()
    s = load_settings(Path(a.home) if a.home else None)
    journal = Journal(s.logs_dir, s.system.get("timezone_local", "UTC"), component="smoke_test")
    b = make_broker(a.broker or s.broker_kind, s)
    if not b.connect():
        print("ECHEC connexion:", b.last_error())
        return 2
    acc = b.account_info()
    if acc is None:
        print("ECHEC account_info")
        return 2
    print(f"ACCOUNT    = {acc.login}\nSERVER     = {acc.server}\nTRADE_MODE = {acc.trade_mode.value}\nBALANCE    = {acc.balance}\nEQUITY     = {acc.equity}\nCURRENCY   = {acc.currency}")
    exp = s.get("account_expected", {}) or {}
    if acc.trade_mode is not TradeMode.DEMO:
        print("REFUS : le compte n'est pas DEMO. Aucun ordre envoyé.")
        return 3
    if exp.get("login") and int(exp["login"]) != acc.login:
        print(f"REFUS : login {acc.login} différent du compte attendu {exp['login']}.")
        return 3
    print("DEMO ACCOUNT CONFIRMED")
    journal.event("smoke_account", **acc.public_dict())
    sym = resolve_symbols([a.symbol], b.symbols()).get(a.symbol)
    if not sym:
        print("symbole introuvable:", a.symbol)
        return 4
    b.symbol_select(sym)
    spec = b.symbol_info(sym)
    t = b.tick(sym)
    df = b.rates(sym, "H1", 50)
    print(f"SYMBOL={sym} tick bid={t.bid if t else None} ask={t.ask if t else None} spread={t.spread_points(spec) if t else None} pts ; OHLC H1 barres={len(df)} ; positions={len(b.positions())}")
    if t is None or df.empty:
        print("données indisponibles : test reporté")
        return 5
    if not forex_market_open(b.server_time()) and spec.asset_class == "forex":
        print("MARCHE FERME : smoke test reporté (aucun ordre).")
        return 6
    if not a.yes:
        print("Ajouter --yes pour envoyer l'ordre de test (volume minimum, SL obligatoire, fermeture immédiate).")
        return 0
    rm = RiskManager(RiskLimits.from_config(s.risk))
    atr = float((df["high"] - df["low"]).tail(14).mean())
    entry = t.ask
    sl = normalize_price(entry - max(1.0 * atr, spec.min_stop_distance * 3), spec)
    chk = validate_stop_loss(Side.BUY, entry, sl, spec, atr)
    if not chk.ok:
        print("SL invalide:", chk.reason)
        return 7
    sizing = rm.size(acc.equity, entry, sl, spec, risk_percent=0.02)
    vol = max(spec.volume_min, sizing.volume) if sizing.ok else spec.volume_min
    if 100.0 * vol * sizing.loss_per_lot / acc.equity > float(s.risk.get("max_risk_per_trade_percent", 0.35)):
        print("REFUS : le volume minimum dépasse le risque max autorisé.")
        return 7
    req = OrderRequest(symbol=sym, side=Side.BUY, volume=vol, sl=sl, tp=0.0, magic=s.magic, comment="TLAB:SMOKE")
    chk2 = b.order_check(req)
    print("order_check:", chk2.ok, chk2.retcode, chk2.comment)
    if not chk2.ok:
        return 8
    res = b.order_send(req)
    journal.event("smoke_order", request=req.to_dict(), result=res.to_dict())
    print("order_send:", res.ok, res.retcode, res.comment, "ticket", res.ticket)
    if not res.ok:
        return 9
    time.sleep(1.0)
    pos = b.position(res.ticket) or next((p for p in b.positions(magic=s.magic) if p.symbol == sym), None)
    if pos is None:
        print("ECHEC : position introuvable après fill")
        return 10
    print(f"position {pos.ticket} vol={pos.volume} entry={pos.price_open} sl={pos.sl}")
    if not pos.has_sl:
        fix = b.modify_position(pos.ticket, sl, 0.0)
        print("SL absent → correction:", fix.ok, fix.comment)
        pos = b.position(pos.ticket)
        if pos is None or not pos.has_sl:
            b.close_position(pos.ticket if pos else res.ticket, comment="SMOKE no-SL")
            print("ECHEC : SL impossible → position fermée")
            return 11
    new_sl = normalize_price(pos.sl + 0.1 * abs(pos.price_open - pos.sl), spec)
    mod = b.modify_position(pos.ticket, new_sl, 0.0)
    print("modify SL (resserrement):", mod.ok, mod.comment)
    close = b.close_position(pos.ticket, comment="SMOKE close")
    print("close:", close.ok, close.retcode, close.comment)
    time.sleep(1.0)
    remaining = [p for p in b.positions(magic=s.magic) if p.comment.startswith("TLAB:SMOKE")]
    if remaining:
        print("ECHEC : position test encore ouverte", [p.ticket for p in remaining])
        return 12
    print("OK : aucune position test restante. SMOKE TEST DEMO REUSSI")
    journal.event("smoke_ok", ticket=pos.ticket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
