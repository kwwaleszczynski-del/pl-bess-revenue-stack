from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bess_stack import capacity_market, market
from bess_stack.backtest import RunConfig, annualise, build_days, period_summary, run_configs
from bess_stack.optimizer import BatteryParams
from bess_stack.pse_api import PRODUCTS

from conftest import synthetic_offers


def test_build_days_skips_incomplete_days(week):
    energy, price, _ = week
    first_day = energy["business_date"].iloc[0]
    broken = energy.drop(energy.index[energy["business_date"] == first_day][:3])
    days = build_days(broken, price)
    assert len(days) == 6 and days[0].business_date != first_day
    no_cap = price[price["business_date"] != days[-1].business_date]
    assert len(build_days(energy, no_cap)) == 6


def test_run_configs_and_annualise(week):
    energy, price, _ = week
    days = build_days(energy, price)[:2]
    cfgs = [RunConfig("arbitrage_1h", BatteryParams(energy_mwh=1), "arbitrage"),
            RunConfig("stacked_1h", BatteryParams(energy_mwh=1), "stacked")]
    daily = run_configs(days, cfgs, workers=1)
    assert len(daily) == 4 and set(daily["config"]) == {"arbitrage_1h", "stacked_1h"}
    ann = annualise(daily)
    row = ann.loc[("stacked_1h", 2025)]
    assert row["days"] == 2
    assert row["total_per_year"] == pytest.approx(daily[daily["config"] == "stacked_1h"]["total"].sum() / 2 * 365)
    summ = period_summary(daily)
    assert summ.loc["stacked_1h", "reserves"] == pytest.approx(
        summ.loc["stacked_1h", [f"rev_{k}" for k in PRODUCTS]].sum())
    assert summ.loc["stacked_1h", "total"] >= summ.loc["arbitrage_1h", "total"]


def test_monthly_prices_and_hour_profile(week):
    _, price, _ = week
    mp = market.monthly_prices(price)
    assert list(mp.index) == ["2025-07"]
    assert mp.loc["2025-07", "fcr_up"] == pytest.approx(price["fcr_up"].mean())
    prof = market.hour_profile(price)
    assert list(prof.index) == list(range(24))
    assert prof["afrr_up"].idxmax() in (18, 19, 20)        # evening peak of the synthetic prices


def test_annual_market_cost(week):
    _, price, volume = week
    ann = market.annual_market(price, volume)
    row = ann.loc[(2025, "fcr_up")]
    assert row["cost_mpln"] == pytest.approx((price["fcr_up"] * volume["fcr_up"]).sum() / 1e6)
    assert row["mean_mw"] == 197


def test_depth_cover_ratio(week):
    _, price, volume = week
    day = price["business_date"].iloc[0]
    p1, v1 = price[price["business_date"] == day], volume[volume["business_date"] == day]
    offers = synthetic_offers(p1, v1)
    sup = pd.DataFrame(columns=PRODUCTS + ["business_date"])
    depth = market.monthly_depth(v1, sup, offers)
    assert depth.loc["2025-07", ("cover", "fcr_up")] == pytest.approx(2.0)
    assert depth.loc["2025-07", ("bought", "mfrr_down")] == 90


def test_depth_summary_over_a_period(week):
    _, price, volume = week
    days = sorted(set(price["business_date"]))
    offers = synthetic_offers(price[price["business_date"] == days[0]], volume[volume["business_date"] == days[0]])
    q = pd.date_range(volume.index[0], periods=96, freq="15min", name="ts_utc")
    sup = pd.DataFrame({k: np.nan for k in PRODUCTS if k != "rr_up"}, index=q)   # NaN: nothing bought
    sup.iloc[:48, sup.columns.get_loc("afrr_up")] = 20.0
    sup["business_date"] = days[0]
    out = market.depth_summary(volume, sup, offers, days[0], days[0])
    assert out.loc["afrr_up", "bought_mw"] == 400 and out.loc["afrr_up", "supplementary_mw"] == 10
    assert out.loc["fcr_up", "supplementary_mw"] == 0
    assert out.loc["afrr_up", "cover"] == pytest.approx(2.0)


def test_correlation_detects_shared_driver():
    idx = pd.date_range("2025-01-01", periods=24 * 60, freq="h", tz="UTC", name="ts_utc")
    rng = np.random.default_rng(0)
    driver = rng.normal(size=len(idx))
    cap = pd.DataFrame({k: 100 + 10 * driver for k in PRODUCTS}, index=idx)
    cap["rr_up"] = 100 + rng.normal(size=len(idx))           # unrelated
    cap["business_date"] = [ts.date() for ts in idx]
    q = pd.date_range(idx[0], periods=4 * len(idx), freq="15min", name="ts_utc")
    energy = pd.DataFrame({"price": np.repeat(400 + 50 * driver, 4), "business_date": [ts.date() for ts in q]},
                          index=q)
    corr = market.correlations(cap, energy)
    assert corr.loc[(2025, "afrr_up"), "pearson"] == pytest.approx(1.0)
    assert abs(corr.loc[(2025, "rr_up"), "pearson"]) < 0.1


def test_trend_of_halving_prices():
    months = [f"2025-{m:02d}" for m in range(1, 13)] + [f"2026-{m:02d}" for m in range(1, 13)]
    t = np.arange(24) / 12
    monthly = pd.DataFrame({k: 400 * 0.5 ** t for k in PRODUCTS}, index=months)
    tr = market.trends(monthly)
    assert tr.loc["fcr_up", "pct_per_year"] == pytest.approx(-50.0, abs=1e-6)
    assert tr.loc["fcr_up", "pln_per_year"] < 0


def test_capacity_market_layer():
    layer = capacity_market.capacity_layer()
    assert layer.loc[2030, "revenue_pln_mw_year"] == pytest.approx(465.02 * 1000 * 0.1339)
    assert layer.loc[2029, "revenue_pln_mw_year"] == pytest.approx(264.90 * 1000 * 0.6130)
    kwd = capacity_market.kwd_history()["kwd_battery"]
    assert kwd.loc[2026] > kwd.loc[2029] > kwd.loc[2030]
