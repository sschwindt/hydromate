"""Retrieve river-gauge data (discharge + stage-discharge) - GUI, multi-source.

A small, self-contained **Streamlit** helper that pulls **discharge** time series and
the **stage-discharge relation** (rating curve) for a river gauge and exports them as
CSVs that drop straight into a hydromate ``case-config.yml``:

* the discharge series -> ``boundaries.inflow`` (a ``datetime,Q`` CSV, m³/s);
* the stage-discharge table -> ``boundaries.stage_discharge`` (a ``Q,WSE`` CSV; enter
  the gauge-zero elevation to turn the relative stage into an absolute WSE).

Two data sources, auto-detected from what you paste:

* **HND Bayern** (Hochwassernachrichtendienst) - paste a gauge page URL, e.g.
  ``https://www.hnd.bayern.de/pegel/donau_bis_passau/muehldorf-18004506/abfluss`` or
  ``https://www.hnd.bayern.de/pegel/isar/rissbachdueker-16001303``. Discharge series
  come from ``.../tabelle?methode=abfluss`` and the rating from ``.../abflusstafel``
  (native units m³/s and cm; scraped from the HTML tables, stdlib parser).
* **USGS NWIS** (US Geological Survey) - paste a site number or a waterdata URL, e.g.
  ``11421000`` (Lower Yuba River near Marysville, CA). Discharge/stage come from the
  Instantaneous- or Daily-Values JSON web service and the rating from the USGS rating
  depot (native units ft³/s and ft; **converted to metric** on ingest).

Everything is normalised to **metric** (m³/s, m) so the exports feed hydromate directly.

Run it (from the repository root)::

    mamba run -n hydromate-env python cases/gauge_data.py
    # equivalently:  streamlit run cases/gauge_data.py

Running it with plain ``python`` re-launches it under Streamlit automatically; extra
arguments are forwarded (e.g. ``python cases/gauge_data.py --server.port 8600``).
"""

from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

import pandas as pd

HND_HOST = "www.hnd.bayern.de"
CFS_TO_CMS = 0.028316846592     # cubic feet/s -> cubic metres/s
FT_TO_M = 0.3048                # feet -> metres

PRESETS = {
    "HND · Muehldorf / Inn (18004506)":
        "https://www.hnd.bayern.de/pegel/donau_bis_passau/muehldorf-18004506/abfluss",
    "HND · Rissbachdueker / Isar (16001303)":
        "https://www.hnd.bayern.de/pegel/isar/rissbachdueker-16001303/abfluss",
    "USGS · Lower Yuba R nr Marysville CA (11421000)": "11421000",
    "USGS · Yuba R at Marysville CA (11421500)": "11421500",
}
# period label -> (days, use daily-values service)
PERIODS = {
    "7 days": (7, False), "30 days": (30, False), "90 days": (90, False),
    "1 year (daily mean)": (365, True),
}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def fetch(url: str, timeout: float = 40.0) -> str:
    """GET *url* and return the decoded body (browser-like User-Agent)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# The normalised result every backend returns
# --------------------------------------------------------------------------- #
@dataclass
class GaugeData:
    """Metric-normalised gauge data, source-agnostic for the GUI/export code."""

    source: str                 # "HND Bayern" | "USGS NWIS"
    label: str                  # station name
    station_id: str
    slug: str                   # safe filename stem
    discharge: pd.DataFrame     # columns: datetime, Q          (m³/s)
    stage: pd.DataFrame         # columns: datetime, stage_m    (may be empty)
    rating: pd.DataFrame        # columns: stage_m, Q, stage_native   (ascending stage)
    rating_note: str = ""
    q_unit_native: str = "m³/s"
    stage_unit_native: str = "m"
    source_urls: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# HND Bayern backend (HTML scraping, stdlib parser)
# --------------------------------------------------------------------------- #
@dataclass
class _HndGauge:
    base_url: str
    station_no: str
    slug: str


def parse_hnd_url(url: str) -> _HndGauge:
    """Resolve any HND gauge page URL to its base URL + station number.

    The gauge segment is the path element ending in ``-<digits>`` (the station
    number); everything after it (``/abfluss``, ``/abflusstafel``, ...) is dropped.
    """
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    gi = next((i for i, s in enumerate(segs) if re.search(r"-\d{4,}$", s)), None)
    if gi is None:
        raise ValueError(
            f"{url!r} is not an HND gauge URL (no '<name>-<number>' segment)")
    station_no = re.search(r"-(\d{4,})$", segs[gi]).group(1)
    base = urlunsplit((parts.scheme or "https", parts.netloc or HND_HOST,
                       "/" + "/".join(segs[: gi + 1]), "", ""))
    return _HndGauge(base_url=base, station_no=station_no, slug=segs[gi])


class _TableExtractor(HTMLParser):
    """Collect every ``<table>`` as a list of rows, each a list of cell texts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._stack:
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _de_float(text: str) -> float | None:
    """Parse a German-formatted number (comma decimal). Empty/'-' -> None."""
    s = _clean(text)
    if not s or s in {"-", "–"}:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def extract_tables(html: str) -> list[list[list[str]]]:
    p = _TableExtractor()
    p.feed(html)
    return p.tables


