"""Les plafonds de concentration doivent rester proportionnels au risque par trade.

Régression réelle du 2026-09-21. `risk_per_trade_percent` est passé de 0,25 % à 0,125 %
pour diversifier à budget constant, mais les plafonds de corrélation — exprimés en % de
l'equity — sont restés inchangés. Résultat : le nombre de positions corrélées autorisées a
**doublé** sans décision, de 2 à 4.

Conséquence mesurée sur le compte :

    06:05:05  ENTREE XAGEUR   agent B03
    06:15:06  ENTREE XAGUSD   agent C06
    06:15:07  ENTREE XAGAUD   agent C06
    ...
    06:21:05  SORTIE XAGAUD   -641,48
    06:21:06  SORTIE XAGUSD   -653,30
    07:23:09  SORTIE XAGEUR   -651,71        total -1 946 $

XAGUSD, XAGEUR et XAGAUD sont le **même métal** coté dans trois devises : un seul mouvement
de l'argent a donc été payé trois fois. Les croisés métaux ont été retirés de l'univers, et
ces tests verrouillent le rapport plafond / risque_par_trade pour que la dérive ne puisse
pas se reproduire silencieusement lors d'un prochain ajustement.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CFG = yaml.safe_load((ROOT / "config" / "risk.yaml").read_text(encoding="utf-8"))
MARKETS = yaml.safe_load((ROOT / "config" / "markets.yaml").read_text(encoding="utf-8"))
RISK = CFG["risk"]
CORR = CFG["correlation"]


def _positions_autorisees(plafond: float) -> float:
    return plafond / RISK["risk_per_trade_percent"]


# 2026-09-23, mode test demandé explicitement : `risk_per_trade_percent` est divisé (0,125 → 0,05 %) pour
# ouvrir PLUS de petites positions et accumuler plus vite les échantillons par agent (40 trades requis pour
# juger un agent). Les plafonds de concentration restent inchangés EN POURCENTAGE D'EQUITY : l'exposition
# maximale EN ARGENT sur un cluster, une devise ou une classe est donc strictement la même qu'avant — seul
# le nombre de tickets qui la compose augmente. C'est ce que ces tests verrouillent désormais, plutôt qu'un
# nombre de positions figé qui n'a de sens qu'à taille de lot constante.
EXPOSITION_MAX_HISTORIQUE = {"max_correlated_cluster_risk_percent": 0.25,
                             "max_currency_factor_risk_percent": 0.3,
                             "max_asset_class_risk_percent": 0.5}


#: 2026-09-24, profil « Modéré » choisi par l'utilisateur (0,25 % par trade) : les plafonds suivent le risque unitaire.
#: Référence : les valeurs historiques ci-dessus valaient pour 0,125 % par trade.
RISQUE_REFERENCE = 0.125


def test_exposition_en_argent_inchangee_malgre_les_lots_plus_petits():
    """Le garde-fou qui compte : le rapport plafond / risque par trade reste celui calibré le 2026-09-21. Quand le
    risque unitaire change (0,125 → 0,05 → 0,25 %), un plafond resté figé saturerait (ou autoriserait) un autre
    nombre de positions que prévu ; chaque changement de risque déplace donc les plafonds dans la même proportion,
    sauf en mode test à 0,05 % où l'exposition EN ARGENT est restée celle de 0,125 %."""
    rpt = RISK["risk_per_trade_percent"]
    for cle, plafond in EXPOSITION_MAX_HISTORIQUE.items():
        attendu = plafond if rpt < RISQUE_REFERENCE else plafond * rpt / RISQUE_REFERENCE
        assert CORR[cle] == pytest.approx(attendu), f"{cle} ne suit pas le risque par trade"


def test_hierarchie_des_plafonds_conservee():
    """cluster < devise < classe < budget total : un plafond qui dépasserait le suivant ne protégerait plus."""
    assert (CORR["max_correlated_cluster_risk_percent"] < CORR["max_currency_factor_risk_percent"]
            < CORR["max_asset_class_risk_percent"] < RISK["max_total_open_risk_percent"])
    assert CORR["max_asset_class_risk_percent"] <= RISK["max_total_open_risk_percent"] / 2


def test_les_emplacements_suivent_le_budget():
    """Le budget de risque ne doit pas permettre plus de positions qu'il n'y a d'emplacements, sinon le
    plafond de risque serait atteint sans pouvoir ouvrir (ou l'inverse)."""
    total = RISK["max_total_open_risk_percent"] / RISK["risk_per_trade_percent"]
    assert total <= RISK["max_open_positions"], f"{total:.0f} positions permises pour {RISK['max_open_positions']} emplacements"
    assert RISK["risk_per_trade_percent"] <= RISK["max_risk_per_trade_percent"]


def test_les_plafonds_restent_sous_le_budget_total():
    """Un plafond de concentration au-dessus du budget total ne protègerait plus de rien."""
    total = RISK["max_total_open_risk_percent"]
    for cle in ("max_correlated_cluster_risk_percent", "max_currency_factor_risk_percent",
                "max_asset_class_risk_percent"):
        assert CORR[cle] < total, f"{cle} doit rester strictement sous max_total_open_risk_percent"


def test_un_seul_couple_par_metal():
    """Les croisés or/argent sont le même sous-jacent : aucune diversification, que de la répétition."""
    metaux = MARKETS["markets"]["metals"]
    bases = [m[:3] for m in metaux]
    assert len(bases) == len(set(bases)), f"un métal apparaît plusieurs fois : {metaux}"
    for interdit in ("XAUEUR", "XAUJPY", "XAUAUD", "XAUCHF", "XAUGBP", "XAGEUR", "XAGAUD"):
        assert interdit not in metaux, f"{interdit} duplique un métal déjà présent"


def test_pas_de_metal_cote_dans_plusieurs_devises():
    """Le préfixe XAU/XAG/XPT/XPD identifie le métal : deux symboles de même préfixe = même pari.

    Ce contrôle ne vaut QUE pour les métaux. Ailleurs un préfixe commun ne signifie rien :
    CHINA50 et CHINAH sont deux indices chinois distincts (actions A et H), MidDE50 et
    MidDE60 deux indices allemands différents.
    """
    metaux = MARKETS["markets"]["metals"]
    prefixes = [m[:3] for m in metaux if m[:3] in ("XAU", "XAG", "XPT", "XPD")]
    doublons = {p for p in prefixes if prefixes.count(p) > 1}
    assert not doublons, f"métal coté dans plusieurs devises : {doublons} dans {metaux}"
