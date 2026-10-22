"""Synthetic data in the shapes returned by ``pse_api.load_series`` (no network).

Tests never depend on the datetime resolution of pandas (ns in pandas 2, us by default in pandas 3): timestamps are
compared as ``pd.Timestamp`` values, never as integers or dtype strings.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from bess_stack.pse_api import PRODUCTS

TZ = "Europe/Warsaw"


def quarter_hours(start: date, end: date) -> pd.DatetimeIndex:
    """Interval starts (UTC) of local delivery days start..end-1 (92, 96 or 100 per day)."""
    idx = pd.date_range(pd.Timestamp(start, tz=TZ), pd.Timestamp(end, tz=TZ), freq="15min", inclusive="left")
    return pd.DatetimeIndex(idx.tz_convert("UTC"), name="ts_utc")


def business_dates(idx: pd.DatetimeIndex) -> np.ndarray:
    return np.asarray([ts.date() for ts in idx.tz_convert(TZ)])


def synthetic_energy(start: date, end: date, seed: int = 0) -> pd.DataFrame:
    """Duck-curve day-ahead prices: cheap (sometimes negative) middays, expensive evenings."""
    rng = np.random.default_rng(seed)
    idx = quarter_hours(start, end)
    local = idx.tz_convert(TZ)
    hour = np.asarray(local.hour + local.minute / 60, dtype=float)
    price = 450 - 380 * np.exp(-((hour - 13) ** 2) / 8) + 350 * np.exp(-((hour - 19.5) ** 2) / 3)
    price = price + rng.normal(0, 30, len(idx))
    return pd.DataFrame({"price": price.round(2), "business_date": business_dates(idx)}, index=idx)


def synthetic_capacity(start: date, end: date, seed: int = 1) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Hourly capacity prices (PLN/MW per hour) and MW bought for all products."""
    rng = np.random.default_rng(seed)
    q = quarter_hours(start, end)
    idx = pd.DatetimeIndex(q[::4], name="ts_utc")
    local = idx.tz_convert(TZ)
    hour = np.asarray(local.hour, dtype=float)
    evening = np.exp(-((hour - 19) ** 2) / 4)
    base = {"fcr_up": 60, "fcr_down": 40, "afrr_up": 90, "afrr_down": 50, "mfrr_up": 45, "mfrr_down": 20, "rr_up": 15}
    mw = {"fcr_up": 197, "fcr_down": 197, "afrr_up": 400, "afrr_down": 400, "mfrr_up": 581, "mfrr_down": 90,
          "rr_up": 400}
    prices = pd.DataFrame({k: base[k] * (1 + 2 * evening) * rng.uniform(0.7, 1.3, len(idx)) for k in PRODUCTS},
                          index=idx).round(2)
    volume = pd.DataFrame({k: np.full(len(idx), float(mw[k])) for k in PRODUCTS}, index=idx)
    bd = business_dates(idx)
    prices["business_date"] = bd
    volume["business_date"] = bd
    return prices, volume


def synthetic_offers(cap_price: pd.DataFrame, cap_volume: pd.DataFrame, n: int = 12, seed: int = 2) -> pd.DataFrame:
    """Offer stacks whose marginal offer at the MW bought equals the published price."""
    rng = np.random.default_rng(seed)
    rows = []
    for ts in cap_price.index:
        for k in PRODUCTS:
            d, p = cap_volume.at[ts, k], cap_price.at[ts, k]
            mw = np.full(n, 2 * d / n)                       # offered = twice the demand
            prices = np.sort(rng.uniform(0.1 * p, 0.99 * p, n))
            m = int(np.ceil(n / 2)) - 1                       # cumulative MW reaches demand at offer m
            prices[m] = p
            prices[m + 1:] = np.sort(rng.uniform(1.01 * p, 3 * p, n - m - 1))
            for q, c in zip(mw, prices):
                rows.append((ts, k, q, c, cap_price.at[ts, "business_date"]))
    df = pd.DataFrame(rows, columns=["ts_utc", "product", "mw", "price", "business_date"]).set_index("ts_utc")
    return df


@pytest.fixture(scope="session")
def week():
    """One week in July including normal days (energy prices, capacity prices and volumes)."""
    start, end = date(2025, 7, 14), date(2025, 7, 21)
    energy = synthetic_energy(start, end)
    price, volume = synthetic_capacity(start, end)
    return energy, price, volume