def hnd_station_title(html: str) -> str:
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    title = _clean(m.group(1)) if m else ""
    return re.sub(r"^(Abfluss|Wasserstand)\s+", "", title)


def hnd_timeseries(html: str, value_col: str) -> pd.DataFrame:
    """Parse an HND ``/tabelle`` page into a ``datetime, <value_col>`` DataFrame."""
    for rows in extract_tables(html):
        if rows and _clean(rows[0][0]).lower().startswith("datum"):
            recs = []
            for r in rows[1:]:
                if len(r) < 2:
                    continue
                ts = pd.to_datetime(_clean(r[0]), format="%d.%m.%Y %H:%M",
                                    errors="coerce")
                val = _de_float(r[1])
                if pd.notna(ts) and val is not None:
                    recs.append((ts, val))
            return (pd.DataFrame(recs, columns=["datetime", value_col])
                    .sort_values("datetime").reset_index(drop=True))
    raise ValueError("no 'Datum' measurement table found on the HND page")


def hnd_rating(html: str) -> tuple[pd.DataFrame, str]:
    """Parse the HND ``/abflusstafel`` matrix into a ``stage_cm, discharge_m3s`` table.

    Each data row starts with a base stage in cm, followed by the discharge at that
    base plus each column offset (``00 10 ... 90`` cm from the header). Empty cells
    (beyond the rated range) are skipped.
    """
    offsets: list[int] | None = None
    recs: list[tuple[int, float]] = []
    for rows in extract_tables(html):
        for r in rows:
            cells = [_clean(c) for c in r]
            if not cells:
                continue
            if offsets is None:
                if cells[0].lower() == "cm":
                    offsets = [int(c) for c in cells[1:] if re.fullmatch(r"-?\d+", c)]
                continue
            if re.fullmatch(r"-?\d+", cells[0]):
                base = int(cells[0])
                for j, val in enumerate(cells[1:]):
                    if j >= len(offsets):
                        break
                    q = _de_float(val)
                    if q is not None:
                        recs.append((base + offsets[j], q))
        if recs:
            break
    if not recs:
        raise ValueError("no Abflusstafel matrix found on the HND page")
    df = (pd.DataFrame(recs, columns=["stage_cm", "discharge_m3s"])
          .drop_duplicates("stage_cm").sort_values("stage_cm").reset_index(drop=True))
    m = re.search(r"Abflusskurve\s*\(([^)]+)\)", html)
    return df, (_clean(m.group(1)) if m else "")


def load_hnd(url: str) -> GaugeData:
    gauge = parse_hnd_url(url)
    q_url = f"{gauge.base_url}/tabelle?methode=abfluss"
    w_url = f"{gauge.base_url}/tabelle?methode=wasserstand"
    tafel_url = f"{gauge.base_url}/abflusstafel"
    q_html = fetch(q_url)
    discharge = hnd_timeseries(q_html, "Q")                     # already m³/s
    try:
        stage = hnd_timeseries(fetch(w_url), "stage_cm")
        stage["stage_m"] = stage["stage_cm"] / 100.0
        stage = stage[["datetime", "stage_m"]]
    except Exception:
        stage = pd.DataFrame(columns=["datetime", "stage_m"])
    rt, note = hnd_rating(fetch(tafel_url))
    rating = pd.DataFrame({"stage_m": rt["stage_cm"] / 100.0,
                           "Q": rt["discharge_m3s"], "stage_native": rt["stage_cm"]})
    return GaugeData(
        source="HND Bayern", label=hnd_station_title(q_html) or gauge.slug,
        station_id=gauge.station_no, slug=gauge.slug, discharge=discharge,
        stage=stage, rating=rating, rating_note=note, q_unit_native="m³/s",
        stage_unit_native="cm",
        source_urls={"discharge": q_url, "stage": w_url, "rating": tafel_url})


