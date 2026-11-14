"""Commission crypto en % de la valeur du trade (2026-09-26).

Par lot (3 $), la commission valait 150 % (SOL) à 16 700 % (XRP) du risque : le contrôle 07b bloquait toute crypto
sauf le BTC, et le break-even devenait impossible à placer. En % de la valeur, le coût relatif est le même partout."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tradinglab.risk.prop_guard import PropGuard, PropProfile


def _guard():
    prof = PropProfile.from_config({"commissions_per_lot": {"forex": 7.0, "commodities": 7.0, "indices": 0.0, "crypto": 3.0},
                                    "crypto_commission_percent_of_notional": 0.004})
    return PropGuard(prof, True, False, 1.0)


def test_crypto_en_pourcentage_quel_que_soit_le_lot():
    g = _guard()
    xrp = SimpleNamespace(asset_class="crypto", tick_size=0.00001, tick_value=0.00001)    # 1 lot = 1 XRP
    btc = SimpleNamespace(asset_class="crypto", tick_size=0.01, tick_value=0.01)          # 1 lot = 1 BTC
    assert g.commission_price(xrp, 2.9) == pytest.approx(2.9 * 0.004 / 100)               # et non 3 $ de prix
    assert g.commission_price(btc, 84000) == pytest.approx(3.36)                           # ≈ 3 $ par lot BTC
    stop_xrp = 0.03                                                                        # stop typique (~1 %)
    assert g.commission_price(xrp, 2.9) / stop_xrp < 0.01                                  # < 1 % du risque


def test_forex_inchange():
    g = _guard()
    eur = SimpleNamespace(asset_class="forex", tick_size=0.00001, tick_value=1.0)
    assert g.commission_price(eur, 1.1) == pytest.approx(7.0 * 0.00001)
