"""Market-data providers used by the daily refresh and the dashboard.

Finnhub  - earnings calendar, P/E, P/B, market cap, EPS, company name (60 calls/min free)
Yahoo    - analyst target and forward EPS (unofficial; paced, best-effort backup)
Tiingo   - latest prices for every ticker in one bulk call
FMP      - analyst price-target consensus (~250 calls/day free, so only for small lists)

Keys come from environment variables (GitHub Actions) or .streamlit/secrets.toml (local / Streamlit Cloud).
Every call is a single attempt: on any failure it returns None/empty and the caller keeps old values.
"""
import os
import time
import tomllib
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
_SECRETS = ROOT / ".streamlit/secrets.toml"
_FILE_KEYS = tomllib.loads(_SECRETS.read_text()) if _SECRETS.exists() else {}


def key(name):
    return os.environ.get(name) or _FILE_KEYS.get(name, "")


def _get(url, **params):
    try:
        r = requests.get(url, params=params, timeout=20)
        return r.json() if r.ok else None
    except Exception:
        return None


# ------------------------------------------------------------------ Finnhub
_last_call = [0.0]


def _finnhub(path, **params):
    wait = 1.1 - (time.monotonic() - _last_call[0])  # stay under 60 calls/min
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()
    return _get(f"https://finnhub.io/api/v1/{path}", token=key("FINNHUB_KEY"), **params)


def earnings_calendar(start, end):
    """List of dicts: symbol, date, hour, quarter, year, epsEstimate, epsActual."""
    data = _finnhub("calendar/earnings", **{"from": start, "to": end}) or {}
    return data.get("earningsCalendar") or []


def metrics(symbol):
    """P/E, P/B, market cap ($B), trailing EPS - or {} if unavailable."""
    m = (_finnhub("stock/metric", symbol=symbol, metric="all") or {}).get("metric") or {}
    cap = m.get("marketCapitalization")  # Finnhub reports $ millions
    return {k: v for k, v in {
        "Trailing P/E": m.get("peTTM"), "P/B": m.get("pbQuarterly") or m.get("pbAnnual"),
        "Market Cap ($B)": round(cap / 1e3, 2) if cap else None, "Trailing EPS": m.get("epsTTM"),
    }.items() if v is not None}


def profile(symbol):
    """Company name and market cap ($B) - or {} (Finnhub has no profile for ETFs/funds)."""
    p = _finnhub("stock/profile2", symbol=symbol) or {}
    cap = p.get("marketCapitalization")
    return {k: v for k, v in {"Name": p.get("name"),
                              "Market Cap ($B)": round(cap / 1e3, 2) if cap else None}.items() if v}


# ------------------------------------------------------------------ Tiingo
def prices(symbols=None):
    """{SYMBOL: last price}. No symbols -> every ticker Tiingo covers, in one call."""
    params = {"token": key("TIINGO_KEY")}
    if symbols:
        params["tickers"] = ",".join(symbols)
    out = {}
    for row in _get("https://api.tiingo.com/iex/", **params) or []:
        p = row.get("tngoLast") or row.get("last") or row.get("prevClose")
        if row.get("ticker") and p:
            out[row["ticker"].upper()] = p
    return out


# ------------------------------------------------------------------ FMP
def fmp_calendar(start, end):
    """FMP earnings calendar rows (symbol, date, epsActual, epsEstimated) - fills gaps in Finnhub's."""
    data = _get("https://financialmodelingprep.com/stable/earnings-calendar",
                **{"from": start, "to": end, "apikey": key("FMP_KEY")})
    return data if isinstance(data, list) else []


def price_target(symbol):
    data = _get("https://financialmodelingprep.com/stable/price-target-consensus",
                symbol=symbol, apikey=key("FMP_KEY"))
    return data[0].get("targetConsensus") if isinstance(data, list) and data else None


# ------------------------------------------------------------------ Yahoo (backup)
def yahoo(symbol):
    """Analyst target, forward EPS and fallbacks for other fields - or {} on any failure."""
    try:
        import yfinance as yf
        i = yf.Ticker(symbol).info or {}
    except Exception:
        return {}
    time.sleep(0.4)  # pace requests: Yahoo blocks bursts
    return {k: v for k, v in {
        "Analyst Target Price": i.get("targetMeanPrice"), "Forward EPS": i.get("forwardEps"),
        "Trailing P/E": i.get("trailingPE"), "P/B": i.get("priceToBook"),
        "Name": i.get("shortName"), "Trailing EPS": i.get("trailingEps"),
    }.items() if v is not None}
