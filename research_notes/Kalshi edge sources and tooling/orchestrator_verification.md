# Orchestrator verification notes (2026-09-18/19)

Checks I ran directly against primary sources, to be treated as confirmed.

## Verified true
- GitHub metadata, via the GitHub API on 2026-09-18:
  - `Jon-Becker/prediction-market-analysis`: 3,844 stars, MIT, pushed 2026-08-10, NOT archived. README confirms "largest publicly available dataset of Polymarket and Kalshi market and trade data", 36 GiB compressed via `make setup`, Parquet storage, indexers for both venues.
  - `Kalshi/kalshi-starter-code-python`: 99 stars, no license, last push 2025-03-07 (stale).
  - `arshka/pykalshi`: 125 stars, MIT, pushed 2026-07-29.
  - `Polymarket/py-clob-client`: 1,231 stars, ARCHIVED, MIT.
  - `warproxxx/poly_data`: 2,343 stars, GPL-3.0, pushed 2026-09-08.
  - `jdkatz21/Prediction_Markets_Public`: 45 stars (NOT high-profile), pushed 2026-06-30.
  - `TexasCoding/kalshi-python-sdk`: 6 stars only — treat "very active" as one person's recent commits, not adoption.
- Kalshi WebSocket authentication: confirmed from Kalshi's own docs index (docs.kalshi.com/llms.txt), which states of the WebSocket connection: "Authentication is required to establish the connection; include API key headers during the WebSocket handshake. Some channels carry only public market data, but the connection itself still requires authentication." **Consequence: a live terminal needs API credentials, unlike every pipeline in this repo so far, which is deliberately credential-free.** Documented channels include orderbook updates, market ticker, public trades, user fills, CF Benchmarks and Pyth value feeds.
- arXiv papers exist with the titles cited (fetched abstract pages 2026-09-19):
  - 2606.07811 — "When Do Markets Fully Process Public Information? Evidence from Real-Time Prediction Markets"
  - 2609.12878 — "The Favorite-Longshot Bias in Prediction Markets: Evidence from Polymarket"
  - 2607.14430 — "Prices, Probabilities, and Parlays: Systematic Bias in Sports Prediction Markets"
  - 2602.19520 — resolves (200), title not separately captured.

## The apparent contradiction, and how it probably resolves
Bürgi, Deng & Whelan ("Makers and Takers") reportedly find makers average **-9.64%** and takers **-31.46%**, with makers on **>=50c contracts averaging +2.6% (SD 33%)**. This repo's maker simulation found passive selling of cheap weather brackets loses (-1.9% ROI) with perfect adverse selection (100% of eventual winners filled).

These are less contradictory than they look:
1. **Both headline maker numbers are losses.** -9.64% is not an edge; the only positive cell is favourites (>=50c).
2. **Selling a 3c YES bracket IS buying a 97c NO contract**, which falls in their >=50c bucket. So the comparable claim is +2.6% with a 33% standard deviation — an enormous dispersion around a thin mean, which is exactly the tail-risk shape this repo measured (418 wins of ~32c against 23 losses of ~946c).
3. **Category matters**: the same paper reportedly finds weather and financials have among the *smallest* bias coefficients, i.e. the categories this repo tested are the ones with least left on the table.
4. **Fee regime**: their data is pre-April-2025, when makers paid no fees. Makers pay fees now, so any historical maker edge is an upper bound.
5. **Selection differs**: this repo posted on the single cheapest ask per event (the most extreme longshot); their maker population is every passive fill across all categories and price levels.

## The experiment this points to (not yet run)
This repo has measured the ask side of cheap brackets. It has NOT fill-simulated the **favourite side**: resting a bid on contracts >=50c. A raw (fill-free) pass over the same data showed buying YES at the bid in the 40-60c band earns +2.68c/contract before fill modelling — the direct analogue of the paper's only positive cell. Running `maker_sim` against that side is a small change and is the highest-value open test.
