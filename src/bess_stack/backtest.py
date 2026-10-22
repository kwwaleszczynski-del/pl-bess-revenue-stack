"""Day-by-day backtest of the optimiser over the PSE history, run in parallel processes.

A run configuration (``RunConfig``) is a battery, a strategy and the SoC-reserve durations. Every delivery day
with complete energy prices (92, 96 or 100 quarter-hours) and hourly capacity prices is solved independently;
results are one row per day and configuration.

Usage:
    days = build_days(rce, cap_price)
    daily = run_configs(days, [RunConfig("stacked_2h", BatteryParams(energy_mwh=2), "stacked")], workers=8)
    annual = annualise(daily)
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from bess_stack.optimizer import STRATEGIES, BatteryParams, optimize_day
from bess_stack.pse_api import PRODUCTS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Day:
    business_date: date
    hours: pd.DatetimeIndex        # UTC start of each capacity hour
    prices: np.ndarray             # energy price per quarter-hour
    hour_of: np.ndarray            # hour index of each quarter-hour
    reserve_prices: np.ndarray     # hours x PRODUCTS, PLN/MW per hour (NaN: not bought)


@dataclass(frozen=True)
class RunConfig:
    name: str
    params: BatteryParams
    strategy: str
    duration_req: dict = field(default_factory=dict)
    group: str = "base"            # base | sensitivity


def build_days(energy: pd.DataFrame, cap_price: pd.DataFrame) -> list[Day]:
    """Pair quarter-hour energy prices with hourly capacity prices per business date; incomplete days are skipped."""
    days = []
    cap = cap_price[PRODUCTS]
    cap_by_day = dict(tuple(cap.groupby(cap_price["business_date"].to_numpy())))
    for d, e in energy.groupby(energy["business_date"].to_numpy()):
        n = len(e)
        if n not in (92, 96, 100) or e["price"].isna().any() or d not in cap_by_day:
            continue
        c = cap_by_day[d]
        hours = pd.DatetimeIndex(e.index.floor("h").unique())
        if len(c) != len(hours) or not c.index.equals(hours) or n != 4 * len(hours):
            continue
        hour_of = hours.get_indexer(e.index.floor("h"))
        days.append(Day(d, hours, e["price"].to_numpy(float), hour_of, c.to_numpy(float)))
    return days


def solve_days(cfg: RunConfig, days: list[Day], reserve_prices: dict | None = None,
               reserve_limits: dict | None = None, keep_reserves: bool = False) -> list[dict]:
    """Solve every day for one configuration. Optional per-day overrides of capacity prices and MW limits (used by
    the saturation model) are keyed by business date."""
    strategy = STRATEGIES[cfg.strategy]
    rows = []
    for day in days:
        pi = reserve_prices.get(day.business_date, day.reserve_prices) if reserve_prices else day.reserve_prices
        lim = reserve_limits.get(day.business_date) if reserve_limits else None
        r = optimize_day(day.prices, day.hour_of, pi, cfg.params, strategy, duration_req=cfg.duration_req,
                         reserve_limit=lim)
        row = {"config": cfg.name, "business_date": day.business_date, "total": r.total, "arbitrage": r.arbitrage,
               "degradation": r.degradation, "cycles": r.cycles, "discharged_mwh": r.discharged_mwh,
               **{f"rev_{k}": v for k, v in r.reserve_revenue.items()},
               **{f"mw_{k}": float(r.reserves[k].mean()) for k in PRODUCTS}}
        if keep_reserves:
            row["reserves"] = r.reserves.to_numpy()
        rows.append(row)
    return rows


def _solve_chunk(args) -> list[dict]:
    return solve_days(*args)


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def run_configs(days: list[Day], configs: list[RunConfig], workers: int | None = None,
                keep_reserves: bool = False, reserve_prices: dict | None = None,
                reserve_limits: dict | None = None, executor: ProcessPoolExecutor | None = None) -> pd.DataFrame:
    """All configurations over all days; work is split into chunks of days per configuration.

    ``reserve_prices`` / ``reserve_limits`` override the capacity prices and MW limits per business date for every
    configuration. An open ``executor`` can be reused across calls (the saturation model calls this repeatedly).
    """
    workers = workers or default_workers()
    n_chunks = max(1, -(-2 * workers // max(1, len(configs))))      # about two tasks per worker
    chunk = max(1, -(-len(days) // n_chunks))
    tasks = []
    for cfg in configs:
        for i in range(0, len(days), chunk):
            part = days[i:i + chunk]
            keys = [d.business_date for d in part]
            pr = {k: reserve_prices[k] for k in keys if k in reserve_prices} if reserve_prices else None
            lim = {k: reserve_limits[k] for k in keys if k in reserve_limits} if reserve_limits else None
            tasks.append((cfg, part, pr, lim, keep_reserves))
    log.debug("solving %d configurations x %d days in %d tasks", len(configs), len(days), len(tasks))
    rows: list[dict] = []
    if executor is not None:
        for part in executor.map(_solve_chunk, tasks):
            rows += part
    elif workers == 1:
        for t in tasks:
            rows += _solve_chunk(t)
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for part in ex.map(_solve_chunk, tasks):
                rows += part
    out = pd.DataFrame(rows)
    return out.sort_values(["config", "business_date"], kind="stable").reset_index(drop=True)


REVENUE_COLS = ["total", "arbitrage", "degradation"] + [f"rev_{k}" for k in PRODUCTS]


def annualise(daily: pd.DataFrame) -> pd.DataFrame:
    """Per configuration and calendar year: sums, number of days and the full-year equivalent (sum / days * 365).

    Partial years (2024 from 14 June, the current year to date) are scaled by days covered, so seasons that are
    missing bias them; full years are reported as they are.
    """
    d = daily.copy()
    d["year"] = [x.year for x in d["business_date"]]
    g = d.groupby(["config", "year"])
    out = g[REVENUE_COLS + ["cycles"]].sum()
    out["days"] = g.size()
    for c in REVENUE_COLS:
        out[f"{c}_per_year"] = out[c] / out["days"] * 365
    out["cycles_per_day"] = out["cycles"] / out["days"]
    return out.drop(columns=["cycles"])


def period_summary(daily: pd.DataFrame, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Per configuration over a period: PLN per MW per year (mean per day x 365), split into components."""
    d = daily
    if start is not None:
        d = d[d["business_date"] >= start]
    if end is not None:
        d = d[d["business_date"] <= end]
    g = d.groupby("config")
    out = g[REVENUE_COLS].mean() * 365
    out["cycles_per_day"] = g["cycles"].mean()
    out["days"] = g.size()
    out["reserves"] = out[[f"rev_{k}" for k in PRODUCTS]].sum(axis=1)
    return out


def monthly(daily: pd.DataFrame) -> pd.DataFrame:
    """Per configuration and month: PLN per MW per day, averaged over the days of the month."""
    d = daily.copy()
    d["month"] = [f"{x:%Y-%m}" for x in d["business_date"]]
    return d.groupby(["config", "month"])[REVENUE_COLS + ["cycles"]].mean()
