# NBA model vs. Kalshi market prices, 2025-26 season

Run 2026-09-17. **Result: the model loses to the market decisively, and also loses to a
plain ELO baseline.** Nothing here supports trading NBA contracts.

## Method

- **Games**: 1,225 regular-season games from `nba_game_results` (2025-26), via the existing
  `fetch_historical_results` pipeline.
- **Market prices**: Kalshi `KXNBAGAME` moneyline markets, quoted **30 minutes before
  tip-off** using the same no-look-ahead rule as the weather backtest — the most recent
  candle ending at or before that instant, never a later one. Tip-off times come from the
  league schedule, not from Kalshi's expiration field (verified to be a settlement estimate,
  not tip-off). 1,224 of 1,225 games had a usable quote; the one miss is a game Kalshi
  listed under its originally scheduled date after a reschedule, reported rather than
  guessed at. Mean bid-ask spread: 1.07 cents.
- **Comparison**: `src/analytics/backtest.run_backtest` walk-forward, 4 windows,
  minimum 100 training games. The model is refitted per window and never trained on the
  games it is scored against. This is a within-season walk-forward, not a held-out season.
- **Probability convention**: market mid (bid+ask)/2 as P(home win). Bid and ask are kept
  separately in the CSV so a later cost-aware version can use executable prices.

## Results (1,123 out-of-sample games with odds)

| Comparison | Brier improvement | 95% CI | Verdict |
| --- | --- | --- | --- |
| Model vs. market | **-0.0439** | (-0.0539, -0.0345) | Model clearly worse |
| Model vs. market, clustered by slate date | -0.0424 | (-0.0530, -0.0317) | Unchanged by clustering |
| Model vs. ELO-only baseline | -0.0287 | (-0.0372, -0.0204) | Model clearly worse |
| Model vs. always-home baseline | +0.0115 | — | Marginally better |

Absolute scores: model Brier 0.2363 (accuracy 58.1%), ELO-only 0.2076 (66.9%),
always-home 0.2478 (55.3%), market 0.1943 (on the 1,224 quoted games; home teams won 55.5%,
and a constant home base rate scores 0.2470).

## Reading this honestly

The model does not beat team strength alone, so its extra features are not adding
information — they are adding noise. The market is far ahead of both. Every interval
excludes zero and clustering by game-day rather than by game does not change that, so this
is not a sample-size or correlation artifact.

This is the expected outcome for a major sports moneyline market, which is heavily traded
and well calibrated (market Brier 0.194 against a 0.247 base-rate baseline). It is reported
as a negative result rather than tuned away: no parameter was adjusted after seeing it.

## Caveats

- Within-season walk-forward: the first ~101 games train the first window, so early-season
  estimates are thin. A held-out prior season would be a stronger design.
- The comparison uses the market mid. Trading would pay the spread and fees, which makes the
  model's position worse, not better.
- NBA markets are restricted in Washington, so this is research only.
- One game (0022500644) has no Kalshi event under its played date after a reschedule.
