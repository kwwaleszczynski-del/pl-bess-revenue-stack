"""Client for the public PSE reports API (https://api.raporty.pse.pl) with a monthly Parquet cache.

Series used here (business dates in Europe/Warsaw; field meanings and units from the PSE report pages and the
API field map https://api.raporty.pse.pl/EndpointsMap.pdf):

    rce          rce-pln       RCE, market price of electricity (day-ahead), PLN/MWh, 15 min
    sdac         csdac-pln     SDAC day-ahead price for the Polish zone, PLN/MWh, 15 min
    cap_price    cmbp-tp       prices of balancing capacity bought in the basic (day-ahead) procurement, CMBP,
                               PLN/MW per hour ("zl/MW-h"), hourly
    cap_volume   mbp-tp        balancing capacity bought in the basic procurement, MW, hourly
    sup_price    cmbu-tu       prices of balancing capacity bought in the supplementary procurement, CMBU,
                               PLN/MW per hour, 15 min
    sup_volume   mbu-tu        balancing capacity bought in the supplementary procurement, MW, 15 min
    afrr_energy  eb-rozl       balancing energy from aFRR activated on the European aFRR platform, MWh per
                               15 min (eb_afrrg up, eb_afrrd down, negative); published since the platform
                               connection in 2025
    afrr_price   crb-rozl      average balancing energy price of aFRR activations up / down (CEB aFRRg/aFRRd)
                               and the imbalance price CEN, PLN/MWh, 15 min
    offers       popmb-rmb     accepted portfolio offers for balancing capacity in the basic procurement
                               (anonymous): offered MW (pom) and offer price (com, PLN/MW per hour), hourly,
                               many rows per hour

Products (capacity reserve types): FCR (frequency containment), aFRR (automatic frequency restoration), mFRRd
(manual frequency restoration, direct activation) and RR (replacement reserve), each up ("g") and down ("d").
RR is only bought upwards.

The API needs no key. ``dtime_utc`` is the END of each interval; frames returned here are indexed by interval START
(UTC). Pagination: ``$filter`` on business_date, ``$first=20000`` and ``nextLink``.

Usage:
    from bess_stack.pse_api import load_series
    df, status = load_series("cap_price", date(2024, 6, 14), date(2026, 10, 22), cache_dir=Path("data"),
                             today=date.today())
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://api.raporty.pse.pl/api"
HISTORY_START = date(2024, 6, 14)   # first business date of the reformed balancing market reports
log = logging.getLogger(__name__)

PRODUCTS = ["fcr_up", "fcr_down", "afrr_up", "afrr_down", "mfrr_up", "mfrr_down", "rr_up"]
UP = [p for p in PRODUCTS if p.endswith("_up")]
DOWN = [p for p in PRODUCTS if p.endswith("_down")]
CAPACITY_FIELDS = {"fcr_g": "fcr_up", "fcr_d": "fcr_down", "afrr_g": "afrr_up", "afrr_d": "afrr_down",
                   "mfrrd_g": "mfrr_up", "mfrrd_d": "mfrr_down", "rr_g": "rr_up"}
SUPPLEMENTARY_FIELDS = {k: v for k, v in CAPACITY_FIELDS.items() if not k.startswith("rr")}
# reserve_type labels of popmb-rmb use modifier letters: U+1D33 (superscript G, "gora" = up), U+1D30 (D, down)
RESERVE_TYPES = {"FCRᴳ": "fcr_up", "FCRᴰ": "fcr_down", "aFRRᴳ": "afrr_up", "aFRRᴰ": "afrr_down",
                 "mFRRdᴳ": "mfrr_up", "mFRRdᴰ": "mfrr_down", "RRᴳ": "rr_up", "RRᴰ": "rr_down"}


@dataclass(frozen=True)
class Series:
    endpoint: str
    fields: dict[str, str]          # API field -> column name
    interval: str = "15min"
    multi: bool = False             # several rows per interval (offers)


SERIES = {
    "rce": Series("rce-pln", {"rce_pln": "price"}),
    "sdac": Series("csdac-pln", {"csdac_pln": "price"}),
    "cap_price": Series("cmbp-tp", CAPACITY_FIELDS, "1h"),
    "cap_volume": Series("mbp-tp", CAPACITY_FIELDS, "1h"),
    "sup_price": Series("cmbu-tu", SUPPLEMENTARY_FIELDS),
    "sup_volume": Series("mbu-tu", SUPPLEMENTARY_FIELDS),
    "afrr_energy": Series("eb-rozl", {"eb_afrrg": "afrr_up_mwh", "eb_afrrd": "afrr_down_mwh"}),
    "afrr_price": Series("crb-rozl", {"ceb_sr_afrrg_cost": "afrr_up_price", "ceb_sr_afrrd_cost": "afrr_down_price",
                                      "cen_cost": "imbalance_price"}),
    "offers": Series("popmb-rmb", {"reserve_type": "product", "pom": "mw", "com": "price"}, "1h", multi=True),
}


@dataclass
class SeriesStatus:
    """What happened while loading one series (shown on the dashboard as data freshness)."""
    name: str
    fetched: list[str] = field(default_factory=list)   # months downloaded in this run
    cached: list[str] = field(default_factory=list)    # final months read from cache
    stale: list[str] = field(default_factory=list)     # download failed, older cached copy used
    missing: list[str] = field(default_factory=list)   # download failed and no cache
    last_interval: pd.Timestamp | None = None

    def text(self) -> str:
        last = "none" if self.last_interval is None else f"{self.last_interval:%Y-%m-%d %H:%M} UTC"
        extra = f", stale {','.join(self.stale)}" if self.stale else ""
        extra += f", missing {','.join(self.missing)}" if self.missing else ""
        return f"last interval {last}{extra}"


def empty_frame(series: str) -> pd.DataFrame:
    spec = SERIES[series]
    idx = pd.DatetimeIndex([], tz="UTC", name="ts_utc")
    out = pd.DataFrame({c: pd.Series(dtype=object if c == "product" else float) for c in spec.fields.values()},
                       index=idx)
    out["business_date"] = pd.Series(dtype=object)
    return out


def parse_records(records: list[dict], series: str) -> pd.DataFrame:
    """API records -> frame indexed by interval START (UTC). Rows without any value (future intervals) are dropped."""
    if not records:
        return empty_frame(series)
    spec = SERIES[series]
    df = pd.DataFrame.from_records(records)
    cols = {}
    for api, col in spec.fields.items():
        if api == "reserve_type":
            cols[col] = df[api].map(RESERVE_TYPES) if api in df else None
        else:
            cols[col] = pd.to_numeric(df[api], errors="coerce") if api in df else float("nan")
    out = pd.DataFrame(cols)
    end = pd.to_datetime(df["dtime_utc"], format="ISO8601")
    out.index = pd.DatetimeIndex(end, name="ts_utc").tz_localize("UTC") - pd.Timedelta(spec.interval)
    out["business_date"] = pd.to_datetime(df["business_date"]).dt.date.to_numpy()
    values = [c for c in spec.fields.values() if c != "product"]
    out = out.dropna(subset=values, how="all")
    if spec.multi:
        return out.dropna(subset=["product"]).sort_index(kind="stable")
    return out[~out.index.duplicated(keep="last")].sort_index()


def _get(session: requests.Session, url: str, params: dict | None = None, retries: int = 4) -> dict:
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=120)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == retries - 1:
                raise
            log.warning("PSE API request failed (%s), retrying", e)
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def _fetch_records(session: requests.Session, spec: Series, start: date, end: date) -> list[dict]:
    params = {
        "$filter": f"business_date ge '{start:%Y-%m-%d}' and business_date le '{end:%Y-%m-%d}'",
        "$select": ",".join(["dtime_utc", "business_date", *spec.fields]),
        "$first": 20000,
    }
    payload = _get(session, f"{BASE_URL}/{spec.endpoint}", params)
    records = list(payload.get("value", []))
    while payload.get("nextLink"):
        payload = _get(session, payload["nextLink"])
        records += payload.get("value", [])
    return records


def fetch_period(series: str, start: date, end: date, session: requests.Session | None = None) -> pd.DataFrame:
    """Fetch one series for business dates start..end (inclusive), following pagination links.

    Offers (about 5,000-9,000 rows per day) need many pages per month and the deep pages sometimes fail with
    server errors; the month is then requested again one business date at a time (one page per day, slower).
    """
    spec = SERIES[series]
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", "pl-bess-revenue-stack (open-source analysis)")
    try:
        return parse_records(_fetch_records(session, spec, start, end), series)
    except (requests.RequestException, ValueError) as e:
        if not spec.multi or start == end:
            raise
        log.warning("%s %s..%s: paginated download failed (%s), retrying day by day", series, start, end, e)
    records = []
    for d in pd.date_range(start, end, freq="D"):
        records += _fetch_records(session, spec, d.date(), d.date())
    return parse_records(records, series)


def load_series(series: str, start: date, end: date, cache_dir: Path, today: date, refresh_days: int = 10,
                session: requests.Session | None = None, offline: bool = False) -> tuple[pd.DataFrame, SeriesStatus]:
    """Load business dates start..end month by month without raising on API errors.

    Months that ended more than ``refresh_days`` before ``today`` are final and read from cache when the cached copy
    reaches the last day of the month; recent months are downloaded again (late publications and corrections).
    When a download fails, the last cached copy is used and the month is reported as stale. ``offline`` uses the
    cache only. Settlement reports (``afrr_price``) are published with a delay of weeks, so their recent months stay
    incomplete and are downloaded again on every run.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    session = session or requests.Session()
    status = SeriesStatus(series)
    frames = []
    for month_start in pd.date_range(date(start.year, start.month, 1), end, freq="MS"):
        label = f"{month_start:%Y-%m}"
        m_start = max(month_start.date(), start)
        m_end = (month_start + pd.offsets.MonthEnd(0)).date()
        cache = cache_dir / f"{series}_{label}.parquet"
        final = m_end < today - timedelta(days=refresh_days)
        cached = pd.read_parquet(cache) if cache.exists() else None
        complete = cached is not None and len(cached) > 0 and cached["business_date"].max() >= m_end
        if (offline and cached is not None) or (final and complete):
            frames.append(cached)
            status.cached.append(label)
            continue
        df = None
        if not offline:
            try:
                df = fetch_period(series, m_start, min(m_end, end), session)
            except (requests.RequestException, ValueError, KeyError) as e:
                log.error("%s %s: download failed (%s)", series, label, e)
        if df is not None:
            if not df.empty:
                df.to_parquet(cache)
            frames.append(df)
            status.fetched.append(label)
            log.info("%s %s: %d rows", series, label, len(df))
        elif cached is not None:
            frames.append(cached)
            status.stale.append(label)
        else:
            status.missing.append(label)
    frames = [f for f in frames if not f.empty]
    out = pd.concat(frames).sort_index(kind="stable") if frames else empty_frame(series)
    if not SERIES[series].multi:
        out = out[~out.index.duplicated(keep="last")]
    out = out[(out["business_date"] >= start) & (out["business_date"] <= end)]
    status.last_interval = out.index.max() if len(out) else None
    return out, status