# --------------------------------------------------------------------------- #
# USGS NWIS backend (JSON web services + rating depot; converted to metric)
# --------------------------------------------------------------------------- #
def usgs_site_id(identifier: str) -> str:
    """Extract the USGS site number from a site number or a waterdata/NWIS URL."""
    m = re.search(r"(\d{8,15})", identifier)
    if not m:
        raise ValueError(f"no USGS site number found in {identifier!r}")
    return m.group(1)


def _usgs_series(site: str, param: str, period_days: int, daily: bool
                 ) -> tuple[str, pd.DataFrame]:
    """One USGS parameter as ``(site_name, DataFrame[datetime, value])`` (native units)."""
    svc = "dv" if daily else "iv"
    extra = "&statCd=00003" if daily else ""       # daily mean
    url = (f"https://waterservices.usgs.gov/nwis/{svc}/?sites={site}"
           f"&parameterCd={param}&period=P{period_days}D&format=json{extra}")
    ts = json.loads(fetch(url))["value"]["timeSeries"]
    if not ts:
        return "", pd.DataFrame(columns=["datetime", "value"])
    s = ts[0]
    label = s["sourceInfo"]["siteName"]
    vals = s["values"][0]["value"] if s["values"] else []
    raw = [(v["dateTime"], v["value"]) for v in vals
           if v["value"] not in ("", "-999999", "-999999.0")]
    df = pd.DataFrame(raw, columns=["datetime", "value"])
    if not df.empty:
        # keep local wall-clock: strip the trailing UTC offset, then parse naive
        stamps = df["datetime"].str.replace(r"([+-]\d{2}:?\d{2}|Z)$", "", regex=True)
        df["datetime"] = pd.to_datetime(stamps, errors="coerce")
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna().sort_values("datetime").reset_index(drop=True)
    return label, df


def usgs_rating(site: str) -> tuple[pd.DataFrame, str]:
    """Parse the USGS expanded shift-adjusted rating (``exsa``) into ``stage_ft, Q_cfs``."""
    url = f"https://waterdata.usgs.gov/nwisweb/get_ratings/?site_no={site}&file_type=exsa"
    text = fetch(url)
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if len(lines) < 3:
        raise ValueError(f"no rating curve published for USGS site {site}")
    hdr = lines[0].split("\t")
    try:
        ii, di = hdr.index("INDEP"), hdr.index("DEP")
    except ValueError as exc:
        raise ValueError(f"unexpected USGS rating format for site {site}") from exc
    rows = []
    for ln in lines[2:]:                            # skip the RDB format-spec line
        c = ln.split("\t")
        try:
            rows.append((float(c[ii]), float(c[di])))
        except (ValueError, IndexError):
            pass
    df = (pd.DataFrame(rows, columns=["stage_ft", "Q_cfs"])
          .drop_duplicates("stage_ft").sort_values("stage_ft").reset_index(drop=True))
    note = next((ln.strip("# ").strip() for ln in text.splitlines()
                 if ln.startswith("#") and re.search(r"expanded|shift|rating", ln, re.I)),
                "USGS expanded shift-adjusted (exsa) rating")
    return df, note[:200]


