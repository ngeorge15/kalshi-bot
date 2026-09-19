# Midday nowcast — predeclared design

*Written 2026-09-19, **before any result exists**. The design section is not edited
once results are appended below it.*

## The question

The intraday floor (`intraday-floor.md`) tested only brackets that observation had
already made *impossible*, and found the market zeroes 99.8% of them before we can
act. It did not test the harder and more realistic case: brackets still **alive**
at midday. At 12:00 local, does a model that combines the day-ahead forecast with
the temperature observed so far beat the market's price for the day's high?

## A correction that shapes the design

An earlier claim that Open-Meteo's `previous-runs-api` serves HRRR at "24/24
coverage" was verified with `temperature_2m_previous_day1` — a forecast issued a
day earlier. It is **not** a same-day nowcast. The archive exposes only whole-day
leads; its "latest run" field for a past date is stitched from the first hours of
successive runs, i.e. the same look-ahead leak recorded in `historical-forecasts.md`.
**An honest 10am short-range model forecast cannot be obtained from this source.**

So this test is deliberately weaker than "HRRR nowcasting": it uses the day-1 NBM
forecast plus observed-so-far. That is a **lower bound** on what a true nowcast could
do, and a null result here does not rule out a model built on real HRRR archives
(NOAA's `noaa-hrrr-bdp-pds` S3 bucket, GRIB2, not attempted).

## Model (fixed in advance)

At decision instant T on local day D, let `M(T)` be the running maximum of ASOS
observations usable at T (10-minute latency, `observed_max_at`), and `F` the day-1
NBM forecast maximum for D (`ncep_nbm_conus`, lead1, as in `weather_backtest.py`).

- Remaining rise `R = final_high − M(T)` is modelled as a normal, **censored at 0**
  (the day's high cannot fall below what has already been observed).
- Its mean is `a_h + b_h * (F − M(T))`, its sigma `s_h`, with `(a_h, b_h, s_h)`
  fitted **per decision hour** on the training window only, per station.
- Bracket probability = probability that `M(T) + R` falls in the bracket.
  Brackets entirely below `M(T)` get probability 0 by construction.

## Predeclared parameters

| Parameter | Value |
|---|---|
| Decision instants (local standard time) | 10:00, 12:00, 14:00 — **12:00 is the headline** |
| Stations | KNYC, KMDW, KMIA, KAUS |
| Train window | 2024-12-01 to 2025-09-30 |
| Evaluation window | 2025-10-01 to 2025-12-31 (both inside the training era; **2026 holdout untouched**) |
| Universe | brackets still alive at T (upper bound above `M(T)`) |
| Price used | last 1-minute candle ending at or before T (`price_at_instant`) |
| Sigma floor | 1.0F, as in `ensemble_model.py`, justified against the oracle control |
| Trade rule | buy the side with edge ≥ 0.05 at the **executable** price, 10 contracts, fee once per order |
| Uncertainty | event-clustered bootstrap on city-day |

## Primary deliverable and falsification

**Primary:** paired Brier improvement of the nowcast over the market at the
12:00 headline instant, with an event-clustered 95% CI. Reported *first*.

**Falsified if any of these holds:**
1. The headline CI includes zero or lies below it.
2. The result appears at only one of the three instants (a one-instant win among
   three looks is a chance finding, not an effect).
3. The trade's net P&L CI includes zero after fees.

## Controls to run first

- **Persistence control:** the market's own price at 10:00 vs at 14:00 — how much
  information the market itself absorbs through the day. If it barely moves, our
  nowcast has more room; if it converges hard, there is nothing left to find.
- **Synthetic-effect control:** a planted-effect dataset the fitted model must
  recover, so a null on real data is interpretable.

## Prior, stated in advance

I expect this to fail. The market updates all day on the same public
observations, and a day-old forecast plus observed-so-far is a crude nowcast. It
is worth running because it is the one untested weather route, the data is on disk,
and the mechanism (information arriving after the forecast) is real.

## Results

*Not yet run.*
