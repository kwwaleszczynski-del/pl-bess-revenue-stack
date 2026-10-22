"""One pass of the whole study: market statistics, backtest of the strategies, sensitivity, aFRR activation
variant, reserve saturation and the capacity market layer.

Usage:
    data = load_data(Path("data"), today=date.today())
    result = run_analysis(data, workers=8)
    result.write_tables(Path("results"))
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from bess_stack import activation, capacity_market, market, saturation
from bess_stack.backtest import RunConfig, annualise, build_days, monthly, period_summary, run_configs
from bess_stack.optimizer import DURATION_REQUIREMENT, BatteryParams
from bess_stack.pse_api import HISTORY_START, SERIES, SeriesStatus, load_series

log = logging.getLogger(__name__)
DURATIONS = (1, 2, 4)
STRATEGY_NAMES = ("arbitrage", "reserves", "stacked")
FLEETS_MW = (0, 250, 500, 1000, 2000, 4000)


@dataclass
class Data:
    frames: dict[str, pd.DataFrame]
    status: dict[str, SeriesStatus]
    downloaded: str


def load_data(cache_dir: Path, today: date, start: date = HISTORY_START, end: date | None = None,
              offline: bool = False, series=None) -> Data:
    end = end or today + timedelta(days=1)
    frames, status = {}, {}
    for name in series or SERIES:
        frames[name], status[name] = load_series(name, start, end, cache_dir, today, offline=offline)
        log.info("%s: %d rows, %s", name, len(frames[name]), status[name].text())
    return Data(frames, status, f"{today:%d %B %Y}".lstrip("0"))


def base_configs() -> list[RunConfig]:
    return [RunConfig(f"{s}_{e}h", BatteryParams(energy_mwh=e), s) for e in DURATIONS for s in STRATEGY_NAMES]


def sensitivity_configs() -> list[RunConfig]:
    out = []
    for s in ("arbitrage", "stacked"):
        for rte in (0.80, 0.92):
            out.append(RunConfig(f"{s}_2h_rte{rte * 100:.0f}", BatteryParams(round_trip_efficiency=rte), s,
                                 group="sensitivity"))
        for deg in (0.0, 60.0, 100.0):
            out.append(RunConfig(f"{s}_2h_deg{deg:.0f}", BatteryParams(degradation_cost=deg), s, group="sensitivity"))
    short = {k: 0.25 for k in DURATION_REQUIREMENT}
    for s in ("reserves", "stacked"):
        out.append(RunConfig(f"{s}_2h_req15min", BatteryParams(), s, short, group="sensitivity"))
    out.append(RunConfig("stacked_2h_fcr30min", BatteryParams(), "stacked", {"fcr_up": 0.5, "fcr_down": 0.5},
                         group="sensitivity"))
    return out


@dataclass
class Analysis:
    period: tuple[date, date]
    last12: tuple[date, date]
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def write_tables(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, t in self.tables.items():
            if name.startswith("_"):
                continue
            t.round(4).to_csv(out_dir / f"{name}.csv")
        (out_dir / "summary.json").write_text(json.dumps(self.summary(), indent=2, default=str), encoding="utf-8")

    def summary(self) -> dict:
        t = self.tables
        s = {"period": [str(d) for d in self.period], "last_12_months": [str(d) for d in self.last12],
             **{k: v for k, v in self.meta.items() if not k.startswith("_")}}
        for key in ("revenue_last12", "revenue_2025"):
            if key in t:
                cols = ["total", "arbitrage", "reserves"]
                s[key] = {c: {k: round(float(v), 0) for k, v in t[key].loc[c, cols].items()}
                          for c in t[key].index if not any(x in c for x in ("rte", "deg", "req", "fcr30"))}
        if "market_room" in t:
            s["market_room_mw"] = {k: round(float(v), 0) for k, v in t["market_room"]["mean_mw"].items()}
        if "fleet" in t:
            s["fleet_total_pln_mw_year"] = {int(k): round(float(v), 0) for k, v in t["fleet"]["total"].items()}
        if "capacity_layer" in t:
            s["capacity_market_pln_mw_year"] = {int(k): round(float(v), 0)
                                                for k, v in t["capacity_layer"]["revenue_pln_mw_year"].items()}
        return s


def _common_period(frames: dict[str, pd.DataFrame]) -> tuple[date, date]:
    rce, cap = frames["rce"], frames["cap_price"]
    full = rce.groupby(rce["business_date"].to_numpy()).size()
    full = full[full >= 92]
    first = max(HISTORY_START, min(full.index), min(cap["business_date"]))
    last = min(max(full.index), max(cap["business_date"]))
    return first, last


def run_analysis(data: Data, workers: int | None = None, fleets_mw=FLEETS_MW, iterations: int = 8,
                 sensitivity: bool = True, saturation_days: int = 365) -> Analysis:
    f = data.frames
    start, end = _common_period(f)
    last12 = (end - timedelta(days=364), end)
    a = Analysis((start, end), last12)
    t = a.tables
    rce = f["rce"][(f["rce"]["business_date"] >= start) & (f["rce"]["business_date"] <= end)]
    cap_price = f["cap_price"][(f["cap_price"]["business_date"] >= start) & (f["cap_price"]["business_date"] <= end)]
    cap_volume = f["cap_volume"][(f["cap_volume"]["business_date"] >= start)
                                 & (f["cap_volume"]["business_date"] <= end)]

    # 1. market
    t["market_monthly_prices"] = market.monthly_prices(cap_price)
    years = range(start.year, end.year + 1)
    t["market_hour_profile"] = pd.concat(
        {str(y): market.hour_profile(cap_price, date(y, 1, 1), date(y, 12, 31)) for y in years}, names=["year"])
    t["market_hour_profile_last12"] = market.hour_profile(cap_price, *last12)
    t["market_annual"] = market.annual_market(cap_price, cap_volume)
    t["market_depth"] = market.monthly_depth(cap_volume, f["sup_volume"], f["offers"])
    t["market_depth_last12"] = market.depth_summary(cap_volume, f["sup_volume"], f["offers"], *last12)
    t["market_correlation"] = market.correlations(cap_price, rce)
    complete = t["market_monthly_prices"]
    if (end + timedelta(days=1)).month == end.month:     # drop the running month from the trend
        complete = complete.iloc[:-1]
    t["market_trend"] = market.trends(complete)
    t["market_rce_monthly"] = rce["price"].groupby(np.asarray([f"{d:%Y-%m}" for d in rce["business_date"]])).mean() \
        .rename("rce").to_frame()

    # 2. backtest
    days = build_days(rce, cap_price)
    log.info("%d complete days %s..%s", len(days), days[0].business_date, days[-1].business_date)
    base = run_configs(days, base_configs(), workers, keep_reserves=True)
    reserves_by = {c: dict(zip(g["business_date"], g["reserves"])) for c, g in base.groupby("config")}
    daily = base.drop(columns=["reserves"])
    if sensitivity:
        daily = pd.concat([daily, run_configs(days, sensitivity_configs(), workers)], ignore_index=True)
    t["_daily"] = daily.set_index(["config", "business_date"])
    t["annual"] = annualise(daily)
    t["revenue_period"] = period_summary(daily)
    t["revenue_last12"] = period_summary(daily, *last12)
    t["revenue_2025"] = period_summary(daily, date(2025, 1, 1), date(2025, 12, 31))
    t["monthly_revenue"] = monthly(daily)

    # 3. aFRR activation variant on the stacked schedules
    ratio = activation.activation_ratio(f["afrr_energy"], f["cap_volume"], f["sup_volume"])
    act = []
    for e in DURATIONS:
        name = f"stacked_{e}h"
        df = activation.activation_revenue(reserves_by[name], days, ratio, f["afrr_price"], rce,
                                           BatteryParams(energy_mwh=e))
        if len(df):
            df.insert(0, "config", name)
            act.append(df)
    if act:
        act = pd.concat(act, ignore_index=True)
        t["_activation_daily"] = act.set_index(["config", "business_date"])
        cap_rev = daily.set_index(["config", "business_date"])
        rows = []
        for name, g in act.groupby("config"):
            joined = cap_rev.loc[name].reindex(g["business_date"])
            rows.append({"config": name, "days": len(g), "first_day": g["business_date"].min(),
                         "last_day": g["business_date"].max(),
                         "capacity_total_per_year": joined["total"].mean() * 365,
                         "afrr_capacity_per_year": (joined["rev_afrr_up"] + joined["rev_afrr_down"]).mean() * 365,
                         "activation_margin_per_year": g["margin"].mean() * 365,
                         "activation_up_per_year": g["margin_up"].mean() * 365,
                         "activation_down_per_year": g["margin_down"].mean() * 365,
                         "energy_up_mwh_per_year": g["energy_up_mwh"].mean() * 365,
                         "energy_down_mwh_per_year": g["energy_down_mwh"].mean() * 365})
        t["activation"] = pd.DataFrame(rows).set_index("config")
        a.meta["activation_ratio_mean"] = {"up": round(float(ratio["alpha_up"].mean()), 4),
                                           "down": round(float(ratio["alpha_down"].mean()), 4)}

    # 4. saturation on the offer stacks of the last ``saturation_days`` days with offers
    offers = f["offers"]
    if len(offers):
        last_off = max(offers["business_date"])
        s_end = min(last_off, end)
        s_start = s_end - timedelta(days=saturation_days - 1)
        sel = (offers["business_date"] >= s_start) & (offers["business_date"] <= s_end)
        st = saturation.build_stacks(offers[sel], cap_volume, cap_price)
        t["saturation_validation"] = saturation.validate(st)
        curves = saturation.product_curves(st, np.arange(0, 2001, 25))
        t["saturation_curves"] = curves
        t["market_room"] = saturation.market_room(st, curves)
        ref_days = [d for d in days if s_start <= d.business_date <= s_end]
        cfg = RunConfig("stacked_2h", BatteryParams(energy_mwh=2), "stacked")
        fleet, conv = saturation.fleet_equilibrium(ref_days, st, cfg, fleets_mw, iterations, workers=workers)
        arb = daily[(daily["config"] == "arbitrage_2h") & (daily["business_date"] >= s_start)
                    & (daily["business_date"] <= s_end)]
        fleet["arbitrage_only"] = float(arb["total"].mean() * 365)
        t["fleet"] = fleet
        t["fleet_convergence"] = conv
        a.meta["saturation_window"] = [str(s_start), str(s_end)]

    # 5. capacity market layer
    t["capacity_layer"] = capacity_market.capacity_layer()
    t["capacity_kwd"] = capacity_market.kwd_history()

    a.meta.update({
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "data_downloaded": data.downloaded,
        "days": len(days),
        "status": {k: v.text() for k, v in data.status.items()},
    })
    return a
