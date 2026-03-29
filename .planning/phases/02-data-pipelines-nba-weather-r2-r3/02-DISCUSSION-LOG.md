# Phase 2: Data Pipelines - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-29
**Phase:** 02-data-pipelines-nba-weather-r2-r3
**Areas discussed:** Cache design, NBA data granularity, Injury data source, Historical data strategy

---

## Cache Design

| Option | Description | Selected |
|--------|-------------|----------|
| File-based JSON | Serialize responses to data/cache/ with timestamps. Survives restarts, easy to inspect. | ✓ |
| SQLite table | cache table in kalshi_bot.db. Unified but mixes concerns. | |
| In-memory only | Python dict, lost on restart. Fast but cold-starts re-fetch everything. | |

**User's choice:** File-based JSON

| Option | Description | Selected |
|--------|-------------|----------|
| Tiered TTLs | NWS 1hr, player stats 6hr, team stats/schedule 24hr, historical 7 days | ✓ |
| Flat 1-hour TTL | Everything expires after 1 hour. Simple but re-fetches stable data constantly. | |
| Claude's discretion | Claude picks TTLs per endpoint. | |

**User's choice:** Tiered TTLs

| Option | Description | Selected |
|--------|-------------|----------|
| Configurable in trading_config.json | TTLs in config, defaults match tiered values. Easy to tune without code changes. | ✓ |
| Hardcoded constants | Constants in cache.py. Simple but requires code edit to tune. | |

**User's choice:** Configurable in trading_config.json

| Option | Description | Selected |
|--------|-------------|----------|
| Serve stale cache as fallback | Return expired cached data with warning log on API failure. | ✓ |
| Raise exception | Fail fast — stale data could be worse than no data. | |

**User's choice:** Serve stale cache as fallback

**Notes:** User asked for clarification on cache storage options (file vs SQLite vs in-memory) before selecting. After explanation, chose file-based for simplicity and restart resilience.

---

## NBA Data Granularity

| Option | Description | Selected |
|--------|-------------|----------|
| Last 5 games | Standard for hot/cold streak detection. | |
| Last 10 games | Smoother signal, less reactive to spikes. | |
| Claude's discretion | Pick based on prop model needs. | ✓ |

**User's choice:** Claude's discretion (trend window)

| Option | Description | Selected |
|--------|-------------|----------|
| All players in tonight's games | On-demand per game. Only pull stats for players actually playing. | ✓ |
| Top ~150 prop-relevant players | Pre-fetch curated list. Requires maintaining roster. | |
| All active NBA players | ~500 players — slow, lots of irrelevant data. | |

**User's choice:** All players in tonight's games (on-demand)

| Option | Description | Selected |
|--------|-------------|----------|
| 3 seasons | ~3,600 games. Post-pandemic data. | |
| 5 seasons | ~6,000 games. 2020-21 bubble is an outlier. | |
| Claude's discretion | Pick based on training data needs. | ✓ |

**User's choice:** Claude's discretion (historical seasons)

---

## Injury Data Source

| Option | Description | Selected |
|--------|-------------|----------|
| nba_api only | Use nba_api injury report endpoint. Accept lag, flag GTD as low-confidence. | |
| ESPN scraping + nba_api fallback | ESPN primary (fresher), nba_api fallback. More accurate but brittle. | ✓ |
| Skip injury data | Treat all players as available. | |

**User's choice:** ESPN scraping + nba_api fallback

**Notes:** User accepted the brittleness tradeoff for fresher data. ESPN scraper to be isolated in its own function for easy swap-out.

| Option | Description | Selected |
|--------|-------------|----------|
| Flag low-confidence, reduce position size | Confidence penalty propagates to position sizer. Pipeline keeps running. | ✓ |
| Skip the market entirely | Don't trade any prop with unclear status. | |

**User's choice:** Flag as low-confidence, reduce position size

---

## Historical Data Strategy

| Option | Description | Selected |
|--------|-------------|----------|
| Pull-once, store to SQLite | First run fetches + writes to DB. --refresh-history flag for re-fetch. | ✓ |
| Always fresh from API | Re-fetch every run. Slow, hammers rate limits. | |
| Lazy load on first model train | Only fetch when model training requests it. | |

**User's choice:** Pull-once, store to SQLite

| Option | Description | Selected |
|--------|-------------|----------|
| 3 years NOAA history | ~1,100 days per station. Enough for seasonal patterns + bias correction. | ✓ |
| 5 years | More data for multi-year climate trends. Slower first-run fetch. | |
| Claude's discretion | Pick based on bias correction needs. | |

**User's choice:** 3 years

---

## Claude's Discretion

- Exact nba_api endpoints for each data type
- Player trend window (last N games)
- Number of NBA historical seasons to fetch
- Cache key naming and file structure within data/cache/
- ESPN scraper implementation and HTML parsing resilience
- ELO calculation methodology
- SQLite schema for new historical tables

## Deferred Ideas

None.
