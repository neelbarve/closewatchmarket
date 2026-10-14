"""Earnings monitor (Oct-Nov 2026).

Tabs: full earnings calendar, Investopedia holdings, one tab per user-created
portfolio, and a form to create a new portfolio from a list of tickers.

Run:  streamlit run app.py
"""
import re
from pathlib import Path

import pandas as pd
import streamlit as st
import yfinance as yf

ROOT = Path(__file__).resolve().parent
ALL_CSV = ROOT / "output/earnings_oct_nov_2026.csv"
HOLD_CSV = ROOT / "output/earnings_oct_nov_2026_holdings.csv"
RAW_CSV = ROOT / "data/calendar_raw.csv"
INFO_CSV = ROOT / "data/quote_info.csv"
PORT_DIR = ROOT / "output/portfolios"  # one CSV per user portfolio, same format as HOLD_CSV
WINDOW = (pd.Timestamp("2026-10-01").date(), pd.Timestamp("2026-11-30").date())

st.set_page_config(page_title="Earnings Monitor", layout="wide")


@st.cache_data
def load(path, mtime):  # mtime busts the cache when the file changes
    return pd.read_csv(path, parse_dates=["Earnings Date"])


def read(path):
    return load(str(path), path.stat().st_mtime)


COLUMNS = read(HOLD_CSV).columns.tolist()


def show(df, key):
    """Summary metrics, filters, table and CSV download for one earnings table."""
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

    view = df
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


def quote(sym, cache):
    """Quote/valuation fields from the cache, else one yfinance attempt (blank on failure)."""
    if sym in cache.index and pd.notna(cache.at[sym, "Quote Type"]):
        return cache.loc[sym].to_dict()
    try:
        i = yf.Ticker(sym).info or {}
    except Exception:
        i = {}
    g = lambda *k: next((i[x] for x in k if i.get(x) is not None), None)
    return {"Name": g("shortName", "longName"), "Quote Type": g("quoteType"),
            "Current Price": g("currentPrice", "regularMarketPrice", "navPrice"),
            "Analyst Target Price": g("targetMeanPrice"), "Market Cap": g("marketCap", "totalAssets"),
            "Trailing P/E": g("trailingPE"), "Forward P/E": g("forwardPE"), "P/B": g("priceToBook"),
            "Trailing EPS": g("trailingEps"), "Forward EPS": g("forwardEps")}


def build_portfolio(tickers):
    """Same rows/columns as the holdings CSV, for an arbitrary ticker list."""
    raw = pd.read_csv(RAW_CSV)
    raw["Earnings Date"] = (pd.to_datetime(raw["Event Start Date"], utc=True)
                            .dt.tz_convert("America/New_York").dt.date)
    cache = pd.read_csv(INFO_CSV).set_index("Symbol") if INFO_CSV.exists() else pd.DataFrame()
    rows = []
    for sym in tickers:
        q = quote(sym, cache)
        events = raw[raw["Symbol"] == sym]
        base = {"Symbol": sym, "Name": q.get("Name"),
                "Current Price": q.get("Current Price"), "Analyst Target Price": q.get("Analyst Target Price"),
                "Market Cap ($B)": round(q["Market Cap"] / 1e9, 2) if pd.notna(q.get("Market Cap")) else None,
                **{k: q.get(k) for k in ["Trailing P/E", "Forward P/E", "P/B", "Trailing EPS", "Forward EPS"]}}
        if len(events):
            for _, e in events.iterrows():
                rows.append({**base, "Earnings Date": e["Earnings Date"], "Timing": e["Timing"],
                             "Event": e["Event Name"] if pd.notna(e["Event Name"]) else "Earnings Announcement",
                             "Name": base["Name"] or e["Company"],
                             "Market Cap ($B)": base["Market Cap ($B)"] or (round(e["Marketcap"] / 1e9, 2) if pd.notna(e["Marketcap"]) else None),
                             "EPS Estimate": e["EPS Estimate"], "Reported EPS": e["Reported EPS"],
                             "Surprise (%)": e["Surprise(%)"]})
            continue
        date, event = None, "No earnings date found"
        if q.get("Quote Type") in ("ETF", "MUTUALFUND"):
            event = "ETF/fund - no earnings"
        else:
            try:
                d = yf.Ticker(sym).calendar.get("Earnings Date") or []
                d = pd.Timestamp(d[0]).date() if d else None
            except Exception:
                d = None
            if d and WINDOW[0] <= d <= WINDOW[1]:
                date, event = d, "Earnings (from ticker calendar)"
            elif d:
                event = f"Next earnings {d} (outside Oct-Nov)"
        rows.append({**base, "Earnings Date": date, "Event": event})

    df = pd.DataFrame(rows).reindex(columns=COLUMNS)
    num = ["Current Price", "Analyst Target Price", "Trailing P/E", "Forward P/E", "P/B"]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce").round(2)
    df["Upside to Target (%)"] = ((df["Analyst Target Price"] / df["Current Price"] - 1) * 100).round(1)
    return df.sort_values(["Earnings Date", "Market Cap ($B)"], ascending=[True, False], na_position="last")


def parse_tickers(text):
    return list(dict.fromkeys(t.upper() for t in re.split(r"[\s,;]+", text) if t))


def slug(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip()).strip("_")


# ---------------------------------------------------------------- layout
st.title("Earnings Monitor - Oct/Nov 2026")
st.caption("Source: Yahoo Finance earnings calendar via yfinance. Blank cells = value not returned by Yahoo.")

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
