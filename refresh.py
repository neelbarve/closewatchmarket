"""Daily refresh of the earnings tables (run by GitHub Actions on weekdays, or by hand).

Writes, in the same format as before:
  data/calendar.csv                         every Oct-Nov earnings event (all caps)
  output/earnings_oct_nov_2026.csv          events for companies >= $2B
  output/earnings_oct_nov_2026_holdings.csv Investopedia holdings
  output/portfolios/*.csv                   user-created portfolios

Any value a provider fails to return keeps yesterday's value, so a bad API day
never blanks the tables.   Usage:  python refresh.py [--metric-limit N]
"""
import argparse
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import sources

ROOT = Path(__file__).resolve().parent
CAL_CSV = ROOT / "data/calendar.csv"
UNIVERSE_CSV = ROOT / "data/universe.csv"  # Symbol -> Name, Market Cap ($B) cache
ALL_CSV = ROOT / "output/earnings_oct_nov_2026.csv"
HOLD_CSV = ROOT / "output/earnings_oct_nov_2026_holdings.csv"
PORT_DIR = ROOT / "output/portfolios"
WINDOW = (date(2026, 10, 1), date(2026, 11, 30))
MIN_CAP_B = 2.0
MAX_NEW_PROFILES = 300  # Finnhub lookups for never-seen symbols per run
MAX_TARGETS = 110       # FMP free tier is ~250 calls/day, and we run twice a day

# Holdings read from investopedia_hedge_funds.pdf (portfolio as of 2026-10-13)
HOLDINGS = """AFRM AKAM AMAT AMD AMZN APLD ATO AVGO BITC BMY BUG CEG CIBR COP CRWD CSCO CTVA
CVX DKS DLR FHLC FTNT GE GEV GOOGL HL IREN IVV JNJ LIN LLY MCD META MGM MRK MU NET
NFLX NKE NVDA NVS PANW PIE PLTR PWR QQQ RDW SOFI T TSLA UNH USAR VLO VZ WFC WMT XOM""".split()

COLUMNS = ["Earnings Date", "Timing", "Symbol", "Name", "Event", "Current Price", "Analyst Target Price",
           "Upside to Target (%)", "EPS Estimate", "Reported EPS", "Surprise (%)", "Trailing EPS", "Forward EPS",
           "Market Cap ($B)", "Trailing P/E", "Forward P/E", "P/B"]
CARRY = ["Name", "Current Price", "Analyst Target Price", "Trailing EPS", "Forward EPS",
         "Market Cap ($B)", "Trailing P/E", "P/B"]  # per-symbol values kept from yesterday if missing today
NO_EVENT = {"ETF/fund - no earnings", "No earnings date found"}


def save(df, path):
    """Write a CSV; if it is locked (e.g. open in Excel) write a side copy instead of failing the run."""
    try:
        df.to_csv(path, index=False)
    except PermissionError:
        alt = path.with_name(path.stem + ".locked-copy.csv")
        df.to_csv(alt, index=False)
        print(f"WARNING: {path.name} is locked (open in Excel?) - wrote {alt.name} instead")


def read(path):
    return pd.read_csv(path) if path.exists() else pd.DataFrame(columns=COLUMNS)


# ------------------------------------------------------------------ calendar
def fetch_calendar():
    rows, d = [], WINDOW[0]
    while d <= WINDOW[1]:  # one call per day: Finnhub caps each response at 1500 rows
        rows += sources.earnings_calendar(d.isoformat(), d.isoformat())
        d += timedelta(days=1)
    if not rows:
        return None
    cal = pd.DataFrame(rows)
    # FMP's calendar sometimes has actual EPS before Finnhub does; use it to fill gaps
    fmp = pd.DataFrame(sources.fmp_calendar(WINDOW[0].isoformat(), WINDOW[1].isoformat()))
    if len(fmp):
        fmp = fmp.dropna(subset=["epsActual"]).drop_duplicates("symbol").set_index("symbol")["epsActual"]
        cal["epsActual"] = cal["epsActual"].fillna(cal["symbol"].map(fmp).where(cal["date"] <= date.today().isoformat()))
    cal = pd.DataFrame({
        "Earnings Date": cal["date"], "Symbol": cal["symbol"],
        "Timing": cal["hour"].str.upper().replace({"": "TAS", "DMH": "TAS"}),
        "Event": "Q" + cal["quarter"].astype(str) + " " + cal["year"].astype(str) + " Earnings Announcement",
        "EPS Estimate": cal["epsEstimate"], "Reported EPS": cal["epsActual"]})
    return cal.drop_duplicates(["Symbol", "Earnings Date"])


