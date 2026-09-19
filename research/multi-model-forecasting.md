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

Bisected more precisely during implementation: null through 02-15, lead1 only
on 02-18, both leads from **2025-02-20**, which is the value
`MODEL_ARCHIVE_USABLE_START` now carries. Treat it as a usable start by the
same rule
`NBM_ARCHIVE_USABLE_START` already encodes: a sparsely-null tail is "not yet
available", not an error. That is ~10 months of training data to the
`TRAIN_END` of 2025-12-31 — enough to fit, not enough to be relaxed about
overfitting, and a reason to keep the parameter count low.

### A coverage caveat found in implementation

The other five models are steady once their archive begins. **UKMO is not.**
Biweekly probes from 2024-08 through mid-2025 kept finding whole days with one
or both leads null, and a real June-2025 build at Central Park had UKMO lead1
coverage at 72% and lead2 at 48%, against 100% for every other model. The
null/completeness machinery handles this correctly -- those days contribute
nothing and are never imputed -- but the archive start for UKMO is a floor,
not a coverage guarantee, and `n_models` is what a consumer must actually
filter on.

### A first data point, too small to mean anything

A 25-complete-day build at Central Park for June 2025 gave lead1 MAE:

| model | MAE | coverage |
|---|---|---|
| `ecmwf_aifs025_single` | 1.62F | 100% |
| `ncep_nbm_conus` | 1.67F | 100% |
| `icon_seamless` | 1.98F | 100% |
| `ukmo_global_deterministic_10km` | 2.05F | 72% |
| `ecmwf_ifs025` | 2.86F | 100% |
| `gfs_seamless` | 3.12F | 100% |
| ensemble mean | 1.65F | 100% |

It is tempting to read the first row as AIFS beating NBM and vindicating the AI
model. **It is not evidence of that.** The gap is 0.05F over 25 days, one
station, one month, one season -- comfortably inside noise, and the ensemble
mean landing between the two is exactly what a sample this size would show
whether or not ensembling helps. It is recorded here only so that nobody
later remembers it as a result. The caution in the previous section stands
until a full-period run says otherwise.

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

---

# Results

*Run 2026-09-19 on 1,584 assembled city-days (four stations, 2024-12-01 to
2025-12-31). Fit on 2025-03-01 to 2025-09-30, evaluated on 2025-10-01 to
2025-12-31 — both inside the training window. The 2026 holdout remains unspent.*

## Both halves of the proposal were right, and it still loses

### Point accuracy: bias was hiding everything

Raw MAE across the 924 days where all six models are present ranks the models
one way; after removing each model's per-station bias — which is what the model
actually does — it ranks them almost the other way.

| model | raw MAE | debiased MAE |
|---|---|---|
| **ensemble mean** | 2.35F | **1.40F** |
| `ncep_nbm_conus` | 2.27F | 1.52F |
| `ecmwf_aifs025_single` | **3.25F** | 1.56F |
| `icon_seamless` | 2.21F | 1.75F |
| `ukmo_global_deterministic_10km` | 2.68F | 1.99F |
| `gfs_seamless` | 2.49F | 2.03F |
| `ecmwf_ifs025` | 2.91F | 2.05F |

Two things worth keeping. **The ensemble mean beats every single model**,
including NBM, by 8% once bias is removed — combining does work here. And
**AIFS looked like the worst model in the set and is nearly the second best**:
its raw MAE of 3.25F was almost entirely a -3.04F constant bias, which any
bias-correction step removes for free. Judging a model by raw MAE when the
consumer bias-corrects is judging the wrong quantity. The earlier 25-day probe
that flattered AIFS and the full-year raw table that condemned it were both
measuring bias, not skill.

### Spread genuinely predicts error

On debiased ensemble residuals across 924 days, the spread-skill correlation is
**0.295**, and it is monotone:

| spread quartile | mean spread | mean absolute residual |
|---|---|---|
| lowest | 0.79F | 0.97F |
| 2nd | 1.22F | 1.25F |
| 3rd | 1.66F | 1.51F |
| highest | 2.48F | 1.86F |

In the fitted model, `b = 0.4579` with a standard error of `0.0635` — about
seven standard errors from zero. The premise of the whole experiment holds:
days when six models disagree really are harder days, and a constant sigma was
throwing that information away.

### The market still wins

Paired Brier improvement over the market, event-clustered, 352 events:

| variant | vs market | 95% CI |
|---|---|---|
| `single` (old model) | −0.01631 | (−0.02016, −0.01242) |
| `ensemble_mean` | −0.01249 | (−0.01659, −0.00834) |
| `spread_sigma` | **−0.00862** | (−0.01408, −0.00342) |

And the internal comparisons, which say *which* change helped:

| comparison | improvement | 95% CI |
|---|---|---|
| `ensemble_mean` vs `single` | +0.00382 | (0.00091, 0.00662) |
| `spread_sigma` vs `ensemble_mean` | +0.00387 | (0.00040, 0.00706) |
| `spread_sigma` vs `single` | +0.00769 | (0.00282, 0.01254) |

