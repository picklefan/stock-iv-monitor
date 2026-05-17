# stock-iv-monitor

Collect options implied volatility (IV) data and chart IV trends over time. Uses Yahoo Finance (free, no API key).

## Setup

```bash
pip install -r requirements.txt
```

## Usage

### Collect IV data

**One-shot — full chain:**
```bash
python collect.py --symbols SPY
```

**One-shot — specific expirations:**
```bash
# Single date
python collect.py --symbols SPY --expirations 2026-06-18

# Multiple dates
python collect.py --symbols SPY --expirations 2026-06-18 2026-07-17

# Partial match (all June 2026 expirations)
python collect.py --symbols SPY --expirations 2026-06

# Multiple symbols
python collect.py --symbols SPY QQQ AAPL --expirations 2026-06-18
```

**Filter by strike — only collect specific contracts:**
```bash
python collect.py --symbols SPY --expirations 2026-06-18 --strikes 590 595 600
```
Only stores calls and puts at the given strikes (6 rows/run instead of 484).

**Scheduled — every 2 hours during trading (10am, 12pm, 2pm ET):**
```bash
# Full chain
python collect.py --symbols SPY --expirations 2026-06-18 --schedule

# VPS-friendly: specific strikes only (~1 KB/run)
python collect.py --symbols SPY --expirations 2026-06-18 --strikes 590 595 600 --schedule
```
Runs continuously. Skips weekends. Press Ctrl+C to stop.

### List tracked expirations

```bash
python collect.py --list
```

Output:
```
SPY — 3 expiration(s) tracked:
  Expiration   Status    Snapshots  Contracts  First                 Last
  ------------ --------- ---------- ---------- --------------------  --------------------
  2026-05-18   !EXPIRED  12         484        2026-05-15T14:00:02   2026-06-16T14:00:05
  2026-06-18    active   9          484        2026-06-01T14:00:03   2026-06-16T14:00:05
  2026-07-17    active   5          360        2026-06-10T14:00:01   2026-06-16T14:00:05
```

### Chart IV trends

```bash
# Nearest expiration, last 7 days (all strikes)
python chart.py --symbol SPY

# Specific expiration, 30-day lookback
python chart.py --symbol SPY --expiration 2026-06-18 --days 30

# Top 5 strikes by open interest (most liquid)
python chart.py --symbol SPY --expiration 2026-06-18 --top-n 5

# Only specific strikes
python chart.py --symbol SPY --expiration 2026-06-18 --strikes 590 595 600

# Custom output path
python chart.py --symbol SPY --expiration 2026-06-18 --top-n 5 --output my_chart.html
```

Generates an interactive HTML chart with three panels:
1. **Call IV trend** — IV over time, one line per strike
2. **Put IV trend** — IV over time, one line per strike
3. **IV skew** — IV vs strike at the latest snapshot

Use `--top-n` or `--strikes` to reduce chart clutter when tracking many strikes.

## Typical workflow

```bash
# Start tracking specific contracts on a VPS
python collect.py --symbols SPY --expirations 2026-06-18 --strikes 590 595 600 --schedule

# Check progress in another terminal
python collect.py --list

# After a few days, chart the IV trend for those strikes
python chart.py --symbol SPY --expiration 2026-06-18 --strikes 590 595 600 --days 30
```

## Data storage

Each symbol gets its own SQLite database at `data/<TICKER>.db`. Charts output to `charts/<TICKER>_<EXP>.html`.
