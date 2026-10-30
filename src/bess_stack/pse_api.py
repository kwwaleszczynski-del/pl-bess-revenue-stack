"""PSE series of the analysis, read with the pse-opendata client (https://pypi.org/project/pse-opendata/).

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

pse-opendata downloads the public PSE reports API (no key) and caches whole months as Parquet files in ``cache_dir``
(``api/<report>/<YYYY-MM>.parquet``); a month is downloaded again until the revision window of its report has
passed. Offers are requested one business date at a time in a single page, because the keyset paging of the API
skips repeated offers at page boundaries. Frames returned here are indexed by interval START (UTC).

Usage:
    from bess_stack.pse_api import load_series
    df, status = load_series("cap_price", date(2024, 6, 14), date(2026, 10, 22), cache_dir=Path("data"))
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
import pse_opendata as pse

HISTORY_START = date(2024, 6, 14)   # first business date of the reformed balancing market reports
# resolution pandas gives time stamps parsed from text (ns in pandas 2, us in pandas 3); the frames keep it
TIME_UNIT = pd.to_datetime(pd.Series(["2024-06-14 00:15:00"]), format="ISO8601").dt.unit
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
STATUS = {"downloaded": "fetched", "cached": "cached", "stale": "stale"}   # pse-opendata status -> SeriesStatus list


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


def to_frame(raw: pd.DataFrame, series: str) -> pd.DataFrame:
    """pse-opendata frame -> frame indexed by interval START (UTC) with the column names of ``SERIES``.

    Rows without any value (future intervals) are dropped; offers keep every row, mapped to the product names.
    """
    spec = SERIES[series]
    if raw.empty:
        return empty_frame(series)
    out = raw.set_index("start_utc")[list(spec.fields)].rename(columns=spec.fields)
    out.index = out.index.as_unit(TIME_UNIT).rename("ts_utc")
    if "product" in out:
        out["product"] = out["product"].map(RESERVE_TYPES)
    out["business_date"] = raw["business_date"].to_numpy()
    out = out.dropna(subset=[c for c in spec.fields.values() if c != "product"], how="all")
    if spec.multi:
        # within an hour, the order of the API (offered MW, then price); saturation stacks break price ties by it
        out = out.dropna(subset=["product"]).reset_index()
        out = out.sort_values(["ts_utc", "mw", "price"], kind="stable", na_position="first")
        return out.set_index("ts_utc")
    return out


def load_series(series: str, start: date, end: date, cache_dir: Path, offline: bool = False,
                client: pse.Client | None = None) -> tuple[pd.DataFrame, SeriesStatus]:
    """Load business dates start..end month by month without raising on API errors.

    Whole months are requested, so that pse-opendata caches them and an offline run rebuilds the same result.
    When a download fails, the cached copy of the month is used and the month is reported as stale; without a cached
    copy it is reported as missing. ``offline`` uses the cache only. Settlement reports (``afrr_price``) are
    published with a delay of weeks and revised later, so their recent months are downloaded again on every run.
    """
    spec = SERIES[series]
    client = client or pse.Client(cache_dir=cache_dir, offline=offline, allow_stale=True)
    status = SeriesStatus(series)
    frames = []
    for month_start in pd.date_range(date(start.year, start.month, 1), end, freq="MS"):
        label = f"{month_start:%Y-%m}"
        m_end = (month_start + pd.offsets.MonthEnd(0)).date()
        loaded = len(client.loads)
        try:
            raw = client.get(spec.endpoint, month_start.date(), m_end, fields=list(spec.fields))
        except pse.PseError as e:
            log.error("%s %s: download failed (%s)", series, label, e)
            status.missing.append(label)
            continue
        for load in client.loads[loaded:]:
            getattr(status, STATUS[load.status]).append(label)
        df = to_frame(raw, series)
        log.info("%s %s: %d rows", series, label, len(df))
        frames.append(df)
    frames = [f for f in frames if not f.empty]
    out = pd.concat(frames).sort_index(kind="stable") if frames else empty_frame(series)
    if not spec.multi:
        out = out[~out.index.duplicated(keep="last")]
    out = out[(out["business_date"] >= start) & (out["business_date"] <= end)]
    status.last_interval = out.index.max() if len(out) else None
    return out, status
