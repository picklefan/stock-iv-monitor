# Yahoo Finance API Rate Limit Policy

**Status:** No official, publicly documented rate limits exist for Yahoo Finance endpoints. The API is undocumented/internal. Everything below is reverse-engineered from community experience.

---

## Community-Observed Limits

No official numbers. These are the most commonly cited thresholds from yfinance GitHub issues and developer forums (2024–2025):

| Window | Reported Limit | Source |
|--------|---------------|--------|
| Per minute | ~60–100 requests | GitHub Discussion #1513, Issue #2325 |
| Per hour | ~360 requests | GitHub Issue #2128 (user citing Yahoo API docs) |
| Per day | ~2,000–8,000 requests | Varies widely by user |
| Per burst | ~100 requests, then 30s cooldown | GitHub Discussion #2431 |
| Real-world | ~950 tickers before 429 (7-day 1m data) | GitHub Issue #2128 |

The old YQL limit of **2,000 requests/hour per IP** is from a deprecated service shut down in 2019. It does NOT apply to the current `query1.finance.yahoo.com` / `query2.finance.yahoo.com` endpoints.

---

## Timeline of Rate Limit Changes

### Nov 2024 — Sudden tightening
- Users who had been pulling 7,000+ tickers daily for years suddenly hit 429 errors after ~950 tickers.
- Yahoo deployed **cookie-crumb tracking** beyond simple IP-based limiting.
- Source: [Issue #2128](https://github.com/ranaroussi/yfinance/issues/2128)

### Jan 2025 — Even single requests blocked
- Users on yfinance v0.2.54 getting `YFRateLimitError` with only 4–5 requests/day.
- User-Agent header detection added by Yahoo.
- Source: [Issue #2289](https://github.com/ranaroussi/yfinance/issues/2289)

### Apr 2025 — TLS fingerprinting deployed
- Widespread blocks affecting even single `yf.Ticker("AAPL").info` calls.
- Python `requests` library identified as non-browser traffic.
- Solution: `curl_cffi` with Chrome impersonation.
- Source: [Issue #2411](https://github.com/ranaroussi/yfinance/issues/2411), [Issue #2422](https://github.com/ranaroussi/yfinance/issues/2422), [Issue #2428](https://github.com/ranaroussi/yfinance/issues/2428)

### Jul 2025–present — Ongoing cat-and-mouse
- Even with `curl_cffi`, bulk downloads of ~100 tickers see 80% failure rates.
- Yahoo continuously tightening bot detection.
- Source: [Issue #2614](https://github.com/ranaroussi/yfinance/issues/2614)

---

## How Yahoo Detects & Blocks

Yahoo uses **multi-layered tracking**, not a simple request counter:

| Layer | What it checks |
|-------|---------------|
| **IP address** | Request volume per IP |
| **Cookie/crumb** | Session-level tracking across requests |
| **TLS fingerprint (JA3)** | Whether the client is a real browser or a script |
| **User-Agent header** | Blocks default Python/Rust user agents |
| **Request patterns** | Timing, intervals, endpoint mix |

A 429 response means Yahoo flagged you on one or more of these layers. Blocks typically last **from minutes to 24+ hours**, and are per-IP.

---

## Mitigation Stack

In order of effectiveness:

1. **Use `curl_cffi`** (TLS impersonation) — most important
   ```bash
   pip install curl-cffi
   ```

2. **Use browser User-Agent header** — basic, always do this

3. **Throttle requests** — 1–2 seconds between calls, ~30s pause every ~100 requests

4. **Exponential backoff on 429** — 10s → 30s → 90s → 300s retry

5. **Batch with `yf.download()`** where possible (fewer requests than individual `Ticker` calls)

6. **Cache aggressively** — avoid re-fetching unchanged data

7. **Avoid VPNs/proxies** — Yahoo blocks many known VPN IP ranges

---

## Key GitHub Issues

- [#2128 — New rate-limiting (Nov 2024)](https://github.com/ranaroussi/yfinance/issues/2128) — first major report of tightened limits
- [#2289 — YFRateLimitError with few requests](https://github.com/ranaroussi/yfinance/issues/2289) — single-request blocking
- [#2325 — Proposal to document rate limits](https://github.com/ranaroussi/yfinance/issues/2325) — never implemented
- [#2411 — YFRateLimitError widespread](https://github.com/ranaroussi/yfinance/issues/2411) — TLS fingerprinting outbreak
- [#2422 — Rate limit on v0.2.57](https://github.com/ranaroussi/yfinance/issues/2422)
- [#2428 — Rate limit persisting 24+ hours](https://github.com/ranaroussi/yfinance/issues/2428)
- [#2469 — TLS/cookie fix with curl_cffi](https://github.com/ranaroussi/yfinance/issues/2469)
- [#2614 — Ongoing rate limit failures](https://github.com/ranaroussi/yfinance/issues/2614)
- [Discussion #1513 — What are the limits?](https://github.com/ranaroussi/yfinance/discussions/1513)
- [Discussion #2431 — Is rate limiting temporary?](https://github.com/ranaroussi/yfinance/discussions/2431)
- [Discussion #2581 — 429 Client Error](https://github.com/ranaroussi/yfinance/discussions/2581)

---

## Bottom Line

**There are no guaranteed safe request rates.** The 429 error is triggered by a combination of IP reputation, TLS fingerprint, cookie state, and request patterns — not a simple counter. A request that works today may be blocked tomorrow. The safest approach is TLS impersonation (`curl_cffi`) + conservative throttling (~1 req/2s) + local caching.
