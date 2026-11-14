"""Correspondance des symboles entre le compte maître et un compte suiveur (2026-09-23).

Chaque broker nomme les mêmes instruments différemment : l'indice S&P 500 s'appelle ``US500`` chez IC
Markets, ``SPX500`` chez Blue Guardian, ``[SP500]`` chez Admirals ; le gaz naturel ``XNGUSD`` ici,
``NGAS`` ailleurs. Sans traduction, le copieur ne répliquait tout simplement pas ces positions.

Trois niveaux, du plus sûr au plus permissif, dans cet ordre :
1. **nom identique** chez le suiveur ;
2. **même racine** après retrait des décorations de broker (``EURUSD.m``, ``EURUSD_SB``, ``#EURUSD``…) ;
3. **alias d'instrument** : tables ci-dessous, écrites à la main pour des sous-jacents dont l'équivalence
   est certaine. Aucune inférence floue : un symbole non résolu n'est PAS copié (et c'est journalisé),
   plutôt que de risquer d'ouvrir une position sur le mauvais instrument.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

#: décorations ajoutées par les brokers autour de la racine réelle
_DECOR = re.compile(r"^[#\[\.\-_]+|[#\]\.\-_]*(?:m|r|raw|pro|ecn|std|c|sb|cash|spot|\.?fx)?$", re.I)


def normalize(symbol: str) -> str:
    """Racine comparable : majuscules, sans préfixe/suffixe de broker ni séparateur."""
    s = str(symbol or "").upper().strip()
    s = re.sub(r"[^A-Z0-9]", "", s)                       # #EURUSD, [SP500], EUR/USD → EURUSD, SP500
    for suf in ("MICRO", "CASH", "SPOT", "ECN", "RAW", "PRO", "STD", "SB", "FX"):
        if len(s) > len(suf) + 2 and s.endswith(suf):
            s = s[: -len(suf)]
    if len(s) > 6 and s[-1] in "MRC" and s[:-1].isalpha():  # EURUSDm, EURUSDc
        s = s[:-1]
    return s


#: familles d'équivalence : tous les noms d'une ligne désignent le MÊME sous-jacent.
#: N'y ajouter que des équivalences certaines — en cas de doute, ne pas copier vaut mieux que se tromper.
FAMILIES: tuple[tuple[str, ...], ...] = (
    # indices
    ("US500", "SPX500", "SP500", "USA500", "SPX", "US500CASH", "SP500CASH", "ES"),
    ("US30", "DJ30", "DJI30", "DOW", "USA30", "WS30", "US30CASH"),
    ("NAS100", "USTEC", "NDX100", "NQ100", "US100", "NAS", "USATECH", "TECH100"),
    ("GER40", "DE40", "DAX40", "DAX", "GER30", "DE30"),
    ("UK100", "FTSE100", "FTSE", "GB100"),
    ("FRA40", "F40", "CAC40", "CAC"),
    ("JP225", "JPN225", "NIKKEI", "NIKKEI225", "N225"),
    ("AUS200", "AU200", "ASX200"),
    ("EU50", "STOXX50", "ESTX50", "EUSTX50"),
    ("NETH25", "NL25", "AEX", "NED25"),
    ("SE30", "SWI30", "SMI", "CH20", "SWI20"),
    # métaux
    ("XAUUSD", "GOLD", "GOLDUSD", "XAU"),
    ("XAGUSD", "SILVER", "SILVERUSD", "XAG"),
    ("XPTUSD", "PLATINUM", "XPT"),
    ("XPDUSD", "PALLADIUM", "XPD"),
    # énergies
    ("XTIUSD", "USOIL", "WTI", "CRUDOIL", "OILUSD", "USCRUDE", "CL"),
    ("XBRUSD", "UKOIL", "BRENT", "BRENTOIL", "UKBRENT"),
    ("XNGUSD", "NGAS", "NATGAS", "NATURALGAS", "GAS", "NG"),
    # crypto (noms courts fréquents)
    ("BTCUSD", "BITCOIN", "BTCUSDT", "XBTUSD"),
    ("ETHUSD", "ETHEREUM", "ETHUSDT"),
    ("XRPUSD", "RIPPLE", "XRPUSDT"),
    ("LTCUSD", "LITECOIN", "LTCUSDT"),
)

_ALIAS: dict[str, int] = {}
for _i, _famille in enumerate(FAMILIES):
    for _nom in _famille:
        _ALIAS[_nom] = _i


def is_single_stock(symbol: str) -> bool:
    """Admirals (et d'autres) préfixent les actions par « # » : #DOW = action Dow Inc., PAS l'indice Dow Jones
    (constaté le 2026-09-24 : US30 traduit en #DOW, ouverture refusée « Invalid stops » par chance). Une action ne
    correspond jamais à un instrument du maître par racine ou par alias, seulement par nom identique."""
    return str(symbol or "").strip().startswith("#")


def family_of(symbol: str) -> Optional[int]:
    if is_single_stock(symbol):
        return None
    return _ALIAS.get(normalize(symbol))


def build_map(master_symbols: Iterable[str], follower_symbols: Iterable[str]) -> dict[str, str]:
    """Table {symbole maître: symbole suiveur} pour les instruments réellement disponibles chez le suiveur."""
    suiveur = list(follower_symbols)
    par_nom = {s: s for s in suiveur}
    par_racine: dict[str, str] = {}
    par_famille: dict[int, str] = {}
    for s in suiveur:
        if is_single_stock(s):
            continue                       # une action ne sert jamais de cible par racine ni par alias
        par_racine.setdefault(normalize(s), s)
        f = family_of(s)
        if f is not None:
            par_famille.setdefault(f, s)
    out: dict[str, str] = {}
    for m in master_symbols:
        cible = par_nom.get(m) or par_racine.get(normalize(m))
        if cible is None:
            f = family_of(m)
            if f is not None:
                cible = par_famille.get(f)
        if cible is not None:
            out[m] = cible
    return out
