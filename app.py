"""Earnings monitor (Oct-Nov 2026).

Tabs: full earnings calendar, Investopedia holdings, one tab per user-created
portfolio, and a form to create a new portfolio from a list of tickers.

Run:  streamlit run app.py
"""
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import streamlit as st

import refresh
import sources

ROOT = Path(__file__).resolve().parent
ALL_CSV = ROOT / "output/earnings_oct_nov_2026.csv"
HOLD_CSV = ROOT / "output/earnings_oct_nov_2026_holdings.csv"
PORT_DIR = ROOT / "output/portfolios"  # one CSV per user portfolio, same format as HOLD_CSV
WINDOW = (pd.Timestamp("2026-10-01").date(), pd.Timestamp("2026-11-30").date())

st.set_page_config(page_title="Earnings Monitor", layout="wide")


@st.cache_data
def load(path, mtime):  # mtime busts the cache when the file changes
    return pd.read_csv(path, parse_dates=["Earnings Date"])


def read(path):
    return load(str(path), path.stat().st_mtime)


COLUMNS = read(HOLD_CSV).columns.tolist()


@st.cache_data(ttl=60)
def live_prices():
    """Latest Tiingo prices for every ticker (one call, refreshed at most once a minute)."""
    return sources.prices(), pd.Timestamp.now(tz="America/New_York")


def last_refresh():
    """When targets/valuations were last refreshed (ET), from the stamp the refresh job writes."""
    try:
        return pd.Timestamp(refresh.STAMP.read_text().strip()).tz_convert("America/New_York")
    except Exception:
        return pd.Timestamp(ALL_CSV.stat().st_mtime, unit="s", tz="UTC").tz_convert("America/New_York")


MAX_LIVE_TARGETS = 150  # on-demand target refresh is limited to what's shown, to stay fast and polite to Yahoo


def refresh_targets(symbols):
    """Fetch the latest analyst targets now and keep them for this browser session."""
    with ThreadPoolExecutor(4) as ex:
        got = dict(zip(symbols, ex.map(sources.yahoo_target, symbols)))
    got = {s: t for s, t in got.items() if t}
    st.session_state.setdefault("live_targets", {}).update(got)
    return len(got)


def with_live_prices(df):
    px, _ = live_prices()
    df = df.copy()
    df["Analyst Target Price"] = df["Symbol"].map(st.session_state.get("live_targets", {})).fillna(
        df["Analyst Target Price"]).round(2)
    df["Current Price"] = df["Symbol"].map(px).fillna(df["Current Price"]).round(2)
    df["Upside to Target (%)"] = ((df["Analyst Target Price"] / df["Current Price"] - 1) * 100).round(1)
    df["Forward P/E"] = (df["Current Price"] / df["Forward EPS"]).where(df["Forward EPS"] > 0).round(2)
    return df


def show(df, key):
    """Summary metrics, filters, table and CSV download for one earnings table."""
    df = with_live_prices(df)
    today = pd.Timestamp.today().normalize()
    dated = df["Earnings Date"].notna()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tickers", df["Symbol"].nunique())
    c2.metric("Reporting Oct-Nov", int(dated.sum()))
    c3.metric("Already reported", int((dated & (df["Earnings Date"] < today)).sum()))
    c4.metric("Next 7 days", int((dated & df["Earnings Date"].between(today, today + pd.Timedelta(days=7))).sum()))

    f1, f2, f3 = st.columns([2, 2, 1])
    search = f1.text_input("Search ticker or name", key=f"{key}_q").strip().lower()
    lo, hi = WINDOW
    rng = f2.date_input("Earnings date range", (lo, hi), min_value=lo, max_value=hi, key=f"{key}_d")
    upcoming = f3.checkbox("Upcoming only", key=f"{key}_u")

    # Range filters on any numeric columns; min/max default to each column's full range
    numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and df[c].notna().any()]
    cols = st.multiselect("Filter columns by range", numeric, key=f"{key}_rc")
    ranges = {}
    for col in cols:
        r1, r2, r3 = st.columns([2, 1, 1])
        r1.markdown(f"<div style='padding-top:2.1rem'><b>{col}</b></div>", unsafe_allow_html=True)
        cmin, cmax = float(df[col].min()), float(df[col].max())
        ranges[col] = (r2.number_input("Min", value=cmin, key=f"{key}_{col}_min"),
                       r3.number_input("Max", value=cmax, key=f"{key}_{col}_max"))

    view = df
    for col, (vmin, vmax) in ranges.items():  # rows blank in a filtered column are hidden
        view = view[view[col].between(vmin, vmax)]
    if search:
        view = view[view["Symbol"].str.lower().str.contains(search, regex=False)
                    | view["Name"].fillna("").str.lower().str.contains(search, regex=False)]
    if isinstance(rng, tuple) and len(rng) == 2:
        d = view["Earnings Date"].dt.date
        view = view[d.isna() | d.between(*rng)]
    if upcoming:
        view = view[view["Earnings Date"].isna() | (view["Earnings Date"] >= today)]

    syms = [s for s in view.loc[view["Event"] != "ETF/fund - no earnings", "Symbol"].dropna().unique()]
    b1, b2 = st.columns([1, 3])
    too_many = len(syms) > MAX_LIVE_TARGETS
    if b1.button("Refresh analyst targets", key=f"{key}_tgt", disabled=not syms or too_many,
                 help=f"Filter to {MAX_LIVE_TARGETS} or fewer tickers first" if too_many else
                 "Fetch the latest analyst targets for the tickers shown"):
        with st.spinner(f"Fetching latest targets for {len(syms)} tickers..."):
            n = refresh_targets(syms)
        st.session_state[f"{key}_tgt_msg"] = (n, len(syms), pd.Timestamp.now(tz="America/New_York"))
        st.rerun()
    if f"{key}_tgt_msg" in st.session_state:
        n, total, when = st.session_state[f"{key}_tgt_msg"]
        (b2.success if n else b2.warning)(
            f"Targets updated for {n} of {total} tickers at {when:%H:%M} ET." if n else
            "Couldn't reach the target source just now - showing targets from the last scheduled refresh.")
    elif too_many:
        b2.caption(f"Showing {len(syms)} tickers - filter to {MAX_LIVE_TARGETS} or fewer to refresh targets live.")

    st.dataframe(view, hide_index=True, use_container_width=True, height=560,
                 column_config={"Earnings Date": st.column_config.DateColumn(format="YYYY-MM-DD")})
    st.download_button("Download CSV", view.to_csv(index=False, date_format="%Y-%m-%d"),
                       f"{key}.csv", "text/csv", key=f"{key}_dl")


