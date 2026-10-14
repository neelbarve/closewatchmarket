"""Pull the Yahoo Finance earnings calendar for Oct-Nov 2026 into data/calendar_raw.csv."""
import sys, pandas as pd, yfinance as yf
from pathlib import Path
out = Path(__file__).resolve().parents[1] / "data"; out.mkdir(exist_ok=True)
frames = []
for s, e in [("2026-10-01","2026-10-16"),("2026-10-16","2026-11-01"),("2026-11-01","2026-11-16"),("2026-11-16","2026-12-01")]:
    cal = yf.Calendars(start=s, end=e); off = 0
    while True:
        df = cal.get_earnings_calendar(filter_most_active=False, limit=100, offset=off, force=True)
        if df is None or df.empty: break
        frames.append(df.reset_index()); off += 100
        if len(df) < 100: break
    print(s, e, off, file=sys.stderr)
raw = pd.concat(frames).drop_duplicates(["Symbol","Event Start Date"])
raw.to_csv(out / "calendar_raw.csv", index=False)
print(len(raw), raw["Marketcap"].describe().to_string())
