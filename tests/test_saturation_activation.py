from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bess_stack import activation, saturation
from bess_stack.backtest import RunConfig, build_days
from bess_stack.optimizer import BatteryParams
from bess_stack.pse_api import PRODUCTS

from conftest import synthetic_offers


@pytest.fixture(scope="module")
def two_days(week):
    energy, price, volume = week
    keep = sorted(set(price["business_date"]))[:2]
    sel_p = price["business_date"].isin(keep)
    sel_e = energy["business_date"].isin(keep)
    p, v, e = price[sel_p], volume[volume["business_date"].isin(keep)], energy[sel_e]
    return e, p, v, synthetic_offers(p, v)


def test_offer_stacks_reproduce_clearing_prices(two_days):
    _, price, volume, offers = two_days
    st = saturation.build_stacks(offers, volume, price)
    val = saturation.validate(st)
    assert (val["match_share"] == 1.0).all()


def test_clearing_with_battery_block(two_days):
    _, price, volume, offers = two_days
    st = saturation.build_stacks(offers, volume, price)
    p0, acc0 = saturation.clear(st, "fcr_up", 0.0)
    assert np.allclose(p0, price.loc[st.hours, "fcr_up"].to_numpy())
    assert (acc0 == 0).all()
    small, acc = saturation.clear(st, "fcr_up", 10.0)
    assert (small <= p0 + 1e-9).all() and np.allclose(acc, 10.0)
    full, acc = saturation.clear(st, "fcr_up", 500.0, bid=3.0)     # more than the 197 MW bought
    assert np.allclose(full, 3.0) and np.allclose(acc, 197.0)


def test_product_curves_fall_with_battery_volume(two_days):
    _, price, volume, offers = two_days
    st = saturation.build_stacks(offers, volume, price)
    curves = saturation.product_curves(st, [0, 50, 100, 200, 400])
    for k in PRODUCTS:
        mp = curves.loc[k, "mean_price"].to_numpy()
        assert (np.diff(mp) <= 1e-9).all()
    assert curves.loc[("fcr_up", 400.0), "accepted_share"] == pytest.approx(197 / 400)
    room = saturation.market_room(st, curves)
    assert room.loc["fcr_up", "mean_mw"] == 197


def test_fleet_equilibrium_lowers_reserve_revenue(two_days):
    energy, price, volume, offers = two_days
    st = saturation.build_stacks(offers, volume, price)
    days = build_days(energy, price)
    cfg = RunConfig("stacked_2h", BatteryParams(energy_mwh=2), "stacked")
    fleet, conv = saturation.fleet_equilibrium(days, st, cfg, [0, 3000], iterations=3, workers=1)
    assert fleet.loc[3000, "reserves"] < fleet.loc[0, "reserves"]
    assert fleet.loc[3000, "total"] <= fleet.loc[0, "total"] + 1e-6
    assert len(conv) == 3


def test_activation_ratio_and_margin(week):
    energy, price, volume = week
    q = energy.index[:4]
    afrr = pd.DataFrame({"afrr_up_mwh": [10.0, 0.0, 200.0, 5.0], "afrr_down_mwh": [-25.0, 0.0, 0.0, -50.0],
                         "business_date": energy["business_date"].iloc[:4].to_numpy()}, index=q)
    sup = pd.DataFrame({"afrr_up": [0.0, 0.0, 0.0, 0.0], "afrr_down": [np.nan] * 4}, index=q)
    ratio = activation.activation_ratio(afrr, volume, sup)
    # 400 MW bought each way: alpha = MWh / (0.25 h x 400 MW)
    assert ratio["alpha_up"].tolist() == pytest.approx([0.1, 0.0, 1.0, 0.05])
    assert ratio["alpha_down"].tolist() == pytest.approx([0.25, 0.0, 0.0, 0.5])

    days = build_days(energy, price)[:1]
    d = days[0]
    r = np.zeros((len(d.hours), len(PRODUCTS)))
    r[:, PRODUCTS.index("afrr_up")] = 1.0
    r[:, PRODUCTS.index("afrr_down")] = 0.5
    prices = pd.DataFrame({"afrr_up_price": 800.0, "afrr_down_price": 100.0}, index=q)
    params = BatteryParams(round_trip_efficiency=0.81, degradation_cost=10.0)
    out = activation.activation_revenue({d.business_date: r}, days, ratio, prices, energy, params).iloc[0]
    p_da = energy["price"].iloc[:4].to_numpy()
    e_up = np.array([0.1, 0.0, 1.0, 0.05]) * 1.0 * 0.25
    e_dn = np.array([0.25, 0.0, 0.0, 0.5]) * 0.5 * 0.25
    assert out["energy_up_mwh"] == pytest.approx(e_up.sum())
    assert out["margin_up"] == pytest.approx((e_up * (800 - p_da / 0.81 - 10)).sum())
    assert out["margin_down"] == pytest.approx((e_dn * (p_da * 0.81 - 100 - 10 * 0.81)).sum())
