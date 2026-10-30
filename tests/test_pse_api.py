from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pse_opendata as pse
import pytest
import requests

from bess_stack import pse_api
from bess_stack.pse_api import load_series


def rec(dtime_utc: str, business_date: str, **values) -> dict:
    return {"dtime_utc": dtime_utc, "business_date": business_date, **values}


class FakeResponse:
    def __init__(self, payload):
        self.status_code, self.headers, self.reason, self.payload = 200, {}, "OK", payload

    def json(self):
        return self.payload


class FakeSession:
    """Answers the business_date filters of pse-opendata (range or single day) from fixed records."""

    def __init__(self, records: dict[str, list[dict]] | None = None, fail: bool = False):
        self.records, self.fail, self.calls = records or {}, fail, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params))
        if self.fail:
            raise requests.ConnectionError("offline")
        flt = params["$filter"]
        dates = [part.split("'")[1] for part in flt.split(" and ")]
        lo, hi = (dates[0], dates[0]) if " eq " in flt else (dates[0], dates[1])
        rows = self.records.get(url.rsplit("/", 1)[-1], [])
        return FakeResponse({"value": [r for r in rows if lo <= r["business_date"] <= hi]})


def load(series, start, end, tmp_path, records=None, fail=False, now=datetime(2026, 10, 25, tzinfo=UTC)):
    session = FakeSession(records, fail)
    client = pse.Client(session=session, cache_dir=tmp_path, now=lambda: now, sleep=lambda s: None, min_interval=0,
                        retries=1, allow_stale=True)
    df, status = load_series(series, start, end, tmp_path, client=client)
    return df, status, session


def test_quarter_hour_series_shift_end_to_start_and_dedupe(tmp_path):
    records = {"rce-pln": [
        rec("2025-07-14 22:30:00", "2025-07-15", rce_pln=481.79),
        rec("2025-07-14 22:15:00", "2025-07-15", rce_pln=481.79),
        rec("2025-07-14 22:15:00", "2025-07-15", rce_pln=500.0),     # duplicate interval: last one wins
    ]}
    df, _, _ = load("rce", date(2025, 7, 15), date(2025, 7, 15), tmp_path, records)
    assert list(df.index) == [pd.Timestamp("2025-07-14 22:00", tz="UTC"), pd.Timestamp("2025-07-14 22:15", tz="UTC")]
    assert df["price"].tolist() == [500.0, 481.79]
    assert df["business_date"].tolist() == [date(2025, 7, 15)] * 2


def test_hourly_capacity_prices_map_products_and_shift_one_hour(tmp_path):
    records = {"cmbp-tp": [rec("2025-07-14 23:00:00", "2025-07-15", fcr_g=67.06, fcr_d=35.44, afrr_g=129.83,
                               afrr_d=35.45, mfrrd_g=111.04, mfrrd_d=33.17, rr_g=9.89, rr_d=None)]}
    df, _, _ = load("cap_price", date(2025, 7, 15), date(2025, 7, 15), tmp_path, records)
    assert df.index[0] == pd.Timestamp("2025-07-14 22:00", tz="UTC")
    assert df.iloc[0][pse_api.PRODUCTS].tolist() == [67.06, 35.44, 129.83, 35.45, 111.04, 33.17, 9.89]


def test_offers_keep_all_rows_and_map_reserve_types(tmp_path):
    records = {"popmb-rmb": [
        rec("2026-10-19 23:00:00", "2026-10-20", reserve_type="aFRRᴳ", pom=1.0, com=5.0),
        rec("2026-10-19 23:00:00", "2026-10-20", reserve_type="aFRRᴳ", pom=1.0, com=5.0),   # same offer twice
        rec("2026-10-19 23:00:00", "2026-10-20", reserve_type="mFRRdᴰ", pom=10.0, com=12.5),
        rec("2026-10-19 23:00:00", "2026-10-20", reserve_type="unknown", pom=3.0, com=1.0),
    ]}
    df, _, session = load("offers", date(2026, 10, 20), date(2026, 10, 20), tmp_path, records)
    assert len(df) == 3 and set(df.index) == {pd.Timestamp("2026-10-19 22:00", tz="UTC")}
    assert sorted(df["product"]) == ["afrr_up", "afrr_up", "mfrr_down"]
    assert df["mw"].sum() == 12.0
    # one business date per request, in a single page (the keyset paging of the API skips repeated offers)
    assert all(" eq " in params["$filter"] for _, params in session.calls) and len(session.calls) == 31


def test_future_intervals_dropped_and_empty_frames(tmp_path):
    records = {"eb-rozl": [rec("2025-07-15 10:00:00", "2025-07-15", eb_afrrg=3.2, eb_afrrd=-1.0),
                           rec("2025-07-15 10:15:00", "2025-07-15", eb_afrrg=None, eb_afrrd=None)]}
    df, _, _ = load("afrr_energy", date(2025, 7, 15), date(2025, 7, 15), tmp_path, records)
    assert len(df) == 1
    empty = pse_api.empty_frame("offers")
    assert empty.empty and {"product", "mw", "price", "business_date"} <= set(empty.columns)


def month_records(month: str, value: float) -> list[dict]:
    days = pd.date_range(f"{month}-01", periods=pd.Period(month).days_in_month, freq="D")
    return [rec(f"{d:%Y-%m-%d} 10:00:00", f"{d:%Y-%m-%d}", rce_pln=value) for d in days]


def test_final_month_comes_from_cache_and_failures_fall_back_to_stale_copies(tmp_path):
    records = {"rce-pln": month_records("2025-01", 100.0) + month_records("2025-02", 50.0)}
    load("rce", date(2025, 1, 1), date(2025, 2, 28), tmp_path, records, now=datetime(2025, 3, 1, tzinfo=UTC))
    df, status, session = load("rce", date(2025, 1, 1), date(2025, 2, 28), tmp_path, fail=True,
                               now=datetime(2025, 3, 2, tzinfo=UTC))
    assert status.cached == ["2025-01"] and status.stale == ["2025-02"] and len(df) == 59
    assert "stale 2025-02" in status.text()


@pytest.mark.parametrize("series", list(pse_api.SERIES))
def test_offline_reports_missing_months(tmp_path, series):
    df, status = load_series(series, date(2025, 2, 1), date(2025, 3, 31), tmp_path, offline=True)
    assert df.empty and status.missing == ["2025-02", "2025-03"]