**Both changes helped, by almost exactly the same amount, and both CIs exclude
zero.** Together they closed **47%** of the gap to the market, from −0.0163 to
−0.0086.

And the market still wins, with the confidence interval lying entirely below
zero. This is not an inconclusive result — it is a decided loss. The report's
verdict line was corrected to say so: an interval wholly below zero means the
market beat us and we can state it at 95% confidence, which is a different
claim from "we could not tell". Reporting both as "does not exclude zero" would
have let a settled negative read as an open question.

## What this actually establishes

The honest reading is that the forecasting route is now closed much more firmly
than before, because the obvious fix worked and was not enough.

Before this, "our model is primitive" was a live excuse for the 0.019 gap. It
is no longer available. Six models, a proper ensemble mean, and a
state-dependent sigma fitted on a real and strongly significant spread-skill
relationship — the standard professional toolkit, correctly applied — recovers
under half the gap. Closing the rest would require beating a market that
presumably already does all of this, using only free public data.

That is a more useful conclusion than the original negative. The first said
"our model lost". This one says "the known fixes work, are measurable, and
still lose", which is a statement about the market rather than about us.

## A side finding on the sign conflict

`implied-distribution.md` records a conflict: Le (arXiv:2602.19520) reports
Kalshi weather prices as *too extreme* at 12-48h, our own `longshot-bias.md`
finds them *compressed*. The market calibration table from this run, at the
same horizon, lands with our measurement:

| market bin | n | mean price | empirical |
|---|---|---|---|
| [0.0,0.1) | 1061 | 0.033 | **0.011** |
| [0.1,0.2) | 268 | 0.146 | **0.104** |
| [0.5,0.6) | 107 | 0.540 | **0.636** |

Cheap brackets settle YES a third as often as priced; expensive ones more often
than priced. That is longshots overpriced — the opposite of "prices too
extreme". Two independent measurements on our data now point the same way,
which shifts the burden onto the reconciliations listed in
`implied-distribution.md`, most likely that a single fitted slope is being
asked to summarise a relationship that changes sign at 35c.

## Sensitivity: the mechanism replicates, the significance is data-limited

Two checks that were listed as outstanding, now run.

**`min_models` does not bind below 6.** Every evaluation-window row carries all
six models; the training window is 592 rows at six models and 264 at five. So
`min_models` of 2, 3, 4 and 5 produce byte-identical results — not because the
flag is inert (it is wired correctly) but because nothing is excluded. Only
`min_models = 6` bites, dropping training pairs from 819 to 568:

| | default (`min_models`<=5) | `min_models=6` |
|---|---|---|
| spread-skill correlation | 0.2448 | 0.2295 |
| `b` (se) | 0.4579 (0.0635) | 0.4406 (0.0785) |
| `spread_sigma` vs market | −0.00862 (−0.01408, −0.00342) | −0.00927 (−0.01489, −0.00388) |
| `spread_sigma` vs `ensemble_mean` | +0.00387 (0.00040, 0.00706) | +0.00325 (**−0.00045**, 0.00669) |

The sigma benefit **loses significance** at `min_models=6`. Worth reading
carefully: the point estimate barely moves (0.00387 → 0.00325) while the
interval widens, which is what a real effect looks like with 30% less data —
not what an artefact looks like, since an artefact would be expected to shift
rather than merely blur. It is still a warning that this effect is modest
enough to be data-limited at our sample size.

**The mechanism replicates at a different lead.** Re-run at `lead2` (two-day
lead):

| comparison | lead1 | lead2 |
|---|---|---|
| `single` vs market | −0.01631 | −0.02368 |
| `ensemble_mean` vs market | −0.01249 | −0.02050 |
| `spread_sigma` vs market | −0.00862 | −0.01684 |
| `ensemble_mean` vs `single` | +0.00382 | +0.00318 |
| `spread_sigma` vs `ensemble_mean` | +0.00387 | +0.00366 |
| `spread_sigma` vs `single` | +0.00769 | +0.00684 |
| spread-skill correlation | 0.2448 | 0.2636 |

The two **internal** improvements are nearly identical at both leads, and the
spread-skill correlation is slightly *higher* at the longer lead, where models
disagree more. A fitted artefact would not be expected to reproduce itself at
a different lead with the same magnitude. This is the strongest evidence yet
that the spread-skill mechanism is real.

The gap to the market, meanwhile, roughly doubles at lead2 (−0.0086 →
−0.0168). Our model degrades faster with lead than the market's does, which is
itself informative: whatever the market knows that we do not, it matters more
at two days than at one.

## What is not yet done

- The evaluation split is inside the training window. Nothing here has touched
  the 2026 holdout, and nothing should until a protocol is declared.
- Why the market's advantage grows with lead has not been investigated. The
  obvious hypothesis — that the market uses a true ensemble rather than a
  six-model proxy, and true ensemble spread matters more at longer leads —
  is untested.
