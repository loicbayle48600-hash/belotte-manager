"""Résolution des suffixes broker et classification par classe d'actif."""
from __future__ import annotations

import re
from typing import Iterable, Optional

_SUFFIX_RE = re.compile(r"^([A-Z0-9]+?)([._\-#]?[a-zA-Z]{0,6}|micro|mini|pro|ecn|\.[a-z]+)?$")


def root_of(symbol: str) -> str:
    """EURUSD.m -> EURUSD ; XAUUSDmicro -> XAUUSD ; GER40.cash -> GER40"""
    s = symbol.strip()
    for sep in (".", "_", "-", "#", "!"):
        if sep in s:
            s = s.split(sep)[0]
    for suf in ("micro", "mini", "pro", "ecn", "raw", "m", "i", "c", "r"):
        if s.lower().endswith(suf) and len(s) - len(suf) >= 6:
            s = s[: -len(suf)]
            break
    return s.upper()


def resolve_symbols(wanted: Iterable[str], available: Iterable[str]) -> dict[str, Optional[str]]:
    """Associe chaque racine voulue au symbole broker réel (ou None si absent)."""
    avail = list(available)
    by_root: dict[str, list[str]] = {}
    for a in avail:
        by_root.setdefault(root_of(a), []).append(a)
    out: dict[str, Optional[str]] = {}
    for w in wanted:
        r = root_of(w)
        cands = by_root.get(r, [])
        if not cands:
            out[w] = None
            continue
        # préférence : nom exact, puis le plus court (souvent le contrat standard)
        exact = [c for c in cands if c.upper() == r]
        out[w] = exact[0] if exact else sorted(cands, key=len)[0]
    return out


def asset_class_of(symbol: str, rules: dict[str, str] | None = None) -> str:
    r = root_of(symbol)
    rules = rules or {}
    for key, cls in rules.items():
        if key.upper() in r:
            return cls
    if r.startswith(("XAU", "XAG", "XPT", "XPD")):
        return "metals"
    if any(k in r for k in ("OIL", "XTI", "XBR", "BRENT", "WTI", "NGAS")):
        return "energies"
    if any(k in r for k in ("BTC", "ETH", "LTC", "XRP", "SOL", "ADA", "DOGE")):
        return "crypto"
    if any(k in r for k in ("US500", "US30", "NAS100", "GER40", "UK100", "JP225", "SPX", "DJI", "NDX", "DAX", "FTSE", "NIKKEI", "AUS200", "FRA40", "EU50", "HK50")):
        return "indices"
    if len(r) == 6 and r[:3].isalpha() and r[3:].isalpha():
        return "forex"
    return "other"


def currencies_of(symbol: str, spec_base: str = "", spec_profit: str = "") -> tuple[str, str]:
    """(devise base, devise cotation) ; pour un indice/métal, base = actif lui-même."""
    r = root_of(symbol)
    if spec_base and spec_profit:
        return spec_base.upper(), spec_profit.upper()
    if len(r) == 6 and r.isalpha():
        return r[:3], r[3:]
    if r.endswith("USD"):
        return r[:-3], "USD"
    return r, "USD"
