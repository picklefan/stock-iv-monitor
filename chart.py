"""Generate IV trend and skew charts from collected options data."""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

DATA_DIR = Path(__file__).resolve().parent / "data"


def load_data(symbol: str, lookback_days: int) -> pd.DataFrame:
    db_path = DATA_DIR / f"{symbol.upper()}.db"
    if not db_path.exists():
        raise FileNotFoundError(f"No data file found: {db_path}")

    with sqlite3.connect(str(db_path)) as conn:
        df = pd.read_sql_query("SELECT * FROM iv_snapshots", conn)

    df["quote_time"] = pd.to_datetime(df["quote_time"], utc=True)
    df["expiration"] = pd.to_datetime(df["expiration"], utc=True)
    df["strike"] = pd.to_numeric(df["strike"])
    df["iv"] = pd.to_numeric(df["iv"], errors="coerce")

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    df = df[df["quote_time"] >= cutoff]
    return df


def pick_expiration(df: pd.DataFrame, expiration_arg: str | None) -> pd.Timestamp:
    exps = sorted(df["expiration"].unique())
    if not len(exps):
        raise ValueError("No expirations found in data")

    if expiration_arg:
        target = pd.Timestamp(expiration_arg, tz="utc")
        # Find closest match
        best = min(exps, key=lambda e: abs((e - target).total_seconds()))
        return best

    # Default: nearest expiration that hasn't expired yet (or the earliest available)
    now = datetime.now(timezone.utc)
    future = [e for e in exps if e >= now]
    return future[0] if future else exps[-1]


def resolve_strikes(exp_df: pd.DataFrame, top_n: int | None, strikes_arg: list[float] | None) -> list[float]:
    """Return the subset of strikes to plot based on filters."""
    all_strikes = sorted(exp_df["strike"].unique())

    if strikes_arg:
        # Find closest matches to requested strikes
        result = []
        for s in strikes_arg:
            best = min(all_strikes, key=lambda x: abs(x - s))
            if abs(best - s) / s < 0.05:  # within 5%
                result.append(best)
            else:
                print(f"  Warning: strike {s} not found (nearest: {best})")
        return sorted(set(result))

    if top_n:
        latest = exp_df[exp_df["quote_time"] == exp_df["quote_time"].max()]
        oi_by_strike = latest.groupby("strike")["open_interest"].sum()
        return sorted(oi_by_strike.nlargest(top_n).index.tolist())

    return all_strikes