def load_usgs(identifier: str, period_days: int = 7, daily: bool = False) -> GaugeData:
    site = usgs_site_id(identifier)
    label, qd = _usgs_series(site, "00060", period_days, daily)     # discharge [cfs]
    if qd.empty and not label:
        raise ValueError(f"USGS returned no data for site {site} "
                         "(check the site number / period)")
    discharge = pd.DataFrame({"datetime": qd["datetime"],
                              "Q": qd["value"] * CFS_TO_CMS}) if not qd.empty \
        else pd.DataFrame(columns=["datetime", "Q"])
    _, sd = _usgs_series(site, "00065", period_days, daily)         # gage height [ft]
    stage = pd.DataFrame({"datetime": sd["datetime"], "stage_m": sd["value"] * FT_TO_M}) \
        if not sd.empty else pd.DataFrame(columns=["datetime", "stage_m"])
    try:
        rt, note = usgs_rating(site)
        rating = pd.DataFrame({"stage_m": rt["stage_ft"] * FT_TO_M,
                               "Q": rt["Q_cfs"] * CFS_TO_CMS,
                               "stage_native": rt["stage_ft"]})
    except ValueError as exc:
        rating, note = pd.DataFrame(columns=["stage_m", "Q", "stage_native"]), str(exc)
    return GaugeData(
        source="USGS NWIS", label=label or f"USGS {site}", station_id=site,
        slug=f"usgs-{site}", discharge=discharge, stage=stage, rating=rating,
        rating_note=note, q_unit_native="ft³/s", stage_unit_native="ft",
        source_urls={
            "discharge": f"https://waterdata.usgs.gov/monitoring-location/{site}/",
            "rating": f"https://waterdata.usgs.gov/nwisweb/get_ratings/"
                      f"?site_no={site}&file_type=exsa"})


# --------------------------------------------------------------------------- #
# Source dispatch
# --------------------------------------------------------------------------- #
def load_gauge(identifier: str, period_days: int = 7, daily: bool = False) -> GaugeData:
    """Auto-detect the source (HND Bayern vs USGS) and load metric gauge data."""
    ident = (identifier or "").strip()
    if not ident:
        raise ValueError("no gauge given")
    low = ident.lower()
    if "hnd.bayern.de" in low or "bayern" in low:
        return load_hnd(ident)
    if re.search(r"usgs|waterdata|waterservices", low) or re.fullmatch(r"\d{7,15}", ident):
        return load_usgs(ident, period_days, daily)
    if re.search(r"-\d{4,}(/|$)", ident):          # a bare HND path fragment
        return load_hnd(ident)
    raise ValueError(
        "could not tell if this is an HND Bayern URL or a USGS site number - "
        "paste a full HND gauge URL or a USGS site number (e.g. 11421000)")