# ------------------------------------------------------------------ per-symbol data
def symbol_data(symbols, prev, universe, metric_limit, targets_for):
    """DataFrame indexed by Symbol with the CARRY fields, fresh where possible."""
    data = prev.set_index("Symbol")[CARRY].groupby(level=0).last() if len(prev) else pd.DataFrame(columns=CARRY)
    data = data.reindex(sorted(set(symbols)))
    names = universe.set_index("Symbol")
    data["Name"] = data["Name"].fillna(names["Name"].reindex(data.index))
    data["Market Cap ($B)"] = data["Market Cap ($B)"].fillna(names["Market Cap ($B)"].reindex(data.index))

    px = sources.prices()  # one bulk call
    data["Current Price"] = pd.Series(px).reindex(data.index).fillna(data["Current Price"])

    first = [s for s in targets_for if s in data.index]
    order = first + [s for s in data.index if s not in set(first)]  # holdings/portfolios first
    for i, sym in enumerate(order[:metric_limit]):
        for k, v in sources.metrics(sym).items():
            data.at[sym, k] = v
        if i and i % 500 == 0:
            print(f"  metrics {i}/{min(len(order), metric_limit)}", flush=True)
    # Yahoo backup for analyst target / forward EPS; stop if Yahoo starts blocking us
    fails = 0
    for i, sym in enumerate(order[:metric_limit]):
        y = sources.yahoo(sym)
        fails = 0 if y else fails + 1
        if fails >= 25:
            print(f"  yahoo: stopped after {i} symbols (blocked)")
            break
        for k, v in y.items():
            if k in ("Analyst Target Price", "Forward EPS") or pd.isna(data.at[sym, k]):
                data.at[sym, k] = v
        if i and i % 500 == 0:
            print(f"  yahoo {i}/{min(len(order), metric_limit)}", flush=True)
    # FMP for whatever still has no target, holdings/portfolios first (free tier ~250/day)
    need = [s for s in order if pd.isna(data.at[s, "Analyst Target Price"])]
    for sym in need[:MAX_TARGETS]:
        t = sources.price_target(sym)
        if t:
            data.at[sym, "Analyst Target Price"] = t
    return data


def make_table(events, data, extra_symbols=(), prev=None):
    """Join events to symbol data; add undated rows for extra_symbols; format like the original CSVs."""
    df = events.join(data, on="Symbol")
    missing = [s for s in extra_symbols if s not in set(events["Symbol"])]
    if missing:
        old = prev.set_index("Symbol")["Event"] if prev is not None and len(prev) else pd.Series(dtype=str)
        old = old[old.isin(NO_EVENT)]
        undated = pd.DataFrame({"Symbol": missing}).join(data, on="Symbol")
        undated["Event"] = [old.get(s) or ("No earnings date found" if pd.notna(undated.at[i, "Name"])
                                           else "ETF/fund - no earnings")
                            for i, s in enumerate(missing)]
        df = pd.concat([df, undated], ignore_index=True)
    df["Surprise (%)"] = ((df["Reported EPS"] - df["EPS Estimate"]) / df["EPS Estimate"].abs() * 100).where(df["EPS Estimate"].abs() > 0).round(2)
    df["Forward P/E"] = (df["Current Price"] / df["Forward EPS"]).where(df["Forward EPS"] > 0)
    df["Upside to Target (%)"] = ((df["Analyst Target Price"] / df["Current Price"] - 1) * 100).round(1)
    num = ["Current Price", "Analyst Target Price", "Trailing P/E", "Forward P/E", "P/B", "Trailing EPS"]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce").round(2)
    df = df.reindex(columns=COLUMNS)
    return df.sort_values(["Earnings Date", "Market Cap ($B)"], ascending=[True, False], na_position="last")


def portfolio_table(tickers, cal, data, prev=None):
    return make_table(cal[cal["Symbol"].isin(tickers)], data, tickers, prev)


# ------------------------------------------------------------------ main
def main(metric_limit):
    cal = fetch_calendar()
    if cal is None:  # Finnhub down: keep yesterday's calendar rather than writing empty tables
        print("calendar fetch failed - reusing previous calendar")
        cal = read(CAL_CSV)
    else:
        save(cal, CAL_CSV)
    print(f"calendar: {len(cal)} events")

    universe = read(UNIVERSE_CSV).reindex(columns=["Symbol", "Name", "Market Cap ($B)"])
    unknown = [s for s in cal["Symbol"].unique() if s not in set(universe["Symbol"])][:MAX_NEW_PROFILES]
    new = [{"Symbol": s, **sources.profile(s)} for s in unknown]
    if new:
        universe = pd.concat([universe, pd.DataFrame(new)], ignore_index=True)
        save(universe, UNIVERSE_CSV)
    print(f"universe: {len(universe)} symbols ({len(new)} new)")

    caps = universe.set_index("Symbol")["Market Cap ($B)"]
    big = cal[cal["Symbol"].map(caps).fillna(0) >= MIN_CAP_B]
    portfolios = {p: read(p)["Symbol"].dropna().unique().tolist() for p in sorted(PORT_DIR.glob("*.csv"))}
    watched = list(dict.fromkeys(HOLDINGS + [s for t in portfolios.values() for s in t]))

    prev = pd.concat([read(ALL_CSV), read(HOLD_CSV)] + [read(p) for p in portfolios], ignore_index=True)
    data = symbol_data(list(big["Symbol"]) + watched, prev, universe, metric_limit, watched)

    save(make_table(big, data), ALL_CSV)
    save(portfolio_table(HOLDINGS, cal, data, read(HOLD_CSV)), HOLD_CSV)
    for path, tickers in portfolios.items():
        save(portfolio_table(tickers, cal, data, read(path)), path)
    print(f"wrote {len(big)} events, {len(HOLDINGS)} holdings, {len(portfolios)} portfolios")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--metric-limit", type=int, default=10**6,
                    help="max Finnhub metric lookups (about 1/sec); default refreshes everything")
    main(ap.parse_args().metric_limit)
