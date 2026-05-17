"""Collect options IV surface data and store in per-symbol SQLite databases."""

import argparse
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ET = ZoneInfo("America/New_York")

# Trading-hours collection slots in Eastern time (10am, 12pm, 2pm)
HOURLY_SLOTS = [10, 12, 14]

DATA_DIR = Path(__file__).resolve().parent / "data"

CREATE_TABLE = """CREATE TABLE IF NOT EXISTS iv_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_time TIMESTAMP NOT NULL,
    expiration TEXT NOT NULL,
    strike REAL NOT NULL,
    option_type TEXT NOT NULL,
    iv REAL,
    last_price REAL,
    underlying_price REAL,
    delta REAL,
    gamma REAL,
    theta REAL,
    vega REAL,
    bid REAL,
    ask REAL,
    volume REAL,
    open_interest REAL
)"""

CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_lookup ON iv_snapshots(expiration, quote_time)"

COLUMNS = [
    "quote_time", "expiration", "strike", "option_type", "iv",
    "last_price", "underlying_price",
    "delta", "gamma", "theta", "vega",
    "bid", "ask", "volume", "open_interest",
]


def get_db_path(symbol: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{symbol.upper()}.db"


def init_db(db_path: Path) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(CREATE_TABLE)
        conn.execute(CREATE_INDEX)


def fetch_options(symbol: str, expiration_filters: list[str] | None = None,
                  strike_filters: list[float] | None = None) -> pd.DataFrame:
    """Fetch options chain for a symbol. Optionally filter by expiration and/or strike."""
    ticker = yf.Ticker(symbol)
    expirations = ticker.options
    if not expirations:
        print(f"  No options data found for {symbol}")
        return pd.DataFrame()

    # Filter to requested expirations
    if expiration_filters:
        matched = []
        for f in expiration_filters:
            matched += [e for e in expirations if f in e]
        matched = list(dict.fromkeys(matched))  # dedupe, preserve order
        if not matched:
            print(f"  No expiration matches filters: {expiration_filters}. Available: {', '.join(expirations[:5])}...")
            return pd.DataFrame()
        expirations = matched
        print(f"  Matched {len(expirations)} expiration(s): {', '.join(expirations)}")

    # Get underlying price once (close approximation)
    try:
        info = ticker.info
        underlying = info.get("regularMarketPreviousClose") or info.get("previousClose")
    except Exception:
        underlying = None

    quote_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []

    for exp in expirations:
        try:
            chain = ticker.option_chain(exp)
        except Exception as e:
            print(f"  Skipping {exp}: {e}")
            continue

        for opt_type, opt_df in [("call", chain.calls), ("put", chain.puts)]:
            if opt_df.empty:
                continue
            for _, row in opt_df.iterrows():
                rows.append({
                    "quote_time": quote_time,
                    "expiration": exp,
                    "strike": row["strike"],
                    "option_type": opt_type,
                    "iv": row.get("impliedVolatility"),
                    "last_price": row.get("lastPrice"),
                    "underlying_price": underlying,
                    "delta": row.get("delta"),
                    "gamma": row.get("gamma"),
                    "theta": row.get("theta"),
                    "vega": row.get("vega"),
                    "bid": row.get("bid"),
                    "ask": row.get("ask"),
                    "volume": row.get("volume"),
                    "open_interest": row.get("openInterest"),
                })

    if not rows:
        return pd.DataFrame()

    # Filter to matching strikes if requested
    if strike_filters:
        available_strikes = sorted(set(r["strike"] for r in rows))
        keep_strikes = set()
        for target in strike_filters:
            best = min(available_strikes, key=lambda s: abs(s - target))
            if abs(best - target) / target < 0.05:  # within 5%
                keep_strikes.add(best)
            else:
                print(f"  Warning: strike {target} not found (nearest: {best})")
        if not keep_strikes:
            print(f"  No matching strikes found for filters: {strike_filters}")
            return pd.DataFrame()
        rows = [r for r in rows if r["strike"] in keep_strikes]
        print(f"  Filtered to {len(keep_strikes)} strike(s): {sorted(keep_strikes)}")

    df = pd.DataFrame(rows)
    # Infer and convert dtypes properly
    for col in ["iv", "last_price", "underlying_price", "delta", "gamma", "theta", "vega", "bid", "ask", "volume", "open_interest"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")

    return df


def list_tracked() -> None:
    """List all tracked symbols and their expiration dates with status."""
    if not DATA_DIR.exists():
        print("No data directory found. Run a collection first.")
        return

    dbs = sorted(DATA_DIR.glob("*.db"))
    if not dbs:
        print("No tracked symbols. Run collect.py --symbols <TICKER> first.")
        return

    now_utc = datetime.now(timezone.utc)

    for db_path in dbs:
        symbol = db_path.stem
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("""
                SELECT
                    expiration,
                    MIN(quote_time) AS first_seen,
                    MAX(quote_time) AS last_seen,
                    COUNT(DISTINCT quote_time) AS snapshots,
                    COUNT(*) AS contracts
                FROM iv_snapshots
                GROUP BY expiration
                ORDER BY expiration
            """).fetchall()

        if not rows:
            print(f"\n{symbol}: no data")
            continue

        print(f"\n{symbol} — {len(rows)} expiration(s) tracked:")
        print(f"  {'Expiration':<12} {'Status':<9} {'Snapshots':<10} {'Contracts':<10} {'First':<20} {'Last':<20}")
        print(f"  {'-'*12} {'-'*9} {'-'*10} {'-'*10} {'-'*20} {'-'*20}")

        for exp, first, last, snaps, contracts in rows:
            exp_dt = datetime.fromisoformat(exp).replace(tzinfo=timezone.utc)
            status = "EXPIRED" if exp_dt < now_utc else "active"
            mark = "!" if status == "EXPIRED" else " "
            print(f"  {exp:<12} {mark}{status:<8} {snaps:<10} {contracts:<10} {first[:19]:<20} {last[:19]:<20}")


def save_to_db(symbol: str, df: pd.DataFrame) -> int:
    """Save snapshot rows to the symbol's database. Returns row count."""
    if df.empty:
        return 0
    db_path = get_db_path(symbol)
    init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        df[COLUMNS].to_sql("iv_snapshots", conn, if_exists="append", index=False)
    return len(df)


def collect_all(symbols: list[str], expirations: list[str] | None = None,
                strikes: list[float] | None = None) -> None:
    """Run one collection cycle for all symbols."""
    for symbol in symbols:
        sym = symbol.upper()
        parts = []
        if expirations:
            parts.append(f"exp: {', '.join(expirations)}")
        if strikes:
            parts.append(f"strikes: {', '.join(str(s) for s in strikes)}")
        label = f" ({', '.join(parts)})" if parts else ""
        print(f"[{sym}]{label} Fetching options chain...")
        try:
            df = fetch_options(sym, expiration_filters=expirations, strike_filters=strikes)
        except Exception as e:
            print(f"  Error fetching {sym}: {e}")
            continue

        if df.empty:
            print(f"  No data to save for {sym}")
            continue

        count = save_to_db(sym, df)
        expirations = df["expiration"].nunique()
        print(f"  Saved {count} contracts across {expirations} expirations → data/{sym}.db")


def next_slot_time() -> datetime:
    """Return the next collection slot (Eastern time), or None if market closed."""
    now_et = datetime.now(ET)
    today = now_et.date()

    # If weekend, jump to Monday 10am
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        days_until_mon = 7 - now_et.weekday()
        monday = today + timedelta(days=days_until_mon)
        return datetime(monday.year, monday.month, monday.day, 10, 0, 0, tzinfo=ET)

    # Find the next slot today
    for hour in HOURLY_SLOTS:
        slot = datetime(today.year, today.month, today.day, hour, 0, 0, tzinfo=ET)
        if slot > now_et:
            return slot

    # All slots passed today — next is tomorrow 10am
    tomorrow = today + timedelta(days=1)
    # Skip weekends
    if tomorrow.weekday() >= 5:
        days_until_mon = 7 - tomorrow.weekday()
        tomorrow = tomorrow + timedelta(days=days_until_mon)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, 10, 0, 0, tzinfo=ET)


def run_scheduled(symbols: list[str], expirations: list[str] | None = None,
                  strikes: list[float] | None = None) -> None:
    """Run collect_all on a schedule: trading hours, every 2 hours from 10am ET."""
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        print("\nShutting down scheduler...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"Scheduler started. Collecting {symbols} every 2 hours from 10am ET (10, 12, 2).")
    print("Press Ctrl+C to stop.\n")

    while running:
        now_et = datetime.now(ET)
        next_slot = next_slot_time()
        wait = (next_slot - now_et).total_seconds()

        # If already within a slot window (e.g. started at 10:05), run immediately
        is_weekday = now_et.weekday() < 5
        in_trading_window = any(
            abs(now_et.hour - h) < 2 for h in HOURLY_SLOTS
        )

        if is_weekday and in_trading_window and wait > 3600:
            # We're near a slot — run now instead of waiting for the next one
            pass
        elif wait > 60:
            until = next_slot.strftime("%Y-%m-%d %H:%M %Z")
            print(f"Next collection at {until} (sleeping {wait/3600:.1f}h)...")
            while running and wait > 0:
                chunk = min(wait, 300)  # sleep in 5-min chunks, check signal
                time.sleep(chunk)
                wait -= chunk
            continue

        if not running:
            break

        ts = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n=== Collection at {ts} ET ===")
        collect_all(symbols, expirations, strikes)
        print()

        # Small sleep to avoid double-fire
        time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description="Collect options IV data")
    parser.add_argument("--list", action="store_true",
                        help="List all tracked symbols and their expiration statuses")
    parser.add_argument("--symbols", nargs="+", help="Stock symbols (e.g. SPY QQQ)")
    parser.add_argument("--expirations", nargs="+", default=None,
                        help="Expiration dates YYYY-MM-DD, partial match ok (default: all expirations)")
    parser.add_argument("--strikes", type=float, nargs="+", default=None, metavar="STRIKE",
                        help="Only collect specific strikes (e.g. 590 595 600)")
    parser.add_argument("--schedule", action="store_true",
                        help="Run continuously, collecting every 2 hours from 10am ET on trading days")
    args = parser.parse_args()

    if args.list:
        list_tracked()
    elif not args.symbols:
        parser.error("--symbols is required (or use --list)")
    elif args.schedule:
        run_scheduled(args.symbols, args.expirations, args.strikes)
    else:
        collect_all(args.symbols, args.expirations, args.strikes)


if __name__ == "__main__":
    main()