def build_chart(df: pd.DataFrame, symbol: str, expiration: pd.Timestamp,
                top_n: int | None = None, strikes_arg: list[float] | None = None) -> go.Figure:
    exp_df = df[df["expiration"] == expiration].copy()
    if exp_df.empty:
        raise ValueError(f"No data for expiration {expiration.date()}")

    strikes = resolve_strikes(exp_df, top_n, strikes_arg)

    underlying = exp_df["underlying_price"].dropna()
    spot = float(underlying.iloc[-1]) if len(underlying) else float(strikes[len(strikes) // 2])

    calls_df = exp_df[exp_df["option_type"] == "call"]
    puts_df = exp_df[exp_df["option_type"] == "put"]

    fig = make_subplots(
        rows=3, cols=1,
        subplot_titles=(
            f"{symbol} — Call IV Trend (exp {expiration.date()})",
            f"{symbol} — Put IV Trend (exp {expiration.date()})",
            f"{symbol} — IV Skew (latest snapshot)",
        ),
        vertical_spacing=0.10,
    )

    # --- Calls IV trend ---
    for strike in strikes:
        sub = calls_df[calls_df["strike"] == strike].sort_values("quote_time")
        if sub.empty:
            continue
        pct = (strike - spot) / spot * 100
        label = f"{strike:.0f} ({pct:+.0f}%)"
        fig.add_trace(
            go.Scatter(
                x=sub["quote_time"], y=sub["iv"], mode="lines+markers",
                name=label, legendgroup=f"call_{strike}", showlegend=False,
                hovertemplate=f"Strike {strike:.0f}<br>IV: %{{y:.2%}}<br>%{{x}}<extra></extra>",
            ),
            row=1, col=1,
        )

    # --- Puts IV trend ---
    for strike in strikes:
        sub = puts_df[puts_df["strike"] == strike].sort_values("quote_time")
        if sub.empty:
            continue
        pct = (strike - spot) / spot * 100
        label = f"{strike:.0f} ({pct:+.0f}%)"
        fig.add_trace(
            go.Scatter(
                x=sub["quote_time"], y=sub["iv"], mode="lines+markers",
                name=label, legendgroup=f"put_{strike}", showlegend=False,
                hovertemplate=f"Strike {strike:.0f}<br>IV: %{{y:.2%}}<br>%{{x}}<extra></extra>",
            ),
            row=2, col=1,
        )

    # --- IV Skew (latest snapshot) ---
    latest_time = exp_df["quote_time"].max()
    latest = exp_df[exp_df["quote_time"] == latest_time]
    latest_calls = latest[latest["option_type"] == "call"].sort_values("strike")
    latest_puts = latest[latest["option_type"] == "put"].sort_values("strike")

    if not latest_calls.empty:
        fig.add_trace(
            go.Scatter(
                x=latest_calls["strike"], y=latest_calls["iv"],
                mode="lines+markers", name="Calls", line=dict(color="blue"),
                hovertemplate="Strike %{x:.0f}<br>IV: %{y:.2%}<extra></extra>",
            ),
            row=3, col=1,
        )
    if not latest_puts.empty:
        fig.add_trace(
            go.Scatter(
                x=latest_puts["strike"], y=latest_puts["iv"],
                mode="lines+markers", name="Puts", line=dict(color="red"),
                hovertemplate="Strike %{x:.0f}<br>IV: %{y:.2%}<extra></extra>",
            ),
            row=3, col=1,
        )

    # Add vertical line at spot price on skew chart
    fig.add_vline(x=spot, line_dash="dash", line_color="gray", row=3, col=1,
                  annotation_text=f"Spot {spot:.2f}")

    # Format axes
    fig.update_yaxes(title_text="IV", tickformat=".0%", row=1, col=1)
    fig.update_yaxes(title_text="IV", tickformat=".0%", row=2, col=1)
    fig.update_yaxes(title_text="IV", tickformat=".0%", row=3, col=1)
    fig.update_xaxes(title_text="Time", row=1, col=1)
    fig.update_xaxes(title_text="Time", row=2, col=1)
    fig.update_xaxes(title_text="Strike", row=3, col=1)

    fig.update_layout(
        title_text=f"{symbol} Options IV Surface — Expiration {expiration.date()}",
        height=1000,
        hovermode="x unified",
    )

    return fig


def main():
    parser = argparse.ArgumentParser(description="Chart options IV trends")
    parser.add_argument("--symbol", required=True, help="Stock symbol (e.g. SPY)")
    parser.add_argument("--expiration", default=None, help="Expiration date YYYY-MM-DD (default: nearest)")
    parser.add_argument("--days", type=int, default=7, help="Lookback in days (default: 7)")
    parser.add_argument("--top-n", type=int, default=None, metavar="N",
                        help="Only show top N strikes by open interest")
    parser.add_argument("--strikes", type=float, nargs="+", default=None, metavar="STRIKE",
                        help="Only show specific strikes (e.g. 590 595 600)")
    parser.add_argument("--output", default=None, help="Output HTML file path (default: charts/<SYMBOL>_<EXP>.html)")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    print(f"Loading data for {symbol} (last {args.days} days)...")
    df = load_data(symbol, args.days)

    if df.empty:
        print("No data found in lookback window.")
        return

    expiration = pick_expiration(df, args.expiration)
    print(f"Using expiration: {expiration.date()}")

    fig = build_chart(df, symbol, expiration, top_n=args.top_n, strikes_arg=args.strikes)

    if args.output:
        out_path = Path(args.output)
    else:
        charts_dir = Path(__file__).resolve().parent / "charts"
        charts_dir.mkdir(exist_ok=True)
        out_path = charts_dir / f"{symbol}_{expiration.date()}.html"

    fig.write_html(str(out_path))
    print(f"Chart saved: {out_path}")
    fig.show()


if __name__ == "__main__":
    main()