def build_portfolio(tickers):
    """Same rows/columns as the holdings CSV, built with the daily-refresh code."""
    universe = refresh.read(refresh.UNIVERSE_CSV)
    data = refresh.symbol_data(tickers, pd.DataFrame(columns=refresh.COLUMNS), universe,
                               metric_limit=len(tickers), targets_for=tickers)
    return refresh.portfolio_table(tickers, refresh.read(refresh.CAL_CSV), data)


def parse_tickers(text):
    return list(dict.fromkeys(t.upper() for t in re.split(r"[\s,;]+", text) if t))


def slug(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip()).strip("_")


# ---------------------------------------------------------------- layout
st.title("Earnings Monitor - Oct/Nov 2026")
_px, _px_time = live_prices()
_ref = last_refresh()
_age_h = (pd.Timestamp.now(tz="America/New_York") - _ref).total_seconds() / 3600
st.caption(f"Prices: live from Tiingo as of {_px_time:%Y-%m-%d %H:%M} ET (reload to update; upside % uses the live price). "
           f"Analyst targets, earnings and valuations: last refreshed {_ref:%Y-%m-%d %H:%M} ET ({_age_h:.0f}h ago) "
           "from Finnhub, FMP and Yahoo - use 'Refresh analyst targets' for the latest. Blank cells = not available.")

PORT_DIR.mkdir(parents=True, exist_ok=True)
portfolios = sorted(PORT_DIR.glob("*.csv"))
tabs = st.tabs(["All earnings (≥ $2B)", "Investopedia holdings"]
               + [p.stem.replace("_", " ") for p in portfolios] + ["➕ New portfolio"])

with tabs[0]:
    show(read(ALL_CSV), "all")
with tabs[1]:
    show(read(HOLD_CSV), "holdings")

for tab, path in zip(tabs[2:-1], portfolios):
    with tab:
        df = read(path)
        show(df, path.stem)
        with st.expander("Edit or delete this portfolio"):
            current = " ".join(df["Symbol"].drop_duplicates())
            new = st.text_area("Tickers", current, key=f"{path.stem}_edit")
            b1, b2 = st.columns(2)
            if b1.button("Save & rebuild", key=f"{path.stem}_save"):
                with st.spinner("Fetching data..."):
                    build_portfolio(parse_tickers(new)).to_csv(path, index=False)
                st.rerun()
            if b2.button("Delete portfolio", key=f"{path.stem}_del"):
                path.unlink()
                st.rerun()

with tabs[-1]:
    st.subheader("Create a portfolio")
    st.write("Enter a name and the tickers (separated by spaces, commas or new lines). "
             "A new tab is created in the same format as the holdings tab.")
    with st.form("new_portfolio"):
        name = st.text_input("Portfolio name")
        text = st.text_area("Tickers", placeholder="AAPL MSFT NVDA")
        submitted = st.form_submit_button("Create")
    if submitted:
        tickers, fname = parse_tickers(text), slug(name)
        if not fname or not tickers:
            st.error("Please enter a name and at least one ticker.")
        elif (PORT_DIR / f"{fname}.csv").exists():
            st.error(f"A portfolio named '{name}' already exists.")
        else:
            with st.spinner(f"Fetching data for {len(tickers)} tickers..."):
                build_portfolio(tickers).to_csv(PORT_DIR / f"{fname}.csv", index=False)
            st.rerun()
