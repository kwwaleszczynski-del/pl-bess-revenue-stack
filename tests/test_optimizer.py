from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from bess_stack.backtest import build_days
from bess_stack.optimizer import DURATION_REQUIREMENT, STRATEGIES, BatteryParams, optimize_day
from bess_stack.pse_api import DOWN, PRODUCTS, UP

from conftest import synthetic_capacity, synthetic_energy

TOL = 1e-6


@pytest.fixture(scope="module")
def days(week):
    energy, price, _ = week
    return build_days(energy, price)


def solve(day, params, strategy, **kw):
    return optimize_day(day.prices, day.hour_of, day.reserve_prices, params, STRATEGIES[strategy], **kw)


def reserve_energy(res, day, params, direction):
    """Energy each interval must hold for the contracted reserves of its hour (MWh)."""
    r = res.reserves.to_numpy()[day.hour_of]
    tau = np.array([DURATION_REQUIREMENT[k] for k in PRODUCTS])
    if direction == "up":
        mask = np.array([k in UP for k in PRODUCTS])
        return (r[:, mask] * tau[mask]).sum(axis=1) / params.eta
    mask = np.array([k in DOWN for k in PRODUCTS])
    return (r[:, mask] * tau[mask]).sum(axis=1) * params.eta


@pytest.mark.parametrize("strategy", ["arbitrage", "reserves", "stacked"])
@pytest.mark.parametrize("energy", [1.0, 2.0, 4.0])
def test_soc_balance_and_bounds(days, strategy, energy):
    params = BatteryParams(energy_mwh=energy)
    day = days[1]
    res = solve(day, params, strategy)
    s = res.schedule
    soc0 = params.energy_mwh * params.soc_start
    prev = np.concatenate([[soc0], s["soc_mwh"].to_numpy()[:-1]])
    expected = prev + params.eta * 0.25 * s["charge_mw"] - 0.25 * s["discharge_mw"] / params.eta
    assert np.allclose(s["soc_mwh"], expected, atol=1e-6)
    assert s["soc_mwh"].min() >= params.energy_mwh * params.soc_min - TOL
    assert s["soc_mwh"].max() <= params.energy_mwh * params.soc_max + TOL
    assert s["soc_mwh"].iloc[-1] == pytest.approx(soc0, abs=1e-6)
    assert ((s["charge_mw"] > TOL) & (s["discharge_mw"] > TOL)).sum() == 0
    assert res.discharged_mwh <= params.max_cycles_per_day * params.usable_mwh + TOL


@pytest.mark.parametrize("energy", [1.0, 2.0, 4.0])
def test_soc_reserve_is_respected(days, energy):
    params = BatteryParams(energy_mwh=energy)
    for day in days[:3]:
        res = solve(day, params, "stacked")
        soc = res.schedule["soc_mwh"].to_numpy()
        soc_start = np.concatenate([[params.energy_mwh * params.soc_start], soc[:-1]])
        e_up, e_dn = reserve_energy(res, day, params, "up"), reserve_energy(res, day, params, "down")
        e_min, e_max = params.energy_mwh * params.soc_min, params.energy_mwh * params.soc_max
        for level in (soc, soc_start):            # at the end and at the start of every quarter-hour
            assert (level - e_up >= e_min - 1e-6).all()
            assert (level + e_dn <= e_max + 1e-6).all()
        assert res.reserves.to_numpy().sum() > 0   # the test is not vacuous


def test_power_headroom_is_respected(days):
    params = BatteryParams(energy_mwh=2.0)
    for day in days[:3]:
        res = solve(day, params, "stacked")
        r = res.reserves.to_numpy()[day.hour_of]
        up = r[:, [PRODUCTS.index(k) for k in UP]].sum(axis=1)
        dn = r[:, [PRODUCTS.index(k) for k in DOWN]].sum(axis=1)
        net = res.schedule["discharge_mw"].to_numpy() - res.schedule["charge_mw"].to_numpy()
        assert (net + up <= params.power_mw + 1e-6).all()
        assert (-net + dn <= params.power_mw + 1e-6).all()


@pytest.mark.parametrize("energy", [1.0, 2.0, 4.0])
def test_stacking_is_not_worse_than_single_strategies(days, energy):
    params = BatteryParams(energy_mwh=energy)
    for day in days:
        stacked = solve(day, params, "stacked").total
        for single in ("arbitrage", "reserves"):
            other = solve(day, params, single).total
            assert stacked >= other - 1e-4 * max(1.0, abs(other))


def test_revenue_accounting(days):
    res = solve(days[0], BatteryParams(), "stacked")
    assert res.total == pytest.approx(res.arbitrage - res.degradation + sum(res.reserve_revenue.values()))
    p = days[0].reserve_prices
    manual = sum(float(np.nansum(p[:, j] * res.reserves[k].to_numpy())) for j, k in enumerate(PRODUCTS))
    assert sum(res.reserve_revenue.values()) == pytest.approx(manual)


def test_strategies_use_only_their_markets(days):
    params = BatteryParams()
    arb = solve(days[0], params, "arbitrage")
    assert arb.reserves.to_numpy().sum() == 0
    res = solve(days[0], params, "reserves")
    assert res.schedule[["charge_mw", "discharge_mw"]].to_numpy().sum() == 0
    assert np.allclose(res.schedule["soc_mwh"], params.energy_mwh * params.soc_start)


def test_products_without_purchase_are_not_sold(days):
    day = days[0]
    pi = day.reserve_prices.copy()
    j = PRODUCTS.index("afrr_up")
    pi[:, j] = np.nan
    res = optimize_day(day.prices, day.hour_of, pi, BatteryParams(), STRATEGIES["stacked"])
    assert res.reserves["afrr_up"].sum() == 0 and res.reserve_revenue["afrr_up"] == 0


def test_reserve_limit_caps_sold_mw(days):
    day = days[0]
    limit = np.full_like(day.reserve_prices, 0.1)
    res = optimize_day(day.prices, day.hour_of, day.reserve_prices, BatteryParams(), STRATEGIES["reserves"],
                       reserve_limit=limit)
    assert res.reserves.to_numpy().max() <= 0.1 + TOL


def test_longer_duration_requirement_cannot_earn_more(days):
    day = days[0]
    params = BatteryParams(energy_mwh=1.0)
    short = solve(day, params, "reserves", duration_req={k: 0.25 for k in PRODUCTS}).total
    long = solve(day, params, "reserves", duration_req={k: 1.0 for k in PRODUCTS}).total
    assert short >= long - 1e-6


@pytest.mark.parametrize("start,n", [(date(2025, 3, 30), 92), (date(2025, 10, 26), 100)])
def test_daylight_saving_days(start, n):
    end = date(start.year, start.month, start.day + 1)
    energy = synthetic_energy(start, end)
    price, _ = synthetic_capacity(start, end)
    (day,) = build_days(energy, price)
    assert len(day.prices) == n and len(day.hours) == n // 4
    res = solve(day, BatteryParams(), "stacked")
    assert len(res.schedule) == n and len(res.reserves) == n // 4
