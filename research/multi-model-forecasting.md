# Six forecast models on the leak-safe endpoint — the obvious defect in our weather model

*Researched 2026-09-18. Every availability claim below was verified by querying
the endpoint, not by reading documentation.*

## The defect

Our weather model, the one that lost to the market by 0.019 Brier
(`weather-backtest-validation.md`), is:

> one deterministic NBM forecast → add a fitted bias → wrap in a normal with a
> **single fitted sigma** → integrate over each bracket.

Two things in that sentence are primitive by the standards of operational
forecasting:

1. **One model.** Multi-model combination is the oldest and most reliable win in
   the field. We use exactly one.
2. **A constant sigma.** Our uncertainty is the same on a calm, well-agreed
   summer day as on a day when models disagree by ten degrees over a frontal
   passage. Real forecast uncertainty is strongly state-dependent, and the
   market's implied distributions presumably reflect that. A constant sigma is
   wrong in a way that is guaranteed to cost Brier score on exactly the days
   where the market's edge would be largest.

The fix for both is the same and it does not require any new kind of model.

## What is actually available, verified by query

Open-Meteo's `previous-runs-api` — the endpoint we already use, and the one
whose `temperature_2m_previous_dayN` fields are genuine **fixed-lead
reforecasts** rather than the stitched near-nowcast that the
`historical-forecast-api` returns (see `historical-forecasts.md`'s CORRECTION
block) — serves far more than NBM.

Probed at Central Park, `temperature_2m_previous_day1`, 2025-06-10:

| Model id | Fixed-lead reforecast | Notes |
|---|---|---|
| `ncep_nbm_conus` | ✅ 24/24 | what we currently use; already a calibrated blend |
| `ecmwf_ifs025` | ✅ 24/24 | ECMWF IFS, the global benchmark |
| `ecmwf_aifs025_single` | ✅ 24/24 | **ECMWF's operational AI model** |
| `gfs_seamless` | ✅ 24/24 | NOAA GFS |
| `icon_seamless` | ✅ 24/24 | DWD ICON |
| `ukmo_global_deterministic_10km` | ✅ 24/24 | Met Office |
| `ecmwf_aifs025` | ❌ 0/24 | returns nulls; use the `_single` id |
| `graphcast025` | ❌ | rejected: "Cannot initialize MultiDomains from invalid String value" |

**Six independent models, all on the same free, leak-safe endpoint, all
backtestable with the no-look-ahead machinery we already have.**

### AIFS archive coverage

Probed at Central Park for `ecmwf_aifs025_single`:

| Date | Non-null hours |
|---|---|
| 2024-06-15 | 0/24 |
| 2024-10-15 | 0/24 |
| 2025-01-15 | 0/24 |
| 2025-02-01 | 0/24 |
| 2025-02-15 | 0/24 |
| **2025-03-01** | **24/24** |
| 2025-03-08 | 24/24 |

So the AIFS fixed-lead archive begins between 2025-02-15 and 2025-03-01. Treat
**2025-03-01** as the usable start, by the same rule
`NBM_ARCHIVE_USABLE_START` already encodes: a sparsely-null tail is "not yet
available", not an error. That is ~10 months of training data to the
`TRAIN_END` of 2025-12-31 — enough to fit, not enough to be relaxed about
overfitting, and a reason to keep the parameter count low.

## The two experiments this unlocks

**Experiment 1 — multi-model spread as a state-dependent sigma.** This is the
cheap one and it attacks the defect directly. Instead of a constant fitted
sigma, let sigma be a fitted function of the *disagreement between the six
models* on that day. The literature calls this the spread-skill relationship,
and a poor-man's ensemble built from independent operational models is the
standard way to get one without paying for an ensemble product. If forecast
uncertainty is state-dependent and the market knows it and we do not, this is
where the 0.019 is hiding.

**Experiment 2 — does AIFS beat NBM at this specific task?** Worth stating
carefully, because it is easy to be misled here: AI weather models are
benchmarked on global headline scores at multi-day leads. Our task is
**station-level 2-metre daily maximum temperature at 12-36 hours**, which is
about the least glamorous corner of the problem and the one where local
statistical post-processing matters most. NBM is itself already a
statistically post-processed, bias-corrected blend built for exactly this
task. A raw global AI model may well *lose* to NBM here even while beating it
on 500hPa geopotential at day 7. Do not assume the newer model is better at
our job; measure it.

## Why this is the right next modelling step

It requires no new data source, no payment, no new leak surface, and no new
validation machinery — `select_forecast_values`, the latency guard, the
degenerate-series check, the train/test split and the event-clustered
bootstrap all apply unchanged. The only new code is fetching five more model
ids and fitting sigma as a function of spread rather than as a constant.

It is also falsifiable in the same clean way as everything else here: if the
multi-model ensemble with state-dependent sigma still scores worse than the
market's Brier of 0.1036, then the market is not beatable with publicly
available forecasts, which is a real and publishable conclusion rather than
a failure.

## What was NOT verified

- Whether Open-Meteo serves GenCast (DeepMind's diffusion **ensemble** model) at
  all. `graphcast025` is rejected outright by the API. GenCast is the model
  whose design most closely matches this problem — it produces a distribution
  natively rather than requiring one to be fitted — so its absence is the
  biggest gap in this survey.
- True ensemble members (ECMWF ENS 51-member) on the `previous-runs-api`.
  Open-Meteo has an Ensemble API, but whether *fixed-lead reforecasts* of
  ensemble members exist is unconfirmed; that is the difference between a real
  ensemble spread and the six-model proxy above.
- Rate limits for a six-model backfill. The free tier has limits and this
  multiplies our request count by six.

## Sources

- Open-Meteo previous-runs API, queried directly 2026-09-18 (results tabulated above)
- [ECMWF Forecast API — Open-Meteo](https://open-meteo.com/en/docs/ecmwf-api)
- [Ensemble API — Open-Meteo](https://open-meteo.com/en/docs/ensemble-api)
- [Exploring GraphCast — Open-Meteo](https://openmeteo.substack.com/p/exploring-graphcast)
- [GraphCast / WeatherNext — Google DeepMind](https://github.com/google-deepmind/graphcast)
