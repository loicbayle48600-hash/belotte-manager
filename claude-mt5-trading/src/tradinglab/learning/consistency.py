"""Statistiques par compte et règle de cohérence (25 %) calculées à partir de trades fermés (2026-09-23).

Sert au dashboard pour le compte maître (trades de `learning.db`) comme pour chaque compte suiveur (deals
d'historique exportés par son copieur dans `state/copy_status_<prefix>.json`). Tout est pur : aucun accès
broker, aucune donnée inventée — un compte sans trade fermé renvoie des séries vides et une part à 0 %.

Règle FOXX rappelée : « aucune idée de trade ne peut représenter plus de 25 % du profit total » ; les
positions du même sens sur le même instrument, réouvertes sous 10 minutes, forment UNE idée.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional


def _parse(ts) -> Optional[datetime]:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def trades_from_deals(deals: Iterable, magic: Optional[int] = None) -> list[dict]:
    """Regroupe les deals MT5 par position : {symbol, side, pnl (profit + commission + swap), volume,
    opened_at, closed_at, comment}. Seules les positions avec au moins un deal de sortie sont des trades."""
    par_pos: dict[int, dict] = {}
    for d in deals:
        if magic is not None and int(getattr(d, "magic", 0) or 0) != int(magic):
            continue
        pid = int(getattr(d, "position_id", 0) or 0)
        if not pid:
            continue
        p = par_pos.setdefault(pid, {"position_id": pid, "symbol": str(d.symbol), "side": None, "pnl": 0.0, "volume": 0.0,
                                     "opened_at": None, "closed_at": None, "comment": "", "out": False})
        entry = str(getattr(d, "entry", "") or "").upper()
        side = getattr(d, "side", None)
        side = getattr(side, "value", side)
        t = _parse(getattr(d, "time", None))
        if entry == "IN":
            p["side"] = str(side) if side else p["side"]
            p["volume"] += float(getattr(d, "volume", 0.0) or 0.0)
            if t and (p["opened_at"] is None or t < p["opened_at"]):
                p["opened_at"] = t
            if getattr(d, "comment", ""):
                p["comment"] = str(d.comment)
        else:
            p["out"] = True
            if p["side"] is None and side:
                p["side"] = "SELL" if str(side) == "BUY" else "BUY"       # le deal de sortie est du sens opposé
            if t and (p["closed_at"] is None or t > p["closed_at"]):
                p["closed_at"] = t
        p["pnl"] += float(getattr(d, "profit", 0.0) or 0.0) + float(getattr(d, "commission", 0.0) or 0.0) \
            + float(getattr(d, "swap", 0.0) or 0.0)
    out = []
    for p in par_pos.values():
        if not p["out"]:
            continue
        out.append({"position_id": p["position_id"], "symbol": p["symbol"], "side": p["side"] or "UNKNOWN",
                    "pnl": round(p["pnl"], 2), "volume": round(p["volume"], 4),
                    "opened_at": p["opened_at"].isoformat() if p["opened_at"] else None,
                    "closed_at": p["closed_at"].isoformat() if p["closed_at"] else None, "comment": p["comment"]})
    out.sort(key=lambda x: x["closed_at"] or "")
    return out


def account_statement(deals: Iterable, equity: Optional[float] = None, since: Optional[str] = None,
                      balance: Optional[float] = None) -> dict:
    """Relevé de compte RÉEL tiré de l'historique MT5 (2026-09-24, demande utilisateur : « les vrais stats de chaque
    compte ») — mêmes rubriques que l'onglet Historique de MT5 : bénéfice (profit des deals de trading), dépôts,
    retraits, swap, commission, solde. Les trades (tous, bot ET manuels) sont reconstitués par position."""
    deals = list(deals)
    t0 = _parse(since) if since else None
    if t0 is not None:
        # remise à zéro des statistiques (2026-09-25, comptes démo remis à 500 000 $) : seuls les deals postérieurs
        # comptent ; le solde affiché est alors celui du broker (`balance`), pas une reconstruction depuis l'origine
        deals = [d for d in deals if (_parse(getattr(d, "time", None)) or t0) >= t0]
    trade = [d for d in deals if str(getattr(d, "kind", "TRADE")) == "TRADE"]
    bal = [d for d in deals if str(getattr(d, "kind", "TRADE")) == "BALANCE"]
    autres = [d for d in deals if str(getattr(d, "kind", "TRADE")) in ("CREDIT", "OTHER")]
    profit = sum(float(d.profit or 0.0) for d in trade)
    commission = sum(float(d.commission or 0.0) for d in deals)
    swap = sum(float(d.swap or 0.0) for d in deals)
    depots = sum(float(d.profit) for d in bal if float(d.profit or 0.0) > 0)
    retraits = sum(float(d.profit) for d in bal if float(d.profit or 0.0) < 0)
    divers = sum(float(d.profit or 0.0) for d in autres)
    solde = depots + retraits + divers + profit + commission + swap
    out = {"profit": round(profit, 2), "deposits": round(depots, 2), "withdrawals": round(retraits, 2),
           "other": round(divers, 2), "swap": round(swap, 2), "commission": round(commission, 2),
           "net": round(profit + commission + swap, 2),
           "balance": round(float(balance), 2) if balance is not None else round(solde, 2),
           "since": since or None,
           "equity": round(float(equity), 2) if equity is not None else None,
           "trades": trades_from_deals(trade)}
    return out


def group_ideas(trades: Iterable[dict], window_minutes: float = 10.0) -> list[dict]:
    """Idées de trade au sens FOXX : même symbole, même sens, et réouverture dans les `window_minutes`
    suivant la dernière clôture de l'idée précédente."""
    ideas: list[dict] = []
    ouverte: dict[tuple, dict] = {}
    for t in sorted(trades, key=lambda x: (x.get("opened_at") or x.get("closed_at") or "")):
        key = (t.get("symbol"), t.get("side"))
        o, c = _parse(t.get("opened_at")), _parse(t.get("closed_at"))
        idea = ouverte.get(key)
        if idea is not None:
            fin = _parse(idea["closed_at"])
            if o is not None and fin is not None and (o - fin) > timedelta(minutes=window_minutes):
                idea = None
        if idea is None:
            idea = {"symbol": key[0], "side": key[1], "pnl": 0.0, "trades": 0,
                    "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at")}
            ideas.append(idea)
            ouverte[key] = idea
        idea["pnl"] += float(t.get("pnl") or 0.0)
        idea["trades"] += 1
        if c is not None and (_parse(idea["closed_at"]) is None or c > _parse(idea["closed_at"])):
            idea["closed_at"] = t.get("closed_at")
    for i in ideas:
        i["pnl"] = round(i["pnl"], 2)
    return ideas


