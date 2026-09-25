"""Plafond de spread par classe d'actif, et classification des symboles IC Markets.

Contexte : la chaîne de `if` d'origine ne connaissait que `metals` et `indices`. La crypto
retombait donc sur `default: 40` alors que BTCUSD cote ~500 points, ce qui refusait
silencieusement tout candidat crypto au contrôle `07_spread`.

Ces tests verrouillent deux choses :
- chaque classe d'actif réellement produite par `asset_class_of` a un plafond explicite
  ou retombe sur `default` de façon assumée ;
- les symboles d'indices d'IC Markets (USTEC, DE40) sont classés `indices`, y compris
  sans règles de configuration.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.execution.gate import SPREAD_KEY_BY_ASSET_CLASS, max_spread_points_for  # noqa: E402
from tradinglab.mt5.symbols import asset_class_of  # noqa: E402

CFG = {"default": 40, "XAU": 60, "indices": 100, "crypto": 6000}


def test_plafond_par_classe():
    assert max_spread_points_for("crypto", CFG) == 6000      # BTCUSD jusqu'a 3874 pts le week-end
    assert max_spread_points_for("metals", CFG) == 60
    assert max_spread_points_for("indices", CFG) == 100
    assert max_spread_points_for("forex", CFG) == 40
    assert max_spread_points_for("energies", CFG) == 40      # pas de clé dédiée : défaut assumé
    assert max_spread_points_for("other", CFG) == 40
    assert max_spread_points_for("", CFG) == 40


def test_classe_inconnue_ou_config_vide_retombe_sur_le_defaut():
    assert max_spread_points_for("licorne", CFG) == 40
    assert max_spread_points_for("crypto", {}) == 40         # pas de config : défaut du code
    assert max_spread_points_for("crypto", {"default": 25}) == 25


def test_casse_indifferente():
    assert max_spread_points_for("CRYPTO", CFG) == 6000
    assert max_spread_points_for("Metals", CFG) == 60


def test_toute_classe_mappee_est_une_cle_connue():
    """Garde-fou : une classe ajoutée au mapping doit correspondre à une clé documentée."""
    assert set(SPREAD_KEY_BY_ASSET_CLASS) <= {"metals", "indices", "crypto", "energies"}
    for key in SPREAD_KEY_BY_ASSET_CLASS.values():
        assert isinstance(key, str) and key


def test_symboles_ic_markets_bien_classes():
    """USTEC et DE40 remplacent NAS100 et GER40 : sans ce classement ils passaient en `other`."""
    for sym in ("USTEC", "DE40", "US500", "US30", "UK100", "JP225"):
        assert asset_class_of(sym) == "indices", sym
    assert asset_class_of("BTCUSD") == "crypto"
    assert asset_class_of("ETHUSD") == "crypto"
    assert asset_class_of("XTIUSD") == "energies"
    assert asset_class_of("XBRUSD") == "energies"
    assert asset_class_of("XAUUSD") == "metals"
    assert asset_class_of("EURUSD") == "forex"


def test_un_indice_classe_other_prendrait_le_plafond_forex():
    """Explicite la conséquence du bug corrigé : `other` n'a droit qu'à 40 points."""
    assert max_spread_points_for("other", CFG) == max_spread_points_for("forex", CFG) == 40


def test_config_du_projet_couvre_la_crypto():
    """La valeur vit dans system.yaml : ce test casse si on la retire par inadvertance."""
    import yaml

    cfg = yaml.safe_load((ROOT / "config" / "system.yaml").read_text(encoding="utf-8"))
    spreads = cfg["execution"]["max_spread_points"]
    assert spreads["crypto"] >= 4000, "BTCUSD monte à 3874 points le week-end : en dessous il est bloqué"
    assert spreads["default"] == 40, "le défaut forex ne doit pas être relâché"
    assert cfg["execution"]["max_spread_atr_ratio"] == 0.15, "le garde-fou ATR reste inchangé"
