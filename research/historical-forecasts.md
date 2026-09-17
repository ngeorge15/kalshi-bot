# Retrospective backtest feasibility: as-issued forecasts vs. Kalshi temperature brackets

Question: can we backtest `predict_hourly_high` (src/paper/weather.py) against ~12-24
months of history instead of waiting on forward collection (protocol-weather-v1.json
wants 120 forward-collected city-days)? Core constraint: no look-ahead — the forecast
used for date D must be one that existed before local midnight of D, not a reanalysis.

Samples saved under `scratchpad/forecast-samples/` (KNYC unless noted; target date
2026-07-16, ~2 months before today).

> **CORRECTION (2026-09-16, orchestrator review) — supersedes the Recommendation below.**
> Do **not** use `historical-forecast-api` for this backtest. Open-Meteo's own docs state
> it is "a continuous hourly timeseries built by stitching the first hours of each
> successive model run" — i.e. near-nowcast values, which would leak information from
> inside the target day and grossly overstate skill.
>
> The claim that `previous-runs-api` rejects NBM is wrong. Verified directly:
> `previous-runs-api.open-meteo.com/v1/forecast?...&hourly=temperature_2m_previous_day1,temperature_2m_previous_day2&models=ncep_nbm_conus`
> returns real values for KNYC (e.g. 2024-11-01, 2025-03-01; null on 2024-06-01), so
> fixed-lead NBM history starts around **Nov 2024** (~22 months, 4 stations).
> `_previous_dayN` is "the value that was predicted N×24 hours before valid time", so for
> every hour of a target local-standard day, `previous_day1` was issued before that day
> began. **Use `previous_day1` (and `previous_day2` as a longer-lead variant).**
> Remaining caveats: (1) each hour comes from a different run, so the "forecast" is a
> composite whose latest issuance is ~24h before the day's last hour — compare against a
> market price taken no earlier than local day start minus ~0h and document the choice;
> (2) Open-Meteo's run-availability latency vs. the nominal 24h offset is unverified;
> (3) still a declared model variant (NBM), not the live NWS-hourly model.

## What the repo already has

- `src/data/weather/noaa.py` — NOAA CDO GHCND **observed** daily TMAX/TMIN, pull-once
  into SQLite. This is truth data, not a forecast, and isn't necessarily identical to
  the NWS CLI report (different QC pipeline/timing) — see truth-source section below.
- `src/data/weather/nws.py` — live-only NWS gridpoint/hourly/griddata fetchers with a
  stale-cache fallback. No historical archive; confirmed `api.weather.gov` rejects a
  `?time=` query param (`400 Bad Request`) — there is **no way to ask the live API for
  a past forecast**, and NWS does not publish one. `forecastHourly` as literally
  consumed by `observe.py` is not archived anywhere I could find (IEM's product
  archive covers NWS **text** products, not the JSON gridpoint API).

## Candidate sources tested

### 1. Open-Meteo Previous Runs API (`previous-runs-api.open-meteo.com`)
Exposes `temperature_2m_previous_dayN` — per Open-Meteo's docs, `previous_day1` is
"the value predicted 24 hours before valid time" (a fixed lead time per hour, not one
frozen model run) [docs, not independently verified against raw NOMADS output].
- Models: confirmed working model strings `gfs_seamless`, `gfs_global`, `ncep_hrrr_conus`
  (`gfs_hrrr` alias also works), `ecmwf_aifs025`, `best_match`. **`ncep_nbm_conus`
  works on `historical-forecast-api` but returns HTTP 400 on `previous-runs-api`** —
  confirmed by direct test — so NBM is not available with per-lead-time semantics.
- Sample: `openmeteo_prevruns_gfs.json` (GFS, KNYC, 2026-07-16, hourly, previous_day1).

### 2. Open-Meteo Historical Forecast API (`historical-forecast-api.open-meteo.com`)
Returns hourly series stitched from "the initial hours of successive model runs"
per Open-Meteo docs — i.e., near-real-time-ish blended data, not a single fixed lead
time. **`models=ncep_nbm_conus` works here** and returns hourly (not 3-hourly) NBM
values.
- Coverage tested directly: KNYC NBM values present 2024-11-01 and 2024-10-15,
  **absent (null) 2024-06-01** — archive start is between those, consistent with
  Open-Meteo's docs claim of "NBM since October 2024" (~23 months of history from
  today). GFS coverage per docs since March 2021.
- Caveat: because this endpoint blends near-the-valid-hour data rather than one
  fixed lead, it is **not a drop-in match** for `predict_hourly_high`'s snapshot
  contract, which needs one `issued_at` covering 24 forward hourly periods. Using it
  for a backtest means declaring a different, honestly-labeled input contract (see
  "model mismatch" below), not literally replaying the shipped function.