def consistency_from_trades(trades: Iterable[dict], max_share_percent: float = 25.0,
                            window_minutes: float = 10.0) -> dict:
    """Part du profit total (net) détenue par chaque idée ; `share_percent` = la plus grande.
    Profit net ≤ 0 → aucune part calculable (0 %, `within_limit` vrai, détail explicite)."""
    ideas = group_ideas(trades, window_minutes)
    total = round(sum(i["pnl"] for i in ideas), 2)
    rows = []
    for i in sorted(ideas, key=lambda x: x["pnl"], reverse=True):
        share = (100.0 * i["pnl"] / total) if (total > 0 and i["pnl"] > 0) else 0.0
        rows.append({**i, "share_percent": round(share, 2)})
    best = max((r["share_percent"] for r in rows), default=0.0)
    return {"share_percent": round(best, 2), "max_share_percent": float(max_share_percent), "within_limit": best <= max_share_percent,
            "total_profit": total, "ideas": rows[:30], "n_ideas": len(rows),
            "detail": ("aucun profit net : rien à contrôler" if total <= 0 else
                       f"meilleure idée = {best:.1f} % du profit total ({total:,.0f} $)")}


def account_stats(trades: list[dict], day_key, max_share_percent: float = 25.0, window_minutes: float = 10.0) -> dict:
    """Cartes + séries jour/semaine/mois/année + par paire + cohérence, pour un compte. `day_key(datetime) -> 'YYYY-MM-DD'`
    = journée de trading (reset 17:00 New York), la même définition que le compte maître."""
    def bucket(items: dict, key: str, t: dict) -> None:
        b = items.setdefault(key, {"pnl": 0.0, "n": 0, "wins": 0})
        b["pnl"] += t["pnl"]; b["n"] += 1; b["wins"] += 1 if t["pnl"] > 0 else 0
    days: dict = {}; weeks: dict = {}; months: dict = {}; years: dict = {}; symbols: dict = {}
    dated = []
    for t in trades:
        c = _parse(t.get("closed_at"))
        if c is None:
            continue
        try:
            dk = day_key(c)
        except Exception:  # noqa: BLE001 - calendrier dégradé : jour UTC
            dk = c.date().isoformat()
        dated.append(t)
        d = date.fromisoformat(dk); iso = d.isocalendar()
        bucket(days, dk, t); bucket(weeks, f"{iso[0]}-S{iso[1]:02d}", t); bucket(months, dk[:7], t); bucket(years, dk[:4], t)
        bucket(symbols, str(t.get("symbol") or "?"), t)
    def series(items: dict, limit: int) -> list[dict]:
        return [{"key": k, "pnl": round(v["pnl"], 2), "n": v["n"],
                 "win_rate": round(100.0 * v["wins"] / v["n"], 1) if v["n"] else 0.0}
                for k, v in sorted(items.items())][-limit:]
    n = len(dated); wins = sum(1 for t in dated if t["pnl"] > 0)
    return {"total": {"trades": n, "wins": wins, "win_rate": round(100.0 * wins / n, 1) if n else 0.0,
                      "pnl": round(sum(t["pnl"] for t in dated), 2),
                      "best": round(max((t["pnl"] for t in dated), default=0.0), 2),
                      "worst": round(min((t["pnl"] for t in dated), default=0.0), 2)},
            "daily": series(days, 60), "weekly": series(weeks, 26), "monthly": series(months, 24), "yearly": series(years, 10),
            "symbols": [{"symbol": k, "pnl": round(v["pnl"], 2), "n": v["n"],
                         "win_rate": round(100.0 * v["wins"] / v["n"], 1) if v["n"] else 0.0}
                        for k, v in sorted(symbols.items(), key=lambda kv: kv[1]["pnl"], reverse=True)],
            "consistency": consistency_from_trades(dated, max_share_percent, window_minutes)}
