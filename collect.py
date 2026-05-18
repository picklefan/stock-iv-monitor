"""Collect options IV surface data and store in per-symbol SQLite databases."""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

ET = ZoneInfo("America/New_York")

# Trading-hours collection slots in Eastern time (10am, 12pm, 2pm)
HOURLY_SLOTS = [10, 12, 14, 16]

# Yahoo rate limits are roughly ~360 req/hour but stricter in practice.
# Exponential backoff retry on 429 errors with these delays:
RETRY_DELAYS = [10, 30, 90, 300]  # seconds — Yahoo cooldown can be 5-15 min

# Shared session with browser-like headers to reduce chance of being blocked
_SHARED_SESSION: requests.Session | None = None


def _is_rate_limit(error: Exception) -> bool:
    """Check if an error is a Yahoo rate limit (429 or related)."""
    msg = str(error).lower()
    return any(kw in msg for kw in ("rate limit", "too many request", "429"))


def get_session() -> requests.Session:
    """Return a requests Session with browser-mimicking headers."""
    global _SHARED_SESSION
    if _SHARED_SESSION is None:
        _SHARED_SESSION = requests.Session()
        _SHARED_SESSION.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        })
        # Optionally use curl_cffi for TLS fingerprinting if installed
        try:
            from curl_cffi import requests as curl_requests
            _SHARED_SESSION = curl_requests.Session(impersonate="chrome")
        except ImportError:
            pass
    return _SHARED_SESSION

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
    """Fetch options chain for a symbol, with exponential backoff on rate limits."""
    session = get_session()
    ticker = yf.Ticker(symbol, session=session)

    # --- Get expirations with retry ---
    for attempt, delay in enumerate(RETRY_DELAYS + [None]):
        try:
            expirations = ticker.options
            break
        except Exception as e:
            if _is_rate_limit(e) and delay is not None:
                print(f"  Rate limited getting expirations, retrying in {delay}s...")
                time.sleep(delay)
            elif delay is None:
                raise
    else:
        raise RuntimeError(f"Failed to get expirations for {symbol} after retries")
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

    # Get underlying price once (close approximation), with retry
    underlying = None
    for attempt, delay in enumerate(RETRY_DELAYS + [None]):
        try:
            info = ticker.info
            underlying = info.get("regularMarketPreviousClose") or info.get("previousClose")
            break
        except Exception as e:
            if _is_rate_limit(e) and delay is not None:
                time.sleep(delay)
            elif delay is None:
                raise
            # non-rate-limit error: accept None

    quote_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []

    for exp in expirations:
        chain = None
        for attempt, delay in enumerate(RETRY_DELAYS + [None]):
            try:
                chain = ticker.option_chain(exp)
                break
            except Exception as e:
                if _is_rate_limit(e) and delay is not None:
                    print(f"  Rate limited on {exp}, retrying in {delay}s...")
                    time.sleep(delay)
                elif delay is None:
                    print(f"  Skipping {exp}: {e}")
                else:
                    print(f"  Skipping {exp}: {e}")
                    break
        if chain is None:
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


def show_history() -> None:
    """Show collection timestamps for each tracked ticker."""
    if not DATA_DIR.exists():
        print("No data directory found. Run a collection first.")
        return

    dbs = sorted(DATA_DIR.glob("*.db"))
    if not dbs:
        print("No tracked symbols.")
        return

    for db_path in dbs:
        symbol = db_path.stem
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("""
                SELECT DISTINCT quote_time
                FROM iv_snapshots
                ORDER BY quote_time
            """).fetchall()

        if not rows:
            print(f"\n{symbol}: no data")
            continue

        times = [datetime.fromisoformat(r[0]) for r in rows]
        print(f"\n{symbol} — {len(times)} collection(s):")
        for t in times:
            et = t.astimezone(ET)
            delta = datetime.now(ET) - et
            ago = _format_ago(delta)
            print(f"  {et.strftime('%Y-%m-%d %H:%M %Z')} ({ago})")


def _format_ago(delta: timedelta) -> str:
    if delta.days > 1:
        return f"{delta.days}d ago"
    elif delta.days == 1:
        return "1d ago"
    hours = delta.seconds // 3600
    if hours > 1:
        return f"{hours}h ago"
    elif hours == 1:
        return "1h ago"
    mins = delta.seconds // 60
    if mins > 1:
        return f"{mins}m ago"
    return "just now"


def save_to_db(symbol: str, df: pd.DataFrame) -> int:
    """Save snapshot rows to the symbol's database. Returns row count."""
    if df.empty:
        return 0
    db_path = get_db_path(symbol)
    init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        df[COLUMNS].to_sql("iv_snapshots", conn, if_exists="append", index=False)
    return len(df)


def load_config(path: str) -> list[dict]:
    """Load monitoring config from a JSON file.
    Format: {"symbols": [{"ticker": "SPY", "expirations": ["2026-06-18"], "strikes": [590, 600]}]}
    """
    with open(path) as f:
        config = json.load(f)
    if "symbols" not in config:
        raise ValueError("Config must contain a 'symbols' key")
    return config["symbols"]