- Samples: `openmeteo_hist_forecast_gfs.json`, `out_nbm_conus.json`.
- License/cost: free for non-commercial use per Open-Meteo's terms; rate limits and
  commercial pricing exist but weren't tested (see open-meteo.com/en/pricing —
  **unverified for exact limits**).

### 3. IEM MOS/NBM text-bulletin archive (`mesonet.agron.iastate.edu/api/1/mos.json`)
Free JSON API over IEM's archive of NWS text bulletins, queried by
`station=KNYC&model=<GFS|NBS|NBE>&runtime=<ISO8601Z>`.
- **`model=GFS`** (classic GFS MOS): one `runtime` returns one bulletin — a genuine
  single-issuance snapshot with a full forward sequence (`ftime`), exactly matching
  `predict_hourly_high`'s "one issued_at, many periods" shape. Cadence is **3-hourly
  early, 6-hourly beyond ~72h** — not the 24 distinct 1-hour periods the live
  function requires, so periods would need re-derivation (see below). Confirmed
  present at runtimes 2023-01-15, 2024-07-15, 2025-01-15 (likely goes back to GFS
  MOS's ~2004 operational start — **not verified that far back**, just confirmed
  ≥2.5 years).
- **`model=NBS`** (NBM short-range text bulletin): same single-issuance shape,
  3-hourly cadence, and it *is* NBM (the model the live NWS forecast is seeded from
  — see below). But archive is short: confirmed **zero rows before ~May 2026**
  (2026-03-01 and 2025-09-15 both empty; 2026-05-01 and later present) — only ~4.5
  months of history, well short of the 12-24 month target.
- **`model=NBE`** (NBM extended) exists too, 12-hourly cadence, same short retention.
- Confirmed working for all four stations (KNYC/KMDW/KMIA/KAUS) for GFS MOS.
- Samples: `iem_mos_knyc.json` (GFS), `iem_nbs_knyc.json` (NBS).

### 4. NOAA AWS Open Data (`noaa-nbm-pds`, `noaa-ndfd-pds`)
Listed both buckets (public, unauthenticated `?list-type=2` GETs, no credentials).
`noaa-nbm-pds` is organized `blendv5.0/conus/<year>/<month>/...` in GRIB2; listing
shows **only 2026/05 through 2026/09 present** — a rolling ~4-5 month operational
mirror, not a long-term archive (matches IEM's NBS retention almost exactly, likely
same upstream feed). `noaa-ndfd-pds` is similarly a live/rolling feed (files dated
the current day under `expr/...`), no evidence of 12-24 months of history. Point
extraction from either would also need GRIB2 decoding + grid interpolation
(`wgrib2`/`cfgrib`/`herbie`) — moot here since retention rules both out anyway.

### 5. `api.weather.gov` forecastHourly archive
Confirmed: none. The live endpoint has no historical query support, and I found no
third-party archive of the raw JSON gridpoint/hourly product (only of the NWS text
products, via IEM, covered above).

## (a) Model mismatch — honest handling

The live model consumes NWS `forecastHourly`, which is **NDFD** output: NBM is
explicitly "the starting point" for NDFD grids, which human forecasters can then
edit [NOAA/MDL description of NBM's role, via search — not an official statement I
fetched directly from weather.gov, treat as credible but secondary-sourced]. So NBM
is a closely related but not identical input — it can diverge from what a forecaster
actually issued, especially in active weather. Recommendation: **do not claim this
reproduces the live model.** Declare it a separate, explicitly-named model variant
(e.g. `weather_hourly_normal_baseline_backtest_nbm_v1` or similar), state in its
`assumption` field that it uses archived NBM guidance rather than the forecaster-
edited NDFD product the live system uses, and report its skill as informative about
NBM-driven strategies, not a validated backtest of the exact deployed model.
A second, permanent honesty gap: none of the archived sources hand you one
`issued_at` snapshot with 24 distinct hourly periods the way the live snapshot does.
Two ways to close it, in order of fidelity:
1. **IEM GFS-MOS-style single bulletin** (real single-issuance semantics) but
   3-hourly/6-hourly cadence — would require a documented, coarser variant of
   `predict_hourly_high` (e.g., max of available sub-daily points as the daily-high
   proxy) rather than literally 24 one-hour periods. Different granularity is itself
   a source of bias since MOS points may miss the exact hourly peak.
2. **Open-Meteo hourly NBM** (`historical-forecast-api`) — real hourly granularity,
   but each hour's value has its own effective lead time (blended-by-lead, not one
   run), so there's no single `issued_at`; the honest framing is "a series built
   from ~24h-lead-time NBM guidance," not "a forecast issued at time T."
Either path needs an adapter separate from (not a monkeypatch of) `predict_hourly_high`
so the live function's staleness/completeness contract isn't silently weakened.

## (b) sigma_f calibration without leakage

