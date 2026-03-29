# Phase 2: Data Pipelines (NBA + Weather) - Context

**Gathered:** 2026-03-29
**Status:** Ready for planning

<domain>
## Phase Boundary

Build the data ingestion layer for both domains:
- **NBA:** team stats, player stats (per-game averages + game logs), schedules, injury reports, historical game results, ELO ratings, matchup context (opponent defensive rating vs position)
- **Weather:** NWS probabilistic forecasts + ensemble/gridpoint data, NOAA historical daily data, station-to-ticker mapping for KNYC, KMDW, KMIA, KAUS

Both pipelines cache responses. Models, trading logic, and the evaluator bot are out of scope — this phase only builds the data layer that those phases consume.

</domain>

<decisions>
## Implementation Decisions

### Cache
- **D-01:** Cache backend is **file-based JSON** in `data/cache/`. Each response serialized to a file with a timestamp embedded. Survives process restarts; easy to inspect.
- **D-02:** TTLs are **tiered by data volatility**:
  - NWS forecasts: 1 hour
  - Player stats / game logs: 6 hours
  - Team stats / schedule: 24 hours
  - Historical data (NOAA daily, NBA game results): 7 days
- **D-03:** TTL values are **configurable in `trading_config.json`** under a `cache` section. Code reads from config with the tiered values above as defaults.
- **D-04:** On API failure (network error, timeout), **serve stale cached data as fallback** with a warning log. Do not crash the pipeline.

### NBA Data Granularity
- **D-05:** Player prop stats are fetched **on-demand for all players in tonight's games** — not a pre-maintained roster list. Fetch triggered by the schedule (who's playing), then pull stats for those players only.
- **D-06:** Trend window (last-N games) and number of historical seasons — **Claude's discretion** based on what logistic regression + gradient boosting training needs and what's reasonable for hot/cold streak detection.

### Injury Data
- **D-07:** Primary injury source is **ESPN injury page scraping**. Fallback to nba_api injury report endpoint when ESPN scrape fails. ESPN scraper must be isolated in its own function to allow easy replacement if HTML structure changes.
- **D-08:** When player injury status is unknown or GTD: **flag as low-confidence in the data output, do not skip**. The prop model receives the data with a confidence penalty; position sizer sees lower confidence and sizes down. The pipeline keeps running.

### Historical Data Bootstrap
- **D-09:** Historical data (both NBA game results and NOAA weather) uses **pull-once, store to SQLite**. First run fetches and writes to the existing `kalshi_bot.db`. Subsequent runs read from DB. A `--refresh-history` CLI flag forces a re-fetch.
- **D-10:** NOAA historical weather: **3 years** of daily temperature/precip data per station (~1,100 days per station). Sufficient for seasonal patterns and NWS bias correction.

### Carrying Forward from Phase 1
- **D-11:** All HTTP calls are **synchronous** (`requests` library). No asyncio — consistent with the established pattern in Phase 1 (D-02).
- **D-12:** Data modules follow the **module-level singleton pattern** established in Phase 1 (D-05) where applicable.
- **D-13:** Integration tests for actual API calls are **opt-in** via `KALSHI_INTEGRATION=true` environment variable, marked `@pytest.mark.integration` (consistent with D-07 from Phase 1).

### Claude's Discretion
- Exact nba_api endpoints for each data type (teams, players, schedules, game logs, matchup context, historical results)
- Player trend window (last N games) — pick based on streak detection signal quality
- Number of NBA historical seasons — pick based on training data size needs
- Cache key naming and file structure within `data/cache/`
- ESPN scraper implementation details and HTML parsing resilience approach
- ELO calculation methodology (K-factor, initial rating, reset policy)
- SQLite table structure for historical NBA and NOAA data

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Specs
- `.planning/CONVENTIONS.md` — directory structure (`src/data/nba/`, `src/data/weather/`, `src/data/cache.py`), Python conventions, config file format
- `.planning/REQUIREMENTS.md` §R2 — full acceptance criteria for NBA pipeline (R2.1–R2.10)
- `.planning/REQUIREMENTS.md` §R3 — full acceptance criteria for Weather pipeline (R3.1–R3.6)
- `.planning/ROADMAP.md` §Phase 2 — deliverables list and working test definition
- `.planning/PROJECT.md` — tech stack (nba_api, NWS API, NOAA, pandas, numpy), architecture overview

### Existing Code (Phase 1 patterns to follow)
- `src/config.py` — module-level singleton pattern, `trading_config.json` loading
- `src/db/database.py` — SQLite data access layer; historical data writes here
- `src/db/schema.sql` — existing full schema; any new tables are additive

No external ADRs or specs beyond the above.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/db/database.py` — `Database` class with `execute()` / `fetchall()` helpers. Historical data (NBA results, NOAA daily) stored here via this interface.
- `src/config.py` — `Config` singleton. Cache TTL values will be added to `trading_config.json` and accessed via `config`.
- `src/kalshi/client.py` — existing synchronous HTTP pattern (requests + retry logic) to follow for NWS/NOAA clients.

### Established Patterns
- Sync `requests` throughout — no async (Phase 1 D-02). NWS and NOAA clients follow the same pattern.
- Module-level singleton (`from src.xyz import xyz_instance`) — use for data clients if they hold state (e.g., a requests.Session).
- Integration test pattern: `@pytest.mark.integration`, opt-in via `KALSHI_INTEGRATION=true`.

### Integration Points
- `src/db/database.py` → historical NBA game results and NOAA daily data written here on first run
- `src/config.py` / `trading_config.json` → cache TTL config added here
- `data/cache/` directory → new, created by cache.py on first write
- Future phases (Phase 3 models) will import from `src/data/nba/` and `src/data/weather/`

</code_context>

<specifics>
## Specific Ideas

- ESPN injury scraper must be isolated in its own function for easy swap-out — explicitly noted during discussion
- Cache stale-fallback behavior must log a warning (not silently serve stale data)
- `--refresh-history` flag on the data pipeline entry point for forcing a full historical re-fetch

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 02-data-pipelines-nba-weather-r2-r3*
*Context gathered: 2026-03-29*