def show_config(path: str) -> None:
    """Print a summary of what's defined in a config file."""
    entries = load_config(path)
    est_per_run = 0
    print(f"Config: {path}")
    print(f"  {len(entries)} symbol(s) defined:\n")
    for entry in entries:
        ticker = entry["ticker"]
        exps = entry.get("expirations") or ["ALL"]
        strikes = entry.get("strikes") or ["ALL"]
        n_strikes = len(strikes) if strikes != ["ALL"] else "?"
        contracts_per_run = 0 if strikes == ["ALL"] or n_strikes == "?" else int(n_strikes) * len(exps) * 2
        est_per_run += contracts_per_run
        print(f"  {ticker}")
        print(f"    Expirations: {', '.join(exps)}")
        print(f"    Strikes:     {', '.join(str(s) for s in strikes)}")
        if contracts_per_run:
            print(f"    Est/run:     ~{contracts_per_run} contracts ({int(n_strikes)} strikes × {len(exps)} exp(s) × 2 types)")
        else:
            print(f"    Est/run:     unknown (all strikes)")
        print()
    if est_per_run:
        print(f"  Total estimated: ~{est_per_run} contracts/run")
    print(f"\n  Run with: python collect.py --config {path}")


def collect_all(symbols: list[str], expirations: list[str] | None = None,
                strikes: list[float] | None = None, delay: float = 10.0) -> None:
    """Run one collection cycle for all symbols."""
    for i, symbol in enumerate(symbols):
        if i > 0:
            time.sleep(delay)  # avoid Yahoo rate limiting
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


def run_scheduled(entries: list[dict], delay: float = 10.0) -> None:
    """Run collect_from_config at fixed times: 10am, 12pm, 2pm, 4pm ET on trading days."""
    tickers = [e["ticker"] for e in entries]
    slots_str = ", ".join(f"{h}:00" for h in HOURLY_SLOTS)
    print(f"Scheduler started. Collecting {tickers} at {slots_str} ET on trading days.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            now_et = datetime.now(ET)
            next_slot = next_slot_time()
            wait = (next_slot - now_et).total_seconds()

            if wait > 60:
                until = next_slot.strftime("%Y-%m-%d %H:%M %Z")
                print(f"Next collection at {until} (sleeping {wait/3600:.1f}h)...")
                while wait > 0:
                    time.sleep(min(wait, 2))
                    wait -= 2

            ts = datetime.now(ET).strftime("%H:%M:%S")
            print(f"\n=== Collection at {ts} ET ===")
            collect_from_config(entries, delay=delay)
            print()

            time.sleep(2)

    except KeyboardInterrupt:
        print("\nShutting down scheduler...")


def collect_from_config(entries: list[dict], delay: float = 10.0) -> None:
    """Collect all symbols defined in config entries."""
    for i, entry in enumerate(entries):
        if i > 0:
            time.sleep(delay)
        ticker = entry["ticker"]
        exps = entry.get("expirations")
        strikes = entry.get("strikes")
        collect_all([ticker], exps, strikes, delay=0)  # delay handled here


def main():
    parser = argparse.ArgumentParser(description="Collect options IV data")
    parser.add_argument("--list", action="store_true",
                        help="List tracked symbols from the database with active/expired status")
    parser.add_argument("--history", action="store_true",
                        help="Show collection timestamps for each tracked ticker")
    parser.add_argument("--show-config", default=None, metavar="CONFIG",
                        help="Show what a config file will track (no fetching)")
    parser.add_argument("--config", default=None,
                        help="JSON config file with symbols, expirations, and strikes")
    parser.add_argument("--symbols", nargs="+", help="Stock symbols (e.g. SPY QQQ)")
    parser.add_argument("--expirations", nargs="+", default=None,
                        help="Expiration dates YYYY-MM-DD, partial match ok (default: all expirations)")
    parser.add_argument("--strikes", type=float, nargs="+", default=None, metavar="STRIKE",
                        help="Only collect specific strikes (e.g. 590 595 600)")
    parser.add_argument("--schedule", action="store_true",
                        help="Run continuously, collecting every 2 hours from 10am ET on trading days")
    parser.add_argument("--delay", type=float, default=10.0, metavar="SECS",
                        help="Delay between symbols to avoid rate limits (default: 10s)")
    args = parser.parse_args()

    if args.list:
        list_tracked()
        return

    if args.history:
        show_history()
        return

    if args.show_config:
        show_config(args.show_config)
        return

    if args.config:
        entries = load_config(args.config)
    elif args.symbols:
        entries = [{"ticker": s, "expirations": args.expirations, "strikes": args.strikes}
                   for s in args.symbols]
    else:
        parser.error("--symbols or --config is required (or use --list)")

    if args.schedule:
        run_scheduled(entries, delay=args.delay)
    else:
        collect_from_config(entries, delay=args.delay)


if __name__ == "__main__":
    main()
