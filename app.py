"""Earnings monitor (Oct-Nov 2026).

Tabs: full earnings calendar, Investopedia holdings, one tab per user-created
portfolio, and a form to create a new portfolio from a list of tickers.

Run:  streamlit run app.py
"""
import re
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


def with_live_prices(df):
    px, _ = live_prices()
    df = df.copy()
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
st.caption(f"Prices live from Tiingo as of {_px_time:%Y-%m-%d %H:%M} ET (updates every minute on reload). "
           "Earnings and valuations: Finnhub, FMP and Yahoo, refreshed 10:00 and 18:30 ET on weekdays. "
           "Blank cells = value not available.")

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
