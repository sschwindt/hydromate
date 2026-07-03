"""Retrieve gauge data from the Hochwassernachrichtendienst (HND) Bayern - GUI.

A small, self-contained **Streamlit** helper that pulls **discharge** (Abfluss)
time series and the **stage-discharge relation** (Abflusstafel / Abflusskurve) for
any HND Bayern gauge and exports them as CSVs that drop straight into a hydromate
``case-config.yml``:

* the discharge series -> ``boundaries.inflow`` (a ``datetime,Q`` CSV);
* the stage-discharge table -> ``boundaries.stage_discharge`` (a ``Q,WSE`` CSV;
  needs the gauge-zero height ``Pegelnullpunkt`` to turn the cm stage into an
  absolute water-surface elevation - HND does not publish it, so enter it in the UI).

It lives in ``cases/`` (above the individual example cases) because it is a generic,
case-independent data-fetch tool: point it at any gauge, save the CSVs into the case
you are building.

Data source (HTML tables scraped from the public pages, stdlib parser - no bs4/lxml):

* discharge series : ``<gauge>/tabelle?methode=abfluss``
* water-level series: ``<gauge>/tabelle?methode=wasserstand``
* rating table      : ``<gauge>/abflusstafel``

where ``<gauge>`` is e.g.
``https://www.hnd.bayern.de/pegel/donau_bis_passau/muehldorf-18004506`` or
``https://www.hnd.bayern.de/pegel/isar/rissbachdueker-16001303``. Paste any of a
gauge's pages (``.../abfluss``, ``.../wasserstand``, ``.../abflusstafel`` or the bare
gauge URL); the tool derives the endpoints from it.

Run it (from the repository root)::

    mamba run -n hydromate-env python cases/hnd_bayern_gauge.py
    # equivalently:  streamlit run cases/hnd_bayern_gauge.py

Running it with plain ``python`` re-launches it under Streamlit automatically; extra
arguments are forwarded (e.g. ``python cases/hnd_bayern_gauge.py --server.port 8600``).
"""

from __future__ import annotations

import re
import ssl
import sys
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

import pandas as pd

HND_HOST = "www.hnd.bayern.de"
PRESETS = {
    "Muehldorf / Inn (18004506)":
        "https://www.hnd.bayern.de/pegel/donau_bis_passau/muehldorf-18004506/abfluss",
    "Rissbachdueker / Isar (16001303)":
        "https://www.hnd.bayern.de/pegel/isar/rissbachdueker-16001303/abfluss",
}


# --------------------------------------------------------------------------- #
# HTTP + HTML parsing (pure functions, no Streamlit - unit-testable offline)
# --------------------------------------------------------------------------- #
@dataclass
class Gauge:
    """A resolved HND gauge: its base URL, station number and slug."""

    base_url: str
    station_no: str
    slug: str


def parse_gauge_url(url: str) -> Gauge:
    """Resolve any HND gauge page URL to its base URL + station number.

    Accepts the bare gauge URL or any of its sub-pages (``/abfluss``,
    ``/wasserstand``, ``/abflusstafel``, ``/tabelle?...``). The gauge segment is the
    path element ending in ``-<digits>`` (the station number).
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("no gauge URL given")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    gi = next((i for i, s in enumerate(segs) if re.search(r"-\d{4,}$", s)), None)
    if gi is None:
        raise ValueError(
            f"{url!r} does not look like an HND gauge URL (no '<name>-<number>' "
            "segment, e.g. 'muehldorf-18004506')")
    station_no = re.search(r"-(\d{4,})$", segs[gi]).group(1)
    base_path = "/" + "/".join(segs[: gi + 1])
    base = urlunsplit((parts.scheme or "https", parts.netloc or HND_HOST,
                       base_path, "", ""))
    return Gauge(base_url=base, station_no=station_no, slug=segs[gi])


def fetch(url: str, timeout: float = 30.0) -> str:
    """GET *url* and return the decoded HTML (browser-like User-Agent)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8", "replace")


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
    """Normalise a cell: drop non-breaking spaces, collapse whitespace."""
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
    """Parse all HTML tables into nested lists of cell text."""
    p = _TableExtractor()
    p.feed(html)
    return p.tables


def station_title(html: str) -> str:
    """The gauge title, e.g. 'Rissbachdueker / Isar' (leading 'Abfluss' stripped)."""
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    title = _clean(m.group(1)) if m else ""
    return re.sub(r"^(Abfluss|Wasserstand)\s+", "", title)


