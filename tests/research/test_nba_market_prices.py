"""Unit tests for src/research/nba_market_prices.py -- real Kalshi KXNBAGAME pre-tip prices.

No real network calls: every test uses a mocked `requests.Session` and/or a
monkeypatched `ScheduleLeagueV2`. Mirrors tests/data/test_kalshi_history.py's
conventions (isolated cache dir, no real sleeping, URL-substring dispatch).
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.analytics.backtest import load_market_prices
from src.data import cache as cache_module
from src.data import kalshi_history as kh
from src.research import nba_market_prices as nmp

LAL_ID = 1610612747
GSW_ID = 1610612744
BOS_ID = 1610612738


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    monkeypatch.setattr(nmp.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(kh.time, "sleep", lambda _seconds: None)


CUTOFF_PAYLOAD = {
    "market_settled_ts": "2026-07-18T00:00:00Z",
    "trades_created_ts": "2026-07-18T00:00:00Z",
    "orders_updated_ts": "2026-07-18T00:00:00Z",
    "market_positions_last_updated_ts": "2026-07-18T00:00:00Z",
}
CUTOFF_MAP = {k: kh._parse_utc(v) for k, v in CUTOFF_PAYLOAD.items()}


def _response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def _mock_session(url_to_payload: dict[str, dict]) -> MagicMock:
    session = MagicMock(spec=["get"])

    def _get(url, params=None, timeout=None):
        for substring, payload in url_to_payload.items():
            if substring in url:
                return _response(payload)
        raise AssertionError(f"Unexpected URL requested: {url} (params={params})")

    session.get.side_effect = _get
    return session


def _raw_market(ticker, event_ticker, result="no", status="finalized"):
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "result": result,
        "status": status,
        "strike_type": "structured",
    }


def _raw_historical_candle(end_ts, bid="0.4200", ask="0.4400", volume="1000", oi="5000"):
    return {
        "end_period_ts": end_ts,
        "yes_bid": {"close": bid},
        "yes_ask": {"close": ask},
        "volume": volume,
        "open_interest": oi,
    }


def _game(game_id="0022500002", game_date="2025-10-21", home=LAL_ID, away=GSW_ID, home_win=1):
    return {
        "game_id": game_id, "game_date": game_date,
        "home_team_id": home, "away_team_id": away,
        "home_pts": 119 if home_win else 100, "away_pts": 100 if home_win else 119,
        "home_win": home_win, "season": "2025-26",
    }


# ---------------------------------------------------------------------------
# Team abbreviation mapping
# ---------------------------------------------------------------------------


class TestTeamAbbrMapping:
    def test_all_30_teams_mapped(self):
        assert len(nmp.TEAM_ID_TO_KALSHI_ABBR) == 30

    def test_known_team_resolves(self):
        assert nmp.kalshi_abbr_for_team(LAL_ID) == "LAL"
        assert nmp.kalshi_abbr_for_team(GSW_ID) == "GSW"
        assert nmp.kalshi_abbr_for_team(BOS_ID) == "BOS"

    def test_unmapped_team_raises_loudly(self):
        with pytest.raises(ValueError, match="No verified Kalshi abbreviation"):
            nmp.kalshi_abbr_for_team(999999999)

    def test_string_team_id_coerced(self):
        # nba_game_results rows sometimes carry ids as strings from sqlite;
        # the map must not silently miss on that.
        assert nmp.kalshi_abbr_for_team(str(LAL_ID)) == "LAL"


# ---------------------------------------------------------------------------
# Ticker construction
# ---------------------------------------------------------------------------


class TestTickerConstruction:
    def test_event_ticker_matches_real_api_example(self):
        # Verified live against the real Kalshi API (see module docstring).
        assert nmp.event_ticker_for_game("2025-10-21", "GSW", "LAL") == "KXNBAGAME-25OCT21GSWLAL"

    def test_event_ticker_accepts_date_object(self):
        from datetime import date
        assert nmp.event_ticker_for_game(date(2026, 6, 13), "NYK", "SAS") == "KXNBAGAME-26JUN13NYKSAS"

    def test_market_ticker_for_home_win(self):
        assert (
            nmp.market_ticker_for_home_win("KXNBAGAME-25OCT21GSWLAL", "LAL")
            == "KXNBAGAME-25OCT21GSWLAL-LAL"
        )

    def test_decision_instant_default_30_minutes(self):
        tip = datetime(2025, 10, 22, 2, 0, 0, tzinfo=timezone.utc)
        assert nmp.decision_instant(tip) == datetime(2025, 10, 22, 1, 30, 0, tzinfo=timezone.utc)

    def test_decision_instant_custom_minutes(self):
        tip = datetime(2025, 10, 22, 2, 0, 0, tzinfo=timezone.utc)
        assert nmp.decision_instant(tip, minutes_before=60) == datetime(2025, 10, 22, 1, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# fetch_season_tipoffs
# ---------------------------------------------------------------------------


class TestFetchSeasonTipoffs:
    def test_reads_gamedatetimeutc(self, monkeypatch):
        fake_payload = {
            "leagueSchedule": {
                "gameDates": [
                    {"games": [{"gameId": "0022500002", "gameDateTimeUTC": "2025-10-22T02:00:00Z"}]},
                    {"games": [{"gameId": "0022500003", "gameDateTimeUTC": "2025-10-22T23:30:00Z"}]},
                ]
            }
        }

        class _FakeSchedule:
            def __init__(self, season, league_id):
                self.season = season
                self.league_id = league_id

            def get_dict(self):
                return fake_payload

        monkeypatch.setattr(nmp, "ScheduleLeagueV2", _FakeSchedule)
        tipoffs = nmp.fetch_season_tipoffs("2025-26")
        assert tipoffs["0022500002"] == datetime(2025, 10, 22, 2, 0, 0, tzinfo=timezone.utc)
        assert tipoffs["0022500003"] == datetime(2025, 10, 22, 23, 30, 0, tzinfo=timezone.utc)

    def test_games_without_tipoff_omitted(self, monkeypatch):
        fake_payload = {"leagueSchedule": {"gameDates": [{"games": [{"gameId": "X"}]}]}}

        class _FakeSchedule:
            def __init__(self, season, league_id):
                pass

            def get_dict(self):
                return fake_payload

        monkeypatch.setattr(nmp, "ScheduleLeagueV2", _FakeSchedule)
        tipoffs = nmp.fetch_season_tipoffs("2025-26")
        assert tipoffs == {}

    def test_caches_across_calls(self, monkeypatch):
        call_count = {"n": 0}
        fake_payload = {
            "leagueSchedule": {
                "gameDates": [{"games": [{"gameId": "0022500002", "gameDateTimeUTC": "2025-10-22T02:00:00Z"}]}]
            }
        }

        class _FakeSchedule:
            def __init__(self, season, league_id):
                call_count["n"] += 1

            def get_dict(self):
                return fake_payload

        monkeypatch.setattr(nmp, "ScheduleLeagueV2", _FakeSchedule)
        nmp.fetch_season_tipoffs("2025-26")
        nmp.fetch_season_tipoffs("2025-26")
        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# fetch_game_quote -- the core no-look-ahead + status logic
# ---------------------------------------------------------------------------


class TestFetchGameQuote:
    TIP = datetime(2025, 10, 22, 2, 0, 0, tzinfo=timezone.utc)  # 2025-10-21 game, per real API example

    def _markets_payload(self):
        return {
            "markets": [
                _raw_market("KXNBAGAME-25OCT21GSWLAL-LAL", "KXNBAGAME-25OCT21GSWLAL", result="yes"),
                _raw_market("KXNBAGAME-25OCT21GSWLAL-GSW", "KXNBAGAME-25OCT21GSWLAL", result="no"),
            ]
        }

    def test_ok_status_picks_most_recent_eligible_candle(self):
        game = _game()
        T = nmp.decision_instant(self.TIP)  # 2025-10-22T01:30:00Z
        candle_before = _raw_historical_candle(int(T.timestamp()) - 3600, bid="0.4000", ask="0.4200")
        candle_at_T = _raw_historical_candle(int(T.timestamp()), bid="0.4500", ask="0.4700")
        candle_after_T = _raw_historical_candle(int(T.timestamp()) + 3600, bid="0.9900", ask="1.0000")
        session = _mock_session({
            "historical/cutoff": CUTOFF_PAYLOAD,
            "historical/markets/KXNBAGAME-25OCT21GSWLAL-LAL/candlesticks": {
                "candlesticks": [candle_before, candle_at_T, candle_after_T]
            },
            "historical/markets": self._markets_payload(),
        })
        quote = nmp.fetch_game_quote(game, self.TIP, session=session, cutoff=CUTOFF_MAP)
        assert quote.status == "ok"
        # Must use the candle ending AT T, never the later one.
        assert quote.yes_bid_cents == 45
        assert quote.yes_ask_cents == 47
        assert quote.mid_probability == pytest.approx(0.46)
        assert quote.market_result == "yes"
        assert quote.ticker == "KXNBAGAME-25OCT21GSWLAL-LAL"

    def test_missing_status_when_no_eligible_candle(self):
        game = _game()
        session = _mock_session({
            "historical/cutoff": CUTOFF_PAYLOAD,
            "historical/markets/KXNBAGAME-25OCT21GSWLAL-LAL/candlesticks": {"candlesticks": []},
            "historical/markets": self._markets_payload(),
        })
        quote = nmp.fetch_game_quote(game, self.TIP, session=session, cutoff=CUTOFF_MAP)
        assert quote.status == "missing"
        assert quote.mid_probability is None

    def test_stale_status_when_candle_too_old(self):
        game = _game()
        T = nmp.decision_instant(self.TIP)
        old_candle = _raw_historical_candle(int(T.timestamp()) - 4 * 3600)  # older than 3h default
        session = _mock_session({
            "historical/cutoff": CUTOFF_PAYLOAD,
            "historical/markets/KXNBAGAME-25OCT21GSWLAL-LAL/candlesticks": {"candlesticks": [old_candle]},
            "historical/markets": self._markets_payload(),
        })
        quote = nmp.fetch_game_quote(game, self.TIP, session=session, cutoff=CUTOFF_MAP)
        assert quote.status == "stale"
        assert quote.mid_probability is None

    def test_no_event_status_when_ticker_not_found(self):
        game = _game()
        session = _mock_session({
            "historical/cutoff": CUTOFF_PAYLOAD,
            "historical/markets": {"markets": []},
        })
        quote = nmp.fetch_game_quote(game, self.TIP, session=session, cutoff=CUTOFF_MAP)
        assert quote.status == "no_event"
        assert quote.mid_probability is None
        assert quote.home_win == 1

    def test_unmapped_team_raises_before_any_network_call(self):
        game = _game(home=999999999)
        session = MagicMock(spec=["get"])
        with pytest.raises(ValueError, match="No verified Kalshi abbreviation"):
            nmp.fetch_game_quote(game, self.TIP, session=session, cutoff=CUTOFF_MAP)
        session.get.assert_not_called()


# ---------------------------------------------------------------------------
# build_market_price_rows / write_market_price_csv / summarize_quotes
# ---------------------------------------------------------------------------


class TestBuildRowsAndCsv:
    def _session_for_games(self, games_and_tips):
        """Build one mock session serving markets+candles for several games."""
        url_map = {"historical/cutoff": CUTOFF_PAYLOAD}
        for game, tip, bid, ask, result in games_and_tips:
            home_abbr = nmp.kalshi_abbr_for_team(game["home_team_id"])
            away_abbr = nmp.kalshi_abbr_for_team(game["away_team_id"])
            event_ticker = nmp.event_ticker_for_game(game["game_date"], away_abbr, home_abbr)
            ticker = nmp.market_ticker_for_home_win(event_ticker, home_abbr)
            T = nmp.decision_instant(tip)
            url_map[f"historical/markets/{ticker}/candlesticks"] = {
                "candlesticks": [_raw_historical_candle(int(T.timestamp()), bid=bid, ask=ask)]
            }
        # All games share the /historical/markets endpoint (no ticker in URL
        # path there); dispatch by event_ticker param instead.
        return url_map, games_and_tips

    def test_build_rows_skips_games_without_tipoff(self, monkeypatch):
        game1 = _game(game_id="G1")
        game2 = _game(game_id="G2")
        tipoffs = {"G1": self.TIP} if hasattr(self, "TIP") else {
            "G1": datetime(2025, 10, 22, 2, 0, 0, tzinfo=timezone.utc)
        }

        def fake_fetch_game_quote(game, tip, **kwargs):
            return nmp.GameMarketQuote(
                game_id=game["game_id"], event_ticker="E", ticker="E-X",
                home_abbr="X", away_abbr="Y", tip_off_utc="2025-10-22T02:00:00Z",
                decision_time_utc="2025-10-22T01:30:00Z", status="ok",
                yes_bid_cents=40, yes_ask_cents=44, mid_probability=0.42,
                volume=100.0, open_interest=200.0, market_result="yes",
                home_win=game.get("home_win"),
            )

        monkeypatch.setattr(nmp, "fetch_game_quote", fake_fetch_game_quote)
        session = MagicMock(spec=["get"])
        session.get.side_effect = lambda url, params=None, timeout=None: _response(CUTOFF_PAYLOAD)
        rows = nmp.build_market_price_rows([game1, game2], tipoffs, session=session)
        assert len(rows) == 1
        assert rows[0]["game_id"] == "G1"

    def test_write_csv_filters_to_usable_rows_and_is_harness_loadable(self, tmp_path):
        rows = [
            {
                "game_id": "G1", "market_yes_probability": 0.62, "event_ticker": "E1",
                "ticker": "E1-X", "home_abbr": "X", "away_abbr": "Y",
                "tip_off_utc": "t", "decision_time_utc": "d", "status": "ok",
                "yes_bid_probability": 0.60, "yes_ask_probability": 0.64,
                "spread_probability": 0.04, "volume": 100.0, "open_interest": 200.0,
                "market_result": "yes", "home_win": 1,
            },
            {
                "game_id": "G2", "market_yes_probability": None, "event_ticker": "E2",
                "ticker": "E2-X", "home_abbr": "X", "away_abbr": "Y",
                "tip_off_utc": "t", "decision_time_utc": "d", "status": "missing",
                "yes_bid_probability": None, "yes_ask_probability": None,
                "spread_probability": None, "volume": None, "open_interest": None,
                "market_result": None, "home_win": 0,
            },
        ]
        out = tmp_path / "prices.csv"
        n_written = nmp.write_market_price_csv(rows, str(out))
        assert n_written == 1

        with open(out) as fh:
            reader = csv.DictReader(fh)
            written_rows = list(reader)
        assert len(written_rows) == 1
        assert written_rows[0]["game_id"] == "G1"

        # Must be directly loadable by the harness with no post-processing.
        prices = load_market_prices(str(out))
        assert prices == {"G1": pytest.approx(0.62)}

    def test_summarize_quotes_coverage_and_calibration(self):
        rows = [
            {"status": "ok", "spread_probability": 0.02, "market_yes_probability": 0.7, "home_win": 1},
            {"status": "ok", "spread_probability": 0.04, "market_yes_probability": 0.3, "home_win": 0},
            {"status": "ok", "spread_probability": 0.06, "market_yes_probability": 0.6, "home_win": 0},
            {"status": "missing", "spread_probability": None, "market_yes_probability": None, "home_win": 1},
            {"status": "no_event", "spread_probability": None, "market_yes_probability": None, "home_win": None},
        ]
        summary = nmp.summarize_quotes(rows, n_games_total=5)
        assert summary["n_games_total"] == 5
        assert summary["n_rows_attempted"] == 5
        assert summary["n_usable"] == 3
        assert summary["n_missing"] == 1
        assert summary["n_no_event"] == 1
        assert summary["n_scored"] == 3
        assert summary["mean_spread_probability"] == pytest.approx(0.04)
        # Two correct-direction calls (0.7 win, 0.3 loss) and one wrong (0.6 loss):
        expected_brier = ((0.7 - 1) ** 2 + (0.3 - 0) ** 2 + (0.6 - 0) ** 2) / 3
        assert summary["market_brier"] == pytest.approx(expected_brier)
        assert summary["market_looks_suspicious"] is False

    def test_summarize_quotes_flags_suspicious_market(self):
        # A market that is badly anti-calibrated (inverted convention, e.g.)
        # must be flagged rather than reported as "no market skill".
        rows = [
            {"status": "ok", "spread_probability": 0.02, "market_yes_probability": 0.1, "home_win": 1},
            {"status": "ok", "spread_probability": 0.02, "market_yes_probability": 0.9, "home_win": 0},
        ]
        summary = nmp.summarize_quotes(rows, n_games_total=2)
        assert summary["market_brier"] > 0.30
        assert summary["market_looks_suspicious"] is True

    def test_summarize_quotes_no_scored_rows(self):
        rows = [{"status": "no_event", "spread_probability": None, "market_yes_probability": None, "home_win": None}]
        summary = nmp.summarize_quotes(rows, n_games_total=1)
        assert summary["n_scored"] == 0
        assert summary["market_brier"] is None
        assert summary["market_looks_suspicious"] is None
