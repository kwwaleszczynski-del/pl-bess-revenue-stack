from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
import requests

from bess_stack import pse_api
from bess_stack.pse_api import fetch_period, load_series, parse_records


def rec(dtime_utc: str, business_date: str, **values) -> dict:
    return {"dtime_utc": dtime_utc, "business_date": business_date, **values}


def test_quarter_hour_series_shift_end_to_start_and_dedupe():
    records = [
        rec("2025-07-14 22:30:00", "2025-07-15", rce_pln=481.79),
        rec("2025-07-14 22:15:00", "2025-07-15", rce_pln=481.79),
        rec("2025-07-14 22:15:00", "2025-07-15", rce_pln=500.0),     # duplicate interval: last one wins
    ]
    df = parse_records(records, "rce")
    assert list(df.index) == [pd.Timestamp("2025-07-14 22:00", tz="UTC"), pd.Timestamp("2025-07-14 22:15", tz="UTC")]
    assert df["price"].tolist() == [500.0, 481.79]
    assert df["business_date"].tolist() == [date(2025, 7, 15)] * 2


def test_hourly_capacity_prices_map_products_and_shift_one_hour():
    records = [rec("2025-07-14 23:00:00", "2025-07-15", fcr_g=67.06, fcr_d=35.44, afrr_g=129.83, afrr_d=35.45,
                   mfrrd_g=111.04, mfrrd_d=33.17, rr_g=9.89, rr_d=None)]
    df = parse_records(records, "cap_price")
    assert df.index[0] == pd.Timestamp("2025-07-14 22:00", tz="UTC")
    assert df.iloc[0][pse_api.PRODUCTS].tolist() == [67.06, 35.44, 129.83, 35.45, 111.04, 33.17, 9.89]


def test_offers_keep_all_rows_and_map_reserve_types():
    records = [
        rec("2026-10-19 23:00:00", "2026-10-20", reserve_type="aFRRᴳ", pom=1.0, com=5.0),
        rec("2026-10-19 23:00:00", "2026-10-20", reserve_type="aFRRᴳ", pom=1.0, com=5.0),   # same offer twice
        rec("2026-10-19 23:00:00", "2026-10-20", reserve_type="mFRRdᴰ", pom=10.0, com=12.5),
        rec("2026-10-19 23:00:00", "2026-10-20", reserve_type="unknown", pom=3.0, com=1.0),
    ]
    df = parse_records(records, "offers")
    assert len(df) == 3 and set(df.index) == {pd.Timestamp("2026-10-19 22:00", tz="UTC")}
    assert df["product"].tolist() == ["afrr_up", "afrr_up", "mfrr_down"]
    assert df["mw"].sum() == 12.0


def test_parse_records_drops_future_intervals_and_handles_empty():
    records = [rec("2025-07-15 10:00:00", "2025-07-15", eb_afrrg=3.2, eb_afrrd=-1.0),
               rec("2025-07-15 10:15:00", "2025-07-15", eb_afrrg=None, eb_afrrd=None)]
    assert len(parse_records(records, "afrr_energy")) == 1
    empty = parse_records([], "offers")
    assert empty.empty and {"product", "mw", "price", "business_date"} <= set(empty.columns)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    """Serves pages keyed by URL; records every call."""

    def __init__(self, pages: dict[str, dict] | None = None, fail: bool = False):
        self.pages, self.fail, self.calls = pages or {}, fail, []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self.fail:
            raise requests.ConnectionError("offline")
        return FakeResponse(self.pages[url])


def test_fetch_period_follows_next_link():
    first = f"{pse_api.BASE_URL}/mbp-tp"
    session = FakeSession({
        first: {"value": [rec("2025-07-14 23:00:00", "2025-07-15", fcr_g=171.0)], "nextLink": "page2"},
        "page2": {"value": [rec("2025-07-15 00:00:00", "2025-07-15", fcr_g=170.0)]},
    })
    df = fetch_period("cap_volume", date(2025, 7, 15), date(2025, 7, 15), session)
    assert df["fcr_up"].tolist() == [171.0, 170.0]
    params = session.calls[0][1]
    assert params["$filter"] == "business_date ge '2025-07-15' and business_date le '2025-07-15'"
    assert params["$first"] == 20000 and "mfrrd_g" in params["$select"]


def month_frame(month: str, value: float) -> pd.DataFrame:
    days = pd.date_range(f"{month}-01", periods=pd.Period(month).days_in_month, freq="D")
    idx = pd.DatetimeIndex(days.tz_localize("UTC"), name="ts_utc")
    return pd.DataFrame({"price": value, "business_date": [d.date() for d in days]}, index=idx)


def test_final_complete_month_comes_from_cache(tmp_path):
    month_frame("2025-01", 100.0).to_parquet(tmp_path / "rce_2025-01.parquet")
    session = FakeSession(fail=True)
    df, status = load_series("rce", date(2025, 1, 1), date(2025, 1, 31), tmp_path, today=date(2025, 3, 1),
                             session=session)
    assert session.calls == [] and status.cached == ["2025-01"]
    assert len(df) == 31 and (df["price"] == 100.0).all()


def test_recent_month_failure_falls_back_to_stale_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(pse_api.time, "sleep", lambda s: None)
    month_frame("2025-02", 50.0).iloc[:10].to_parquet(tmp_path / "rce_2025-02.parquet")
    df, status = load_series("rce", date(2025, 2, 1), date(2025, 2, 28), tmp_path, today=date(2025, 2, 20),
                             session=FakeSession(fail=True))
    assert status.stale == ["2025-02"] and len(df) == 10


def test_offline_reports_missing_months(tmp_path):
    df, status = load_series("cap_price", date(2025, 2, 1), date(2025, 3, 31), tmp_path, today=date(2025, 6, 1),
                             offline=True)
    assert df.empty and status.missing == ["2025-02", "2025-03"]


@pytest.mark.parametrize("series", list(pse_api.SERIES))
def test_empty_frame_has_utc_index(series):
    df = pse_api.empty_frame(series)
    assert str(df.index.tz) == "UTC" and df.index.name == "ts_utc"


class MonthFailsSession(FakeSession):
    """Fails for multi-day requests, serves one-day requests: exercises the day-by-day fallback for offers."""

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        start, end = params["$filter"].split("'")[1], params["$filter"].split("'")[3]
        if start != end:
            raise requests.HTTPError("500 Server Error")
        utc = pd.Timestamp(start) - pd.Timedelta("1h")
        return FakeResponse({"value": [rec(f"{utc:%Y-%m-%d} 23:00:00", start, reserve_type="FCR\u1d33", pom=5.0,
                                           com=10.0)]})


def test_offers_fall_back_to_daily_requests(monkeypatch):
    monkeypatch.setattr(pse_api.time, "sleep", lambda s: None)
    session = MonthFailsSession()
    df = fetch_period("offers", date(2025, 7, 1), date(2025, 7, 3), session)
    assert len(df) == 3 and sorted(set(df["business_date"])) == [date(2025, 7, d) for d in (1, 2, 3)]
    with pytest.raises(requests.HTTPError):
        fetch_period("cap_price", date(2025, 7, 1), date(2025, 7, 3), MonthFailsSession())