Fit `sigma_f` as the empirical std-dev of (archived forecast max-hourly-temp − CLI
observed daily high), grouped by station and by lead time bucket (e.g., "forecast
made previous afternoon" vs. "forecast made previous morning"), using
`TemporalSplitter` (`src/validation/splitter.py`) exactly as it's already used
elsewhere in the repo: sort city-days chronologically, fit sigma on the train slice
only, and never touch `holdout()` until a final report (it's already write-protected
there with `allow_holdout=True` + logging). Evaluate the resulting Brier score on the
`test` slice, which the backtest's headline number should come from — parallel to how
`protocol-weather-v1.json` defines `primary_metric` for the forward experiment, this
backtest should declare its own equivalent metric/threshold up front rather than
reusing the forward protocol's numbers post hoc.

## (c) Observed-high truth source (Kalshi settlement basis)

Kalshi's KNYC/KMDW/KMIA/KAUS daily-high contracts settle to the NWS **CLI** (daily
climate report) local-standard-time daily maximum — confirmed IEM archives the CLI
text product per station:
- Confirmed PILs exist with real data at 2026-07-16: `CLINYC` (KOKX), `CLIMDW`
  (KLOT), `CLIMIA` (KMFL), `CLIAUS` (KEWX), via
  `mesonet.agron.iastate.edu/api/1/nws/afos/list.json?pil=<PIL>&date=<YYYY-MM-DD>`.
- Fetched and read one full CLINYC report (`iem_cli_text.json`): it reports
  `MAXIMUM 84 259 PM` for the Central Park climate summary — exactly the settlement
  quantity, LST, and it's a plain-text product IEM has archived for decades (CLI
  reports predate the API by a wide margin; not tested how far back the archive
  goes, only confirmed present for a mid-2026 date).
- This is a **better truth source than `noaa.py`'s NOAA CDO GHCND pull** for this
  purpose: CDO summaries can differ from the CLI report in QC/rounding/timing and
  aren't guaranteed to be the number Kalshi settles against. Existing `noaa.py`
  code can stay as a cross-check, but CLI-parsed values should be the backtest's
  primary truth series, parsed per-station with the regex-able "MAXIMUM" line shown
  above.

## Recommendation

Use **Open-Meteo's `historical-forecast-api` with `models=ncep_nbm_conus`** as the
primary forecast source, paired with **IEM's CLI archive** as truth.
- Coverage: ~23 months (since ~Oct 2024) at all four stations, hourly, free,
  trivial HTTP+JSON access — the best coverage/fidelity/effort tradeoff of everything
  tested. Roughly **23 months × 4 stations ≈ 90 city-months**, i.e. on the order of
  2,700 city-days of raw material, though only a subset will have qualifying Kalshi
  markets/contract windows — the real backtestable-city-day count depends on Kalshi's
  own market history for these tickers, which wasn't checked here (out of scope: this
  research only covers the forecast/truth side).
- Use IEM GFS MOS (long history, single-issuance snapshot) as a secondary,
  longer-horizon variant for sigma_f calibration robustness checks, since it's the
  only source here with genuine 24+ month single-issuance archives — accepting its
  coarser 3-/6-hourly cadence and its different (classical MOS, not NBM) model family.
- **Must declare this as a separate model variant** (see (a)) — not a backtest of the
  literal deployed `weather_hourly_normal_baseline`, because (i) NBM ≠ forecaster-
  edited NDFD, and (ii) neither archived source gives a true single `issued_at` +
  24×1-hour-period snapshot; both require an honestly-documented adapter.

**Main risks**, ranked:
1. Model-substitution risk (a) — a favorable backtest result may not transfer to the
   live NDFD-consuming model; must be reported as a distinct, weaker claim.
2. Snapshot-shape mismatch — a fixed-lead or blended-lead series is not the same
   object as one forecaster issuance; any resulting sigma_f/skill numbers describe
   the adapter's behavior, not `predict_hourly_high`'s literal contract.
3. Open-Meteo is a third party re-serving NOAA data, not NOAA itself — verify parity
   against a NOMADS/IEM NBM value before trusting it at scale (only spot-checked
   here, not cross-validated).
4. Actual usable-city-day count is gated by Kalshi's own historical market
   availability for these four tickers, which this research did not check.

**Unverified / to confirm before relying on this**: Open-Meteo's exact `previous_dayN`
lead-time mechanics (docs-only, not independently reproduced against raw NBM grib);
Open-Meteo rate limits/pricing for the volume this backtest would need; IEM's CLI
archive depth beyond the one 2026-07-16 sample checked; the NBM→NDFD "starting point"
characterization (search-sourced, not fetched from an official NOAA/MDL page — both
`vlab.noaa.gov/web/mdl/nbm-overview` and `weather.gov/mdl/nbm_home` 404'd during this
research).
