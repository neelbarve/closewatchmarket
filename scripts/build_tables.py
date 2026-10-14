"""Enrich the Oct-Nov earnings calendar with quote/valuation data and write
output/earnings_oct_nov_2026.csv (all caps >= $2B) and
output/earnings_oct_nov_2026_holdings.csv (Investopedia portfolio)."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pandas as pd, yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
MIN_CAP = 2e9
# Holdings read from investopedia_hedge_funds.pdf (portfolio as of 2026-10-13)
HOLDINGS = """AFRM AKAM AMAT AMD AMZN APLD ATO AVGO BITC BMY BUG CEG CIBR COP CRWD CSCO CTVA
CVX DKS DLR FHLC FTNT GE GEV GOOGL HL IREN IVV JNJ LIN LLY MCD META MGM MRK MU NET
NFLX NKE NVDA NVS PANW PIE PLTR PWR QQQ RDW SOFI T TSLA UNH USAR VLO VZ WFC WMT XOM""".split()

def info(sym):
    try:  # single attempt; anything Yahoo doesn't return is left blank
        i = yf.Ticker(sym).info or {}
    except Exception:
        i = {}
    g = lambda *k: next((i[x] for x in k if i.get(x) is not None), None)
    return {"Symbol": sym, "Name": g("shortName", "longName"), "Quote Type": g("quoteType"),
            "Current Price": g("currentPrice", "regularMarketPrice", "navPrice"),
            "Analyst Target Price": g("targetMeanPrice"), "Market Cap": g("marketCap", "totalAssets"),
            "Trailing P/E": g("trailingPE"), "Forward P/E": g("forwardPE"), "P/B": g("priceToBook"),
            "Trailing EPS": g("trailingEps"), "Forward EPS": g("forwardEps")}

def next_earnings(sym):
    try:
        d = yf.Ticker(sym).calendar.get("Earnings Date") or []
        return pd.Timestamp(d[0]) if d else pd.NaT
    except Exception:
        return pd.NaT

def fetch(syms, fn):
    with ThreadPoolExecutor(4) as ex:
        return list(ex.map(fn, syms))

def shape(df):
    df["Market Cap ($B)"] = (df.pop("Market Cap") / 1e9).round(2)
    df["Upside to Target (%)"] = ((df["Analyst Target Price"] / df["Current Price"] - 1) * 100).round(1)
    cols = ["Earnings Date", "Timing", "Symbol", "Name", "Event", "Current Price", "Analyst Target Price",
            "Upside to Target (%)", "EPS Estimate", "Reported EPS", "Surprise (%)", "Trailing EPS", "Forward EPS",
            "Market Cap ($B)", "Trailing P/E", "Forward P/E", "P/B"]
    df = df[[c for c in cols if c in df] + [c for c in df if c not in cols]]
    for c in ["Trailing P/E", "Forward P/E", "P/B", "Current Price", "Analyst Target Price"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").round(2)
    # Ranked by earnings date; undated rows (ETFs, no Oct-Nov report) go last
    return df.sort_values(["Earnings Date", "Market Cap ($B)"], ascending=[True, False], na_position="last")

raw = pd.read_csv(ROOT / "data/calendar_raw.csv")
raw["Marketcap"] = pd.to_numeric(raw["Marketcap"], errors="coerce")
raw["Earnings Date"] = pd.to_datetime(raw["Event Start Date"], utc=True).dt.tz_convert("America/New_York").dt.date
raw["Event Name"] = raw["Event Name"].fillna("Earnings Announcement")
cal = raw.rename(columns={"Event Name": "Event", "Surprise(%)": "Surprise (%)"})[
    ["Symbol", "Company", "Marketcap", "Earnings Date", "Timing", "Event", "EPS Estimate", "Reported EPS", "Surprise (%)"]]

big = cal[cal.Marketcap >= MIN_CAP]
need = sorted(set(big.Symbol) | set(HOLDINGS))
cache_file = ROOT / "data/quote_info.csv"
cached = pd.read_csv(cache_file).set_index("Symbol") if cache_file.exists() else pd.DataFrame()
good = cached[cached["Quote Type"].notna()] if len(cached) else cached
todo = [s for s in need if s not in good.index]
info_df = pd.concat([good, pd.DataFrame(fetch(todo, info)).set_index("Symbol")]) if todo else good
info_df.to_csv(cache_file)
print("quote info missing:", info_df["Quote Type"].isna().sum(), "of", len(info_df))

def merged(rows):
    df = rows.join(info_df, on="Symbol")
    df["Name"] = df["Name"].fillna(df.pop("Company"))
    df["Market Cap"] = df["Market Cap"].fillna(df.pop("Marketcap"))
    return df

out = ROOT / "output"; out.mkdir(exist_ok=True)
shape(merged(big).drop(columns="Quote Type")).to_csv(out / "earnings_oct_nov_2026.csv", index=False)

# Holdings: calendar rows where present; otherwise look up the next earnings date directly
h = cal[cal.Symbol.isin(HOLDINGS)]
missing = [s for s in HOLDINGS if s not in set(h.Symbol)]
extra = pd.DataFrame({"Symbol": missing, "Earnings Date": fetch(missing, next_earnings)})
extra["Earnings Date"] = extra["Earnings Date"].dt.date
lo, hi = pd.Timestamp("2026-10-01").date(), pd.Timestamp("2026-11-30").date()
extra["Event"] = [("ETF/fund - no earnings" if info_df.at[s, "Quote Type"] in ("ETF", "MUTUALFUND") else
                   "Next earnings outside Oct-Nov" if pd.notna(d) and not (lo <= d <= hi) else
                   "No earnings date found" if pd.isna(d) else "Earnings (from ticker calendar)")
                  for s, d in zip(extra.Symbol, extra["Earnings Date"])]
extra.loc[~extra.Event.str.startswith("Earnings"), "Earnings Date"] = None
hold = merged(pd.concat([h, extra.assign(Company=None, Marketcap=None)], ignore_index=True))
shape(hold.drop(columns="Quote Type")).to_csv(out / "earnings_oct_nov_2026_holdings.csv", index=False)
print("done", len(big), len(hold))
