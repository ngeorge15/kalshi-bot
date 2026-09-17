"""Real Kalshi ``KXNBAGAME`` pre-tip prices for NBA games, for the model-vs-market harness.

Answers the plumbing half of "would the NBA model have beaten the market?":
:mod:`src.analytics.backtest` already accepts ``market_prices`` (a
``{game_id: market_yes_probability}`` map, via ``load_market_prices``) but no
real odds source has ever fed it. This module builds one from Kalshi's own
``KXNBAGAME`` moneyline series for the 2025-26 season, entirely from public,
unauthenticated ``GET`` endpoints -- see ``research/kalshi-public-data.md``
(NBA section) for the exchange-format research this relies on.

**Ticker construction, verified against the real API (not assumed).**
``KXNBAGAME``'s event ticker is ``KXNBAGAME-{YYMONDD}{AWAY}{HOME}`` and each
event has exactly two markets, ``{event_ticker}-{TEAM}`` (one per team; that
team's "yes" = that team wins). The date component is the game's ET calendar
date -- confirmed by cross-checking ``nba_game_results.game_date`` (from
``nba_api``) against five real 2025-26 event tickers, all of which resolved
on the first try. Kalshi's own team abbreviations were checked against every
distinct team code seen across all 2,898 ``KXNBAGAME`` markets returned by
``/historical/markets?series_ticker=KXNBAGAME``: every one matches
``nba_api.stats.static.teams`` one-for-one, with a single exception ("GUA",
Guangzhou, an October 2025 preseason exhibition game) that is not part of
any NBA franchise and never appears in ``nba_game_results`` (Regular Season
only). :func:`kalshi_abbr_for_team` still raises loudly on any team_id
outside that verified 30-team map rather than falling back to a guess.

**Tip-off time.** Kalshi's market payload carries no reliable pre-game
tip-off field (``expected_expiration_time`` is a generic settlement
estimate, not tip-off -- confirmed by comparing it against a real double-
overtime game's actual close time, which landed near
``expected_expiration_time`` for an unrelated reason: OT pushed the real
game close to the *generic* expiration estimate by coincidence).
:func:`fetch_season_tipoffs` instead reads ``gameDateTimeUTC`` from
``nba_api``'s ``ScheduleLeagueV2`` endpoint (one API call per season) --
confirmed against a real, publicly known tip-off (the 2025-26 season-opening
Warriors-at-Lakers game tipped 10:00pm ET / 2025-10-22T02:00:00Z, exactly
what this endpoint returns for that ``game_id``).

**Decision instant.** ``T = tip_off_utc - minutes_before`` (default 30
minutes), using the exact same no-look-ahead rule
:func:`src.data.kalshi_history.price_at_instant` already enforces for the
weather backtest: the most recent candle whose end is at or before ``T``,
never a later one, and never interpolated; a candle older than
``max_staleness_hours`` (default 3h) is reported ``"stale"`` and skipped
rather than reused.

**Reuse, and what genuinely couldn't be reused.** This module imports
:mod:`src.data.kalshi_history`'s session/session-config, cache-namespaced
HTTP plumbing, ``get_historical_cutoff``, the historical/live partition
predicate ``_use_historical``, candlestick normalization ``_normalize_candle``,
and the no-look-ahead ``price_at_instant``/``PriceAtInstant`` -- none of
those are weather-specific. Two of that module's functions, however,
``fetch_event_markets`` and ``fetch_candlesticks``, hard-require a series in
``SERIES_STATIONS`` (``_station_for_series`` raises for anything else) and
compute their historical/live reference time from a *weather* event
ticker's date plus a station's fixed UTC offset -- neither applies to
``KXNBAGAME``, whose reference time is a real per-game tip-off, not a
station's local midnight. This module therefore has its own
``_fetch_markets_for_event`` / ``_fetch_candlesticks_for_market`` that
apply the identical decide-before-you-call-never-from-an-empty-response
rule using ``_use_historical`` directly, rather than copying
``kalshi_history``'s weather-specific bodies.

**No market comparison is computed here.** This module only produces the
CSV :func:`src.analytics.backtest.load_market_prices` reads; running
``run_backtest(games, market_prices=...)`` and interpreting the result is a
separate step (see ``research/nba-market-comparison.md``).

Usage::

    from src.data.nba.history import get_historical_results
    from src.research.nba_market_prices import (
        fetch_season_tipoffs, build_market_price_rows, write_market_price_csv,
        summarize_quotes,
    )

    games = get_historical_results(season="2025-26")
    tipoffs = fetch_season_tipoffs("2025-26")
    rows = build_market_price_rows(games, tipoffs)
    write_market_price_csv(rows, "data/nba_market_prices_2025-26.csv")
    print(summarize_quotes(rows, n_games_total=len(games)))
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

import numpy as np
import requests
from nba_api.stats.endpoints import ScheduleLeagueV2
from nba_api.stats.static import teams as nba_static_teams

from src.data.cache import cache_get, cache_set
from src.data.kalshi_history import (
    BASE_URL,
    DEFAULT_MAX_STALENESS_HOURS,
    LONG_CACHE_TTL_SECONDS,
    REQUEST_TIMEOUT,
    PriceAtInstant,
    _format_utc,
    _get_session,
    _MONTH_ABBR,
    _normalize_candle,
    _parse_utc,
    _use_historical,
    get_historical_cutoff,
    price_at_instant,
)

logger = logging.getLogger(__name__)

NBA_SERIES = "KXNBAGAME"

# Politeness delay between Kalshi requests made by this module. Bumped above
# kalshi_history's own SLEEP_SECONDS (1.0s): a separate background process
# fetches Kalshi data continuously while this module may be running (see
# research/nba-market-comparison.md) -- keep this module's own share of
# request volume modest, never below 2s.
NBA_SLEEP_SECONDS = 2.0

# Decision instant default: tip-off minus this many minutes.
DEFAULT_MINUTES_BEFORE_TIPOFF = 30

# How far past tip-off an NBA game's market realistically settles by
# (including overtime); used only to choose the historical/live partition
# side -- decided before the request, never from an empty response, same
# rule kalshi_history documents for weather. NBA games rarely run past 3.5
# hours including a broadcast pregame/postgame buffer; 6h is a deliberately
# generous margin, not a measured game-length statistic.
GAME_SETTLEMENT_BUFFER_HOURS = 6

DEFAULT_PERIOD_INTERVAL_MINUTES = 60
DEFAULT_LOOKBACK_HOURS = 24

# A completed season's schedule is immutable; an in-progress season's is
# not (postponements/reschedules happen), so this is a long-but-not-forever
# cache, unlike kalshi_history's LONG_CACHE_TTL_SECONDS for settled markets.
SCHEDULE_CACHE_NAMESPACE = "nba_schedule_league_v2"
SCHEDULE_CACHE_TTL_SECONDS = 180 * 24 * 3600  # ~6 months

NBA_MARKETS_CACHE_NAMESPACE = "nba_kalshi_markets"
NBA_CANDLES_CACHE_NAMESPACE = "nba_kalshi_candles"


# ---------------------------------------------------------------------------
# Team abbreviation mapping -- explicit, fails loudly on anything unmapped.
# ---------------------------------------------------------------------------


def _build_team_abbr_map() -> dict[int, str]:
    """Build ``{nba_api team_id: Kalshi ticker abbreviation}`` for all 30 franchises.

    See the module docstring for how this identity mapping (nba_api's own
    abbreviation) was verified against real Kalshi ticker data.
    """
    return {int(t["id"]): str(t["abbreviation"]) for t in nba_static_teams.get_teams()}


TEAM_ID_TO_KALSHI_ABBR: dict[int, str] = _build_team_abbr_map()


def kalshi_abbr_for_team(team_id: int) -> str:
    """Return the Kalshi ticker abbreviation for an ``nba_api`` team_id.

    Args:
        team_id: An NBA team's integer ``nba_api`` id (e.g. from
            ``nba_game_results.home_team_id``).

    Returns:
        The team's Kalshi ticker abbreviation (e.g. ``"LAL"``).

    Raises:
        ValueError: If *team_id* is not one of the 30 known NBA franchises.
            Never guessed, never defaulted -- see module docstring.
    """
    abbr = TEAM_ID_TO_KALSHI_ABBR.get(int(team_id))
    if abbr is None:
        raise ValueError(
            f"No verified Kalshi abbreviation for nba team_id={team_id!r}; "
            f"known ids: {sorted(TEAM_ID_TO_KALSHI_ABBR)}. Refusing to guess -- "
            "add it to TEAM_ID_TO_KALSHI_ABBR only after confirming the "
            "mapping against a real Kalshi KXNBAGAME ticker."
        )
    return abbr


# ---------------------------------------------------------------------------
# Ticker construction
# ---------------------------------------------------------------------------


def _as_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def event_ticker_for_game(game_date: str | date, away_abbr: str, home_abbr: str) -> str:
    """Build ``KXNBAGAME-{YYMONDD}{AWAY}{HOME}`` for one game.

    Args:
        game_date: The game's ET calendar date (matches
            ``nba_game_results.game_date``; see module docstring for why
            this is confirmed, not assumed, to be the ticker's date
            component).
        away_abbr, home_abbr: Kalshi ticker abbreviations (see
            :func:`kalshi_abbr_for_team`).

    Returns:
        The event ticker string.
    """
    day = _as_date(game_date)
    return f"{NBA_SERIES}-{day.strftime('%y')}{_MONTH_ABBR[day.month]}{day.day:02d}{away_abbr}{home_abbr}"


def market_ticker_for_home_win(event_ticker: str, home_abbr: str) -> str:
    """Return the ``{event_ticker}-{home_abbr}`` market ticker (home team's "yes" = home wins)."""
    return f"{event_ticker}-{home_abbr}"


def decision_instant(tip_off_utc: datetime, minutes_before: int = DEFAULT_MINUTES_BEFORE_TIPOFF) -> datetime:
    """Return ``T = tip_off_utc - minutes_before`` -- the decision instant to quote at."""
    return tip_off_utc - timedelta(minutes=minutes_before)


# ---------------------------------------------------------------------------
# Tip-off times (nba_api ScheduleLeagueV2, one call per season)
# ---------------------------------------------------------------------------


def fetch_season_tipoffs(season: str) -> dict[str, datetime]:
    """Map every ``nba_api`` ``game_id`` in *season* to its scheduled tip-off (UTC).

    Uses ``ScheduleLeagueV2`` (one API call for the whole season).
    ``gameDateTimeUTC`` on each game is nba.com's own scheduled start time --
    see module docstring for the real-game spot check this relies on.

    Args:
        season: NBA season string, e.g. ``"2025-26"``.

    Returns:
        Dict mapping ``game_id`` (str, matches
        ``nba_game_results.game_id``) to an aware UTC ``datetime``. Games
        missing a ``gameDateTimeUTC`` (should not happen for a played game)
        are omitted, not defaulted.
    """
    cache_params = {"season": season}
    cached = cache_get(SCHEDULE_CACHE_NAMESPACE, cache_params, ttl_seconds=SCHEDULE_CACHE_TTL_SECONDS)
    if cached is None:
        time.sleep(0.5)  # nba_api rate-limit convention (see src/data/nba/teams.py).
        raw = ScheduleLeagueV2(season=season, league_id="00").get_dict()
        game_dates = raw.get("leagueSchedule", {}).get("gameDates", [])
        tipoffs_raw: dict[str, str] = {}
        for game_day in game_dates:
            for g in game_day.get("games", []):
                game_id = g.get("gameId")
                tip = g.get("gameDateTimeUTC")
                if game_id and tip:
                    tipoffs_raw[game_id] = tip
        cache_set(SCHEDULE_CACHE_NAMESPACE, cache_params, tipoffs_raw)
        cached = tipoffs_raw
    return {game_id: _parse_utc(ts) for game_id, ts in cached.items()}


# ---------------------------------------------------------------------------
# Kalshi fetch -- own historical/live partition handling (see module docstring
# for why kalshi_history's fetch_event_markets/fetch_candlesticks don't fit).
# ---------------------------------------------------------------------------


def _fetch_markets_for_event(
    event_ticker: str,
    reference_time: datetime,
    session: requests.Session,
    cutoff: dict[str, datetime],
) -> list[dict]:
    """Fetch raw market payloads for one ``KXNBAGAME`` event.

    Chooses ``/historical/markets`` or ``/markets`` from *reference_time*
    (an estimate of when the event's markets settle) vs.
    ``get_historical_cutoff()`` -- decided before the request, never from an
    empty response (see module docstring / ``research/kalshi-public-data.md``).
    """
    use_historical = _use_historical(reference_time, cutoff["market_settled_ts"])
    endpoint = f"{BASE_URL}/historical/markets" if use_historical else f"{BASE_URL}/markets"
    cache_params = {"event_ticker": event_ticker, "endpoint": "historical" if use_historical else "live"}

    if use_historical:
        cached = cache_get(NBA_MARKETS_CACHE_NAMESPACE, cache_params, ttl_seconds=LONG_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached

    resp = session.get(endpoint, params={"event_ticker": event_ticker}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    time.sleep(NBA_SLEEP_SECONDS)
    markets = resp.json().get("markets", [])

    if use_historical:
        cache_set(NBA_MARKETS_CACHE_NAMESPACE, cache_params, markets)
    return markets


def _fetch_candlesticks_for_market(
    ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int,
    reference_time: datetime,
    session: requests.Session,
    cutoff: dict[str, datetime],
) -> list[dict]:
    """Fetch and normalize candlesticks for one ``KXNBAGAME`` market. See :func:`_fetch_markets_for_event`."""
    use_historical = _use_historical(reference_time, cutoff["market_settled_ts"])
    url = (
        f"{BASE_URL}/historical/markets/{ticker}/candlesticks"
        if use_historical
        else f"{BASE_URL}/series/{NBA_SERIES}/markets/{ticker}/candlesticks"
    )
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}
    cache_params = {
        "ticker": ticker, "start_ts": start_ts, "end_ts": end_ts,
        "period_interval": period_interval, "endpoint": "historical" if use_historical else "live",
    }

    if use_historical:
        cached = cache_get(NBA_CANDLES_CACHE_NAMESPACE, cache_params, ttl_seconds=LONG_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached

    resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    time.sleep(NBA_SLEEP_SECONDS)
    raw_candles = resp.json().get("candlesticks", [])
    normalized = [_normalize_candle(c, use_historical) for c in raw_candles]

    if use_historical:
        cache_set(NBA_CANDLES_CACHE_NAMESPACE, cache_params, normalized)
    return normalized


# ---------------------------------------------------------------------------
# Per-game quote
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameMarketQuote:
    """One game's Kalshi pre-tip quote, or why it is unusable.

    Attributes:
        status: ``"ok"``, ``"missing"`` (no eligible/complete candle),
            ``"stale"`` (eligible candle too old), or ``"no_event"`` (the
            expected event/market did not resolve against the API at all).
    """

    game_id: str
    event_ticker: str
    ticker: str
    home_abbr: str
    away_abbr: str
    tip_off_utc: str
    decision_time_utc: str
    status: str
    yes_bid_cents: Optional[int] = None
    yes_ask_cents: Optional[int] = None
    mid_probability: Optional[float] = None
    volume: Optional[float] = None
    open_interest: Optional[float] = None
    market_result: Optional[str] = None
    home_win: Optional[int] = None


def fetch_game_quote(
    game: dict[str, Any],
    tip_off_utc: datetime,
    minutes_before: int = DEFAULT_MINUTES_BEFORE_TIPOFF,
    session: Optional[requests.Session] = None,
    cutoff: Optional[dict[str, datetime]] = None,
    period_interval: int = DEFAULT_PERIOD_INTERVAL_MINUTES,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    max_staleness_hours: float = DEFAULT_MAX_STALENESS_HOURS,
) -> GameMarketQuote:
    """Fetch one game's Kalshi pre-tip quote for the home team's "yes" market.

    Args:
        game: A ``nba_game_results``-shaped dict with at least ``game_id``,
            ``game_date``, ``home_team_id``, ``away_team_id``, ``home_win``.
        tip_off_utc: The game's scheduled tip-off (from
            :func:`fetch_season_tipoffs`).
        minutes_before: Minutes before tip-off to quote at (T).
        session: Optional ``requests.Session`` (tests; production default
            reuses ``kalshi_history``'s module-level session).
        cutoff: Optional pre-fetched ``get_historical_cutoff()`` result, to
            avoid one extra call per game when fetching many.
        period_interval, lookback_hours, max_staleness_hours: Passed through
            to the candlestick fetch / :func:`~src.data.kalshi_history.price_at_instant`.

    Returns:
        A :class:`GameMarketQuote`. Never raises for a missing/stale/absent
        market -- those are reported via ``status``, not exceptions. Does
        raise (via :func:`kalshi_abbr_for_team`) if either team is unmapped.
    """
    sess = session if session is not None else _get_session()
    cutoff_map = cutoff if cutoff is not None else get_historical_cutoff(sess)

    home_abbr = kalshi_abbr_for_team(game["home_team_id"])
    away_abbr = kalshi_abbr_for_team(game["away_team_id"])
    event_ticker = event_ticker_for_game(game["game_date"], away_abbr, home_abbr)
    ticker = market_ticker_for_home_win(event_ticker, home_abbr)

    T = decision_instant(tip_off_utc, minutes_before)
    reference_time = tip_off_utc + timedelta(hours=GAME_SETTLEMENT_BUFFER_HOURS)

    markets = _fetch_markets_for_event(event_ticker, reference_time, sess, cutoff_map)
    market = next((m for m in markets if m.get("ticker") == ticker), None)

    if market is None:
        return GameMarketQuote(
            game_id=game["game_id"], event_ticker=event_ticker, ticker=ticker,
            home_abbr=home_abbr, away_abbr=away_abbr,
            tip_off_utc=_format_utc(tip_off_utc), decision_time_utc=_format_utc(T),
            status="no_event", home_win=game.get("home_win"),
        )

    result = market.get("result")
    result = result if result in ("yes", "no") else None

    start_ts = int((T - timedelta(hours=lookback_hours)).timestamp())
    end_ts = int(T.timestamp())
    candles = _fetch_candlesticks_for_market(
        ticker, start_ts, end_ts, period_interval, reference_time, sess, cutoff_map,
    )
    price: PriceAtInstant = price_at_instant(candles, T, max_staleness_hours)

    mid = None
    if price.status == "ok":
        mid = (price.yes_bid_cents + price.yes_ask_cents) / 200.0

    return GameMarketQuote(
        game_id=game["game_id"], event_ticker=event_ticker, ticker=ticker,
        home_abbr=home_abbr, away_abbr=away_abbr,
        tip_off_utc=_format_utc(tip_off_utc), decision_time_utc=_format_utc(T),
        status=price.status,
        yes_bid_cents=price.yes_bid_cents, yes_ask_cents=price.yes_ask_cents,
        mid_probability=mid, volume=price.volume, open_interest=price.open_interest,
        market_result=result, home_win=game.get("home_win"),
    )


# ---------------------------------------------------------------------------
# Bulk fetch + CSV (the harness's load_market_prices format, plus diagnostics)
# ---------------------------------------------------------------------------

# load_market_prices (src.analytics.backtest) only requires 'game_id' and
# 'market_yes_probability' -- everything else here is tolerated (DictReader
# keeps only the columns it looks up) and exists so a later cost-aware
# comparison can use executable bid/ask prices instead of the mid.
CSV_FIELDNAMES = [
    "game_id", "market_yes_probability",
    "event_ticker", "ticker", "home_abbr", "away_abbr",
    "tip_off_utc", "decision_time_utc", "status",
    "yes_bid_probability", "yes_ask_probability", "spread_probability",
    "volume", "open_interest", "market_result", "home_win",
]


def build_market_price_rows(
    games: list[dict[str, Any]],
    tipoffs: dict[str, datetime],
    minutes_before: int = DEFAULT_MINUTES_BEFORE_TIPOFF,
    session: Optional[requests.Session] = None,
    period_interval: int = DEFAULT_PERIOD_INTERVAL_MINUTES,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    max_staleness_hours: float = DEFAULT_MAX_STALENESS_HOURS,
) -> list[dict[str, Any]]:
    """Fetch a Kalshi pre-tip quote for every game in *games* that has a tip-off.

    Games with no entry in *tipoffs* are logged and skipped entirely (not
    written as a row) -- this should not happen for a played regular-season
    game, and is reported via the return value's length being short of
    ``len(games)``, never silently.

    Args:
        games: ``nba_game_results``-shaped dicts (see :func:`fetch_game_quote`).
        tipoffs: ``{game_id: tip_off_utc}`` from :func:`fetch_season_tipoffs`.
        minutes_before, period_interval, lookback_hours, max_staleness_hours:
            Passed through to :func:`fetch_game_quote`.
        session: Optional shared session (tests; production builds one cutoff
            fetch + reuses ``kalshi_history``'s module session otherwise).

    Returns:
        One CSV-ready row (dict, see :data:`CSV_FIELDNAMES`) per game with a
        known tip-off, in the same order as *games*.
    """
    sess = session if session is not None else _get_session()
    cutoff_map = get_historical_cutoff(sess)

    rows: list[dict[str, Any]] = []
    n_no_tipoff = 0
    for game in games:
        gid = game["game_id"]
        tip = tipoffs.get(gid)
        if tip is None:
            n_no_tipoff += 1
            logger.warning("No scheduled tip-off for game_id=%s; skipping entirely", gid)
            continue

        quote = fetch_game_quote(
            game, tip, minutes_before=minutes_before, session=sess, cutoff=cutoff_map,
            period_interval=period_interval, lookback_hours=lookback_hours,
            max_staleness_hours=max_staleness_hours,
        )
        yes_bid_prob = quote.yes_bid_cents / 100.0 if quote.yes_bid_cents is not None else None
        yes_ask_prob = quote.yes_ask_cents / 100.0 if quote.yes_ask_cents is not None else None
        spread_prob = (
            yes_ask_prob - yes_bid_prob if yes_bid_prob is not None and yes_ask_prob is not None else None
        )
        rows.append({
            "game_id": quote.game_id,
            "market_yes_probability": quote.mid_probability,
            "event_ticker": quote.event_ticker,
            "ticker": quote.ticker,
            "home_abbr": quote.home_abbr,
            "away_abbr": quote.away_abbr,
            "tip_off_utc": quote.tip_off_utc,
            "decision_time_utc": quote.decision_time_utc,
            "status": quote.status,
            "yes_bid_probability": yes_bid_prob,
            "yes_ask_probability": yes_ask_prob,
            "spread_probability": spread_prob,
            "volume": quote.volume,
            "open_interest": quote.open_interest,
            "market_result": quote.market_result,
            "home_win": quote.home_win,
        })

    if n_no_tipoff:
        logger.warning(
            "%d of %d games had no scheduled tip-off and were skipped entirely",
            n_no_tipoff, len(games),
        )
    return rows


def write_market_price_csv(rows: list[dict[str, Any]], path: str) -> int:
    """Write the harness-loadable CSV: only rows with a usable quote (``status == "ok"``).

    A row with ``status != "ok"`` has no numeric ``market_yes_probability``
    (there is no price to report), and
    :func:`src.analytics.backtest.load_market_prices` requires every row's
    ``market_yes_probability`` to parse as a finite float in ``[0, 1]`` --
    so writing such a row would make the whole file unloadable. Unusable
    games are reported via :func:`summarize_quotes` instead, computed from
    *rows* directly (call it on the full, unfiltered list before this
    function's filtering).

    Args:
        rows: The full row list from :func:`build_market_price_rows`
            (usable and unusable mixed).
        path: Output CSV path.

    Returns:
        Number of rows actually written (``status == "ok"`` count).
    """
    usable = [r for r in rows if r["status"] == "ok"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(usable)
    return len(usable)


# ---------------------------------------------------------------------------
# Sanity / calibration checks
# ---------------------------------------------------------------------------


def summarize_quotes(rows: list[dict[str, Any]], n_games_total: int) -> dict[str, Any]:
    """Coverage and market-calibration sanity checks for a batch of quote rows.

    Per the module's honesty requirements: a market Brier much worse than
    ~0.25 (a coin flip) on real outcomes almost always means the ticker
    mapping or the yes-side convention is inverted, not that Kalshi's market
    is unskilled -- so this is flagged explicitly (``market_looks_suspicious``)
    rather than silently reported as "the market has no edge."

    Args:
        rows: The full row list from :func:`build_market_price_rows`
            (usable and unusable mixed -- coverage is computed over all of
            them; calibration is computed only over usable rows with a
            known ``home_win``).
        n_games_total: Total games considered before any Kalshi fetch (for
            context on how many games never even got a row -- e.g. missing
            tip-off).

    Returns:
        Dict with mapping/coverage counts, ``mean_spread_probability``, and
        (when at least one usable+scored row exists) ``market_brier``,
        ``baseline_half_brier``, ``home_base_rate``,
        ``home_base_rate_brier``, and ``market_looks_suspicious``.
    """
    n_rows = len(rows)
    status_counts = Counter(r["status"] for r in rows)
    usable = [r for r in rows if r["status"] == "ok"]
    spreads = [r["spread_probability"] for r in usable if r["spread_probability"] is not None]
    mean_spread = float(np.mean(spreads)) if spreads else None

    scored = [r for r in usable if r["home_win"] in (0, 1) and r["market_yes_probability"] is not None]

    summary: dict[str, Any] = {
        "n_games_total": n_games_total,
        "n_rows_attempted": n_rows,
        "n_usable": len(usable),
        "n_missing": status_counts.get("missing", 0),
        "n_stale": status_counts.get("stale", 0),
        "n_no_event": status_counts.get("no_event", 0),
        "mean_spread_probability": mean_spread,
        "n_scored": len(scored),
    }

    if scored:
        market_probs = np.array([r["market_yes_probability"] for r in scored], dtype=float)
        actuals = np.array([r["home_win"] for r in scored], dtype=float)
        market_brier = float(np.mean((market_probs - actuals) ** 2))
        home_base_rate = float(np.mean(actuals))
        summary.update({
            "market_brier": market_brier,
            "baseline_half_brier": float(np.mean((0.5 - actuals) ** 2)),
            "home_base_rate": home_base_rate,
            "home_base_rate_brier": float(np.mean((home_base_rate - actuals) ** 2)),
            # A real, correctly-mapped moneyline market beats a coin flip
            # handily; if it doesn't, check the mapping/convention before
            # reporting "the market has no skill".
            "market_looks_suspicious": market_brier > 0.30,
        })
    else:
        summary.update({
            "market_brier": None, "baseline_half_brier": None,
            "home_base_rate": None, "home_base_rate_brier": None,
            "market_looks_suspicious": None,
        })
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m src.research.nba_market_prices",
        description="Fetch real Kalshi KXNBAGAME pre-tip prices for a season's games.",
    )
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument("--minutes-before", type=int, default=DEFAULT_MINUTES_BEFORE_TIPOFF)
    parser.add_argument("--stride", type=int, default=1, help="Take every Nth game (spread across the season)")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N (post-stride) games")
    args = parser.parse_args(argv)

    from src.data.nba.history import get_historical_results  # local import: avoid a hard SQLite dep at module load

    games = get_historical_results(season=args.season)
    if args.stride > 1:
        games = games[:: args.stride]
    if args.limit is not None:
        games = games[: args.limit]

    tipoffs = fetch_season_tipoffs(args.season)
    rows = build_market_price_rows(games, tipoffs, minutes_before=args.minutes_before)
    n_written = write_market_price_csv(rows, args.output)
    summary = summarize_quotes(rows, n_games_total=len(games))
    print(json.dumps(summary, indent=2))
    print(f"Wrote {n_written} usable rows (of {len(rows)} attempted, {len(games)} games) to {args.output}")


if __name__ == "__main__":
    main()
