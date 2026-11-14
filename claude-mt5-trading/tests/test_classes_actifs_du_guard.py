"""Le Correlation Guard doit recevoir les vraies règles de classe d'actif.

Constaté le 2026-09-21. L'orchestrateur construisait le guard avec
`settings.markets.get("asset_class_rules", {})`. Or `settings.markets` n'expose que les listes de
symboles : cet accès renvoie **toujours {}**, alors que `settings.section("asset_class_rules")` en
renvoie 50. Le guard tournait donc sans aucune règle depuis le début.

Le repli codé en dur dans `asset_class_of` rattrape la majorité des cas, ce qui rendait le défaut
invisible. Mais 17 symboles sur 109 restaient mal classés :

    STOXX50 F40 CHINA50 ES35 IT40 US2000 CA60 SE30 SWI20 SA40 NOR25
    TecDE30 MidDE60 MidDE50   -> « other »   au lieu de « indices »
    NETH25                    -> « crypto »  (le motif « ETH » du repli crypto)
    CHINAH                    -> « forex »   (six lettres alphabétiques)
    XNGUSD                    -> « forex »   au lieu de « energies »

Conséquence concrète : seuls 8 des 24 indices comptaient dans le plafond « indices ». Quatre indices
européens fortement corrélés (STOXX50, F40, ES35, IT40) pouvaient donc s'empiler sans que
`max_asset_class_risk_percent` ne s'applique, puisqu'ils tombaient tous dans un fourre-tout « other »
partagé avec des instruments sans rapport.

Ces tests ne verrouillent aucun seuil : seulement le fait que les règles arrivent bien jusqu'au guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.mt5.symbols import asset_class_of  # noqa: E402

#: échantillon des symboles que le repli seul classe mal (symbole -> classe attendue)
MAL_CLASSES_SANS_REGLES = {
    "STOXX50": "indices", "F40": "indices", "CHINA50": "indices", "ES35": "indices",
    "IT40": "indices", "MidDE50": "indices", "NETH25": "indices", "CHINAH": "indices",
    "XNGUSD": "energies",
}


def test_les_regles_sont_lisibles_par_section(settings):
    """`markets.get` renvoie {} : c'est la cause du défaut, on la garde sous test."""
    assert settings.markets.get("asset_class_rules", {}) == {}, \
        "si cet accès se met à fonctionner, la raison d'être de `section()` a changé"
    assert len(settings.section("asset_class_rules")) >= 40


def test_le_guard_recoit_les_regles(settings):
    """Cœur du correctif : le guard construit par l'orchestrateur ne doit pas avoir de règles vides."""
    from tradinglab.mt5.mock_adapter import make_broker
    from tradinglab.orchestration.orchestrator import Orchestrator

    o = Orchestrator(settings, make_broker("mock", settings), "SAFE")
    assert o.corr.asset_rules, "le Correlation Guard tournait sans aucune règle de classe d'actif"
    assert len(o.corr.asset_rules) >= 40


@pytest.mark.parametrize("symbole,attendu", sorted(MAL_CLASSES_SANS_REGLES.items()))
def test_symboles_exotiques_bien_classes(settings, symbole, attendu):
    regles = settings.section("asset_class_rules")
    assert asset_class_of(symbole, regles) == attendu


def test_neth25_n_est_pas_une_crypto(settings):
    """Piège du repli : « NETH25 » contient « ETH ». Un indice n'est pas une crypto."""
    assert asset_class_of("NETH25", {}) == "crypto", "le repli seul se trompe bien ici"
    assert asset_class_of("NETH25", settings.section("asset_class_rules")) == "indices"


def test_les_indices_sont_tous_dans_la_classe_indices(settings):
    """Un indice rangé ailleurs échappe au plafond de sa propre classe."""
    regles = settings.section("asset_class_rules")
    mauvais = [s for s in settings.markets.get("indices", []) if asset_class_of(s, regles) != "indices"]
    assert not mauvais, f"indices mal classés : {mauvais}"


def test_aucun_symbole_de_l_univers_ne_tombe_dans_other(settings):
    """« other » est un fourre-tout : deux instruments sans rapport y partagent le même plafond."""
    regles = settings.section("asset_class_rules")
    orphelins = []
    for groupe, syms in settings.markets.items():
        if groupe == "asset_class_rules" or not isinstance(syms, list):
            continue
        orphelins += [s for s in syms if asset_class_of(s, regles) == "other"]
    assert not orphelins, f"symboles sans classe d'actif : {orphelins}"