def parse_timeseries(html: str, quantity: str) -> pd.DataFrame:
    """Parse a ``/tabelle`` page into a ``datetime, value`` DataFrame (ascending).

    *quantity* is only used for the value-column name (``Q`` for discharge,
    ``stage_cm`` for water level). Matches the ``Datum`` / value table by its header.
    """
    for rows in extract_tables(html):
        if not rows:
            continue
        header = [_clean(c).lower() for c in rows[0]]
        if header and header[0].startswith("datum"):
            recs = []
            for r in rows[1:]:
                if len(r) < 2:
                    continue
                ts = pd.to_datetime(_clean(r[0]), format="%d.%m.%Y %H:%M",
                                    errors="coerce")
                val = _de_float(r[1])
                if pd.notna(ts) and val is not None:
                    recs.append((ts, val))
            df = pd.DataFrame(recs, columns=["datetime", quantity])
            return df.sort_values("datetime").reset_index(drop=True)
    raise ValueError("no 'Datum' measurement table found on the page")


@dataclass
class Rating:
    """A parsed stage-discharge relation and its validity note."""

    table: pd.DataFrame       # columns: stage_cm, discharge_m3s (ascending stage)
    valid_since: str          # e.g. 'gueltig seit 01.01.2016' (raw German note)


def parse_rating(html: str) -> Rating:
    """Parse the ``/abflusstafel`` matrix into a tidy stage-discharge table.

    The Abflusstafel is a matrix: each data row starts with a base stage in cm
    (``<b>0</b>``, ``<b>100</b>``, ... - may be negative), followed by the discharge
    at that base plus each column offset (``00 10 ... 90`` cm from the header row).
    Empty cells (beyond the rated range) are skipped.
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
            if re.fullmatch(r"-?\d+", cells[0]):     # a data row: base stage in cm
                base = int(cells[0])
                for j, val in enumerate(cells[1:]):
                    if j >= len(offsets):
                        break
                    q = _de_float(val)
                    if q is not None:
                        recs.append((base + offsets[j], q))
        if recs:                                     # found the tafel table; stop
            break
    if not recs:
        raise ValueError("no Abflusstafel matrix found on the page")
    df = (pd.DataFrame(recs, columns=["stage_cm", "discharge_m3s"])
          .drop_duplicates("stage_cm").sort_values("stage_cm").reset_index(drop=True))
    m = re.search(r"Abflusskurve\s*\(([^)]+)\)", html)
    return Rating(table=df, valid_since=_clean(m.group(1)) if m else "")


# --------------------------------------------------------------------------- #
# Streamlit GUI
# --------------------------------------------------------------------------- #
def run_app() -> None:
    import streamlit as st

    st.set_page_config(page_title="HND Bayern gauge fetch", page_icon="🌊",
                       layout="wide")
    st.title("🌊 HND Bayern gauge fetch")
    st.caption("Discharge series + stage-discharge relation from the "
               "Hochwassernachrichtendienst Bayern, exported for hydromate.")

    fetch_cached = st.cache_data(ttl=600, show_spinner=False)(fetch)

    with st.sidebar:
        st.header("Gauge")
        preset = st.selectbox("Preset", ["(paste a URL)"] + list(PRESETS))
        default_url = PRESETS.get(preset, "")
        url = st.text_input("HND gauge URL", value=default_url,
                            placeholder="https://www.hnd.bayern.de/pegel/.../<name>-<number>/abfluss")
        go = st.button("Fetch", type="primary", use_container_width=True)

    if not (go and url):
        st.info("Pick a preset or paste an HND gauge URL in the sidebar, then **Fetch**.")
        st.stop()

    try:
        gauge = parse_gauge_url(url)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    q_url = f"{gauge.base_url}/tabelle?methode=abfluss"
    w_url = f"{gauge.base_url}/tabelle?methode=wasserstand"
    tafel_url = f"{gauge.base_url}/abflusstafel"
    try:
        with st.spinner("fetching HND pages ..."):
            q_html = fetch_cached(q_url)
            tafel_html = fetch_cached(tafel_url)
            discharge = parse_timeseries(q_html, "Q")
            rating = parse_rating(tafel_html)
            try:
                stage = parse_timeseries(fetch_cached(w_url), "stage_cm")
            except Exception:                        # water-level page is optional
                stage = pd.DataFrame(columns=["datetime", "stage_cm"])
    except Exception as exc:  # noqa: BLE001 - surface any fetch/parse failure cleanly
        st.error(f"could not retrieve/parse the gauge data: "
                 f"{type(exc).__name__}: {exc}")
        st.stop()

    name = station_title(q_html) or gauge.slug
    st.subheader(f"{name}  ·  station {gauge.station_no}")
    c1, c2, c3 = st.columns(3)
    if not discharge.empty:
        last = discharge.iloc[-1]
        c1.metric("latest discharge", f"{last['Q']:.1f} m³/s",
                  help=str(last["datetime"]))
    c2.metric("rating points", f"{len(rating.table)}")
    c3.metric("rating validity", rating.valid_since or "n/a")

    tab_q, tab_r, tab_w = st.tabs(
        ["Discharge series", "Stage-discharge relation", "Water level series"])

    with tab_q:
        st.markdown(f"Source: [`{q_url}`]({q_url})")
        if discharge.empty:
            st.warning("no discharge values returned.")
        else:
            st.line_chart(discharge.set_index("datetime")["Q"],
                          y_label="Q [m³/s]", x_label="time")
            out = discharge.copy()
            out["datetime"] = out["datetime"].dt.strftime("%Y-%m-%d %H:%M")
            st.download_button(
                "⬇ boundaries.inflow CSV  (datetime,Q)",
                out.to_csv(index=False).encode("utf-8"),
                file_name=f"{gauge.slug}-abfluss.csv", mime="text/csv")
            st.dataframe(discharge, use_container_width=True, height=300)

    with tab_r:
        st.markdown(f"Source: [`{tafel_url}`]({tafel_url})  ·  "
                    f"validity: *{rating.valid_since or 'n/a'}*")
        rt = rating.table
        st.scatter_chart(rt, x="discharge_m3s", y="stage_cm",
                         x_label="Q [m³/s]", y_label="stage [cm]")
        st.markdown("**Absolute water-surface elevation.** The stage is in cm above "
                    "the gauge zero (Pegelnullpunkt), which HND does not publish. "
                    "Enter it to export a hydromate `Q,WSE` rating; leave 0 for a "
                    "stage-in-metres export relative to the gauge zero.")
        pnp = st.number_input("Pegelnullpunkt PNP [m a.s.l.]", value=0.0,
                              step=0.001, format="%.3f")
        exp = rt.copy()
        exp["stage_m"] = exp["stage_cm"] / 100.0
        if pnp:
            exp["WSE"] = pnp + exp["stage_m"]
            hbc = exp.rename(columns={"discharge_m3s": "Q"})[["Q", "WSE", "stage_cm"]]
            label = "⬇ boundaries.stage_discharge CSV  (Q,WSE)"
        else:
            hbc = exp.rename(columns={"discharge_m3s": "Q"})[["Q", "stage_m", "stage_cm"]]
            label = "⬇ rating CSV  (Q,stage_m - relative to gauge zero)"
        st.download_button(label, hbc.to_csv(index=False).encode("utf-8"),
                           file_name=f"{gauge.slug}-rating.csv", mime="text/csv")
        st.dataframe(rt, use_container_width=True, height=300)

    with tab_w:
        st.markdown(f"Source: [`{w_url}`]({w_url})")
        if stage.empty:
            st.info("no water-level series available for this gauge.")
        else:
            st.line_chart(stage.set_index("datetime")["stage_cm"],
                          y_label="stage [cm]", x_label="time")
            out = stage.copy()
            out["datetime"] = out["datetime"].dt.strftime("%Y-%m-%d %H:%M")
            st.download_button(
                "⬇ water-level CSV  (datetime,stage_cm)",
                out.to_csv(index=False).encode("utf-8"),
                file_name=f"{gauge.slug}-wasserstand.csv", mime="text/csv")
            st.dataframe(stage, use_container_width=True, height=300)


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
            "    mamba run -n hydromate-env streamlit run cases/hnd_bayern_gauge.py\n")
        return 1
    sys.argv = ["streamlit", "run", __file__, *sys.argv[1:]]
    return stcli.main()


if __name__ == "__main__":
    if _under_streamlit():
        run_app()
    else:
        raise SystemExit(_relaunch())