# --------------------------------------------------------------------------- #
# Streamlit GUI
# --------------------------------------------------------------------------- #
def run_app() -> None:
    import streamlit as st

    st.set_page_config(page_title="Gauge data fetch", page_icon="🌊", layout="wide")
    st.title("🌊 River-gauge data fetch")
    st.caption("Discharge series + stage-discharge relation from **HND Bayern** or "
               "**USGS NWIS**, normalised to metric and exported for hydromate.")

    load_cached = st.cache_data(ttl=600, show_spinner=False)(load_gauge)

    with st.sidebar:
        st.header("Gauge")
        preset = st.selectbox("Preset", ["(paste a URL / site number)"] + list(PRESETS))
        default = PRESETS.get(preset, "")
        ident = st.text_input("HND gauge URL or USGS site number", value=default,
                              placeholder="https://www.hnd.bayern.de/pegel/... or 11421000")
        period_label = st.selectbox("Period (USGS only; HND serves ~7 days)",
                                    list(PERIODS), index=0)
        days, daily = PERIODS[period_label]
        go = st.button("Fetch", type="primary", use_container_width=True)

    if not (go and ident):
        st.info("Pick a preset or paste an HND gauge URL / USGS site number in the "
                "sidebar, then **Fetch**.")
        st.stop()

    try:
        with st.spinner("fetching gauge data ..."):
            data = load_cached(ident, days, daily)
    except Exception as exc:  # noqa: BLE001 - surface any fetch/parse failure cleanly
        st.error(f"could not retrieve the gauge data: {type(exc).__name__}: {exc}")
        st.stop()

    st.subheader(f"{data.label}  ·  {data.source}  ·  station {data.station_id}")
    if data.q_unit_native != "m³/s" or data.stage_unit_native != "m":
        st.caption(f"native units {data.q_unit_native} / {data.stage_unit_native} "
                   "converted to m³/s / m.")
    c1, c2, c3 = st.columns(3)
    if not data.discharge.empty:
        last = data.discharge.iloc[-1]
        c1.metric("latest discharge", f"{last['Q']:.2f} m³/s", help=str(last["datetime"]))
    c2.metric("rating points", f"{len(data.rating)}")
    c3.metric("rating note", (data.rating_note[:40] + "…") if len(data.rating_note) > 40
              else (data.rating_note or "n/a"))

    tab_q, tab_r, tab_w = st.tabs(
        ["Discharge series", "Stage-discharge relation", "Water level series"])

    with tab_q:
        _src_link(st, data, "discharge")
        if data.discharge.empty:
            st.warning("no discharge values returned.")
        else:
            st.line_chart(data.discharge.set_index("datetime")["Q"],
                          y_label="Q [m³/s]", x_label="time")
            out = data.discharge.copy()
            out["datetime"] = out["datetime"].dt.strftime("%Y-%m-%d %H:%M")
            st.download_button("⬇ boundaries.inflow CSV  (datetime,Q in m³/s)",
                               out.to_csv(index=False).encode("utf-8"),
                               file_name=f"{data.slug}-discharge.csv", mime="text/csv")
            st.dataframe(data.discharge, use_container_width=True, height=300)

    with tab_r:
        _src_link(st, data, "rating")
        if data.rating.empty:
            st.warning(f"no rating curve available. {data.rating_note}")
        else:
            st.caption(f"validity / note: *{data.rating_note or 'n/a'}*")
            st.scatter_chart(data.rating, x="Q", y="stage_m",
                             x_label="Q [m³/s]", y_label="stage above gauge zero [m]")
            st.markdown(
                "**Absolute water-surface elevation.** The stage is metres above the "
                "gauge zero (HND *Pegelnullpunkt* / USGS *gage datum*), which the "
                "services do not always publish. Enter it to export a hydromate "
                "`Q,WSE` rating; leave 0 to export the stage relative to the gauge zero.")
            zero = st.number_input("gauge-zero elevation [m a.s.l.]", value=0.0,
                                   step=0.001, format="%.3f")
            exp = data.rating.rename(columns={"Q": "Q"}).copy()
            if zero:
                exp["WSE"] = zero + exp["stage_m"]
                hbc = exp[["Q", "WSE", "stage_m"]]
                label = "⬇ boundaries.stage_discharge CSV  (Q,WSE)"
            else:
                hbc = exp[["Q", "stage_m"]]
                label = "⬇ rating CSV  (Q,stage_m - relative to gauge zero)"
            st.download_button(label, hbc.to_csv(index=False).encode("utf-8"),
                               file_name=f"{data.slug}-rating.csv", mime="text/csv")
            st.dataframe(data.rating, use_container_width=True, height=300)

    with tab_w:
        _src_link(st, data, "stage")
        if data.stage.empty:
            st.info("no water-level series available for this gauge.")
        else:
            st.line_chart(data.stage.set_index("datetime")["stage_m"],
                          y_label="stage [m]", x_label="time")
            out = data.stage.copy()
            out["datetime"] = out["datetime"].dt.strftime("%Y-%m-%d %H:%M")
            st.download_button("⬇ water-level CSV  (datetime,stage_m)",
                               out.to_csv(index=False).encode("utf-8"),
                               file_name=f"{data.slug}-stage.csv", mime="text/csv")
            st.dataframe(data.stage, use_container_width=True, height=300)


def _src_link(st, data: GaugeData, key: str) -> None:
    u = data.source_urls.get(key)
    if u:
        st.markdown(f"Source: [`{u}`]({u})")


# --------------------------------------------------------------------------- #
# Entry point: run under Streamlit, or re-launch itself under Streamlit
# --------------------------------------------------------------------------- #
def _under_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False


def _relaunch() -> int:
    try:
        from streamlit.web import cli as stcli
    except ModuleNotFoundError:
        sys.stderr.write(
            "Streamlit is not installed. Run in the hydromate GUI env, e.g.:\n"
            "    mamba run -n hydromate-env streamlit run cases/gauge_data.py\n")
        return 1
    sys.argv = ["streamlit", "run", __file__, *sys.argv[1:]]
    return stcli.main()


if __name__ == "__main__":
    if _under_streamlit():
        run_app()
    else:
        raise SystemExit(_relaunch())
