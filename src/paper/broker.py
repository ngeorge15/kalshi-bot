"""Transactional, long-only paper broker driven by timestamped local events.

No Kalshi client, credentials, HTTP calls, or real-money execution paths exist
here. Orders consume later observed asks; touching a bid never implies a fill.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Callable, Iterator

from src.paper.config import PaperConfig
from src.paper.events import cluster_summary, degenerate_clustering
from src.paper.fees import trading_fee_cents
from src.paper.uncertainty import cluster_bootstrap_ci, paired_brier_by_event


def utc(value: str) -> datetime:
    """Parse an explicitly timezone-aware timestamp and normalize to UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def integer(value: int, name: str, low: int = 0, high: int | None = None) -> int:
    """Validate integral counts and cents without accepting booleans."""
    if type(value) is not int or value < low or (high is not None and value > high):
        raise ValueError(f"Invalid {name}")
    return value


def label(value: str, name: str) -> str:
    """Require a nonempty identifier."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value



def _clustered_scores(pairs: list[dict]) -> dict:
    """Summarise one model's paired Brier scores with events as the unit.

    Correlated bracket markets on the same event are not independent
    observations, so the per-market mean understates uncertainty.  This weights
    each event equally and resamples whole events for the interval.

    Args:
        pairs: Rows with ``event_key``, ``model_brier`` and ``market_brier``.

    Returns:
        Event-weighted point estimates, a bootstrap interval (``ci_low`` and
        ``ci_high`` are ``None`` below two events, where variability cannot be
        estimated), cluster shape, and a degeneracy verdict.
    """
    paired = paired_brier_by_event(pairs)
    interval = cluster_bootstrap_ci(pairs)
    return {"n_events": paired["n_events"], "n_markets": paired["n_markets"],
            "model_brier": paired["model_brier"], "market_brier": paired["market_brier"],
            "brier_improvement": paired["brier_improvement"],
            "ci_low": interval["ci_low"], "ci_high": interval["ci_high"],
            "confidence": interval["confidence"], "n_resamples": interval["n_resamples"],
            "clustering": cluster_summary(pairs), "degenerate": degenerate_clustering(pairs)}


class PaperBroker:
    """Persist one paper experiment in its own SQLite file.

    Each event is atomic and idempotent by event_id. BEGIN IMMEDIATE serializes
    cash reservations across processes. Reopening cannot change run assumptions.
    """

    def __init__(self, db_path: str, config: PaperConfig | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.db_path = str(db_path)
        if self.db_path == ":memory:":
            raise ValueError("Use a file so paper state survives reconnects")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        with self._connect() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "paper_account" not in tables:
                raise ValueError("Refusing a non-paper database; choose a separate file")
            conn.executescript(Path(__file__).with_name("schema.sql").read_text())
            conn.execute("BEGIN IMMEDIATE")
            account = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
            if account is None:
                self.config = config or PaperConfig()
                conn.execute("INSERT INTO paper_account(id, config_json, cash_cents) VALUES(1, ?, ?)",
                             (json.dumps(self.config.to_dict(), sort_keys=True), self.config.initial_cash_cents))
            else:
                settings = json.loads(account["config_json"])
                settings["allowed_market_types"] = tuple(settings["allowed_market_types"])
                self.config = PaperConfig(**settings)
                if config is not None and config != self.config:
                    raise ValueError("Experiment settings differ; create a new paper database")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def process(self, event: dict) -> dict:
        """Apply a quote, forecast, order, cancel, settlement, or halt event.

        Replay/synthetic use event time. Forward mode additionally checks event
        arrival against the wall clock; this does not attest to model provenance.
        """
        event_id = label(event.get("event_id"), "event_id")
        payload = json.dumps(event, sort_keys=True, allow_nan=False)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT * FROM paper_events WHERE event_id=?", (event_id,)).fetchone()
            if previous:
                if previous["payload"] != payload:
                    raise ValueError("event_id reused with different content")
                return json.loads(previous["result"])
            now = utc(event["at"])
            account = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
            if account["last_event_at"] and now < utc(account["last_event_at"]):
                raise ValueError("Events must arrive in chronological order")
            if self.config.run_kind == "forward":
                age = (self._clock() - now).total_seconds()
                if not 0 <= age <= self.config.max_quote_age_seconds:
                    raise ValueError("Forward event is stale or in the future")
            self._expire(conn, now)
            handlers = {"quote": self._quote, "forecast": self._forecast,
                        "order": self._order, "cancel": self._cancel,
                        "settlement": self._settle, "halt": self._halt,
                        "weather_forecast": self._weather_forecast, "unavailable": self._unavailable}
            if event.get("type") not in handlers:
                raise ValueError("Unknown paper event type")
            result = handlers[event["type"]](conn, event, now)
            conn.execute("UPDATE paper_account SET last_event_at=? WHERE id=1", (now.isoformat(),))
            conn.execute("INSERT INTO paper_events VALUES(?, ?, ?)",
                         (event_id, payload, json.dumps(result, sort_keys=True)))
            return result

    def _expire(self, conn: sqlite3.Connection, now: datetime) -> None:
        conn.execute("""UPDATE paper_orders SET status='canceled', remaining=0
            WHERE remaining>0 AND ticker IN
            (SELECT ticker FROM paper_markets WHERE close_at<=?)""", (now.isoformat(),))

    def _fee_cents(self, price_cents: int, count: int) -> int:
        """Fee charged for `count` contracts at `price_cents`, per the configured model.

        A paper order rests in ``paper_orders`` until a later quote's ask
        depth fills it (see the module docstring), but the fill itself
        consumes that ask depth outright -- it does not add liquidity and
        wait to be crossed by someone else. Consuming resting depth is a
        taker action on a real exchange regardless of when it happens in
        wall-clock time, so every paper fill is charged the taker rate
        (``is_maker=False``); this is also the conservative (higher-fee)
        choice on any series that carries a maker discount.
        """
        if self.config.fee_model == "flat":
            return count * self.config.fee_per_contract_cents
        return trading_fee_cents(price_cents, count, fee_type=self.config.fee_type,
                                  fee_multiplier=self.config.fee_multiplier, is_maker=False)

    def _fee_reserve_cents(self, limit_cents: int, count: int) -> int:
        """Upper bound on the fees an order at `limit_cents` can incur across its fills.

        A fill may execute at any price up to the limit, and the quadratic fee
        peaks at 50 cents, so a 70-cent limit filling at 50 pays more than a fee
        priced at the limit. Fills are also rounded up individually, so the
        bound charges each contract its own rounded fee at the worst price.
        """
        if self.config.fee_model == "flat":
            return count * self.config.fee_per_contract_cents
        return count * self._fee_cents(min(limit_cents, 50), 1)

    def _market(self, conn: sqlite3.Connection, ticker: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM paper_markets WHERE ticker=?", (ticker,)).fetchone()
        if row is None:
            raise ValueError("A market quote must precede an order or forecast")
        return row

    def _quote(self, conn: sqlite3.Connection, event: dict, now: datetime) -> dict:
        ticker = label(event.get("ticker"), "ticker")
        quote_at, close_at = utc(event["observed_at"]), utc(event["close_at"])
        if not 0 <= (now - quote_at).total_seconds() <= self.config.max_quote_age_seconds:
            raise ValueError("Quote is stale or in the future")
        market_type = label(event.get("market_type"), "market_type")
        event_key = label(event.get("event_key"), "event_key")
        if type(event.get("available")) is not bool:
            raise ValueError("Quote must explicitly specify available true or false")
        levels = {}
        for side in ("yes", "no"):
            prices = set()
            levels[side] = []
            for price, count in event.get(f"{side}_asks", []):
                integer(price, "ask price", 1, 99)
                integer(count, "ask quantity", 1)
                if price in prices:
                    raise ValueError("Duplicate depth price")
                prices.add(price)
                levels[side].append([price, count])
            levels[side].sort()
        if levels["yes"] and levels["no"] and levels["yes"][0][0] + levels["no"][0][0] < 100:
            raise ValueError("Crossed complementary book")
        old = conn.execute("SELECT * FROM paper_markets WHERE ticker=?", (ticker,)).fetchone()
        if old:
            if (old["market_type"], old["event_key"], old["close_at"]) != (market_type, event_key, close_at.isoformat()):
                raise ValueError("Market metadata cannot change during an experiment")
            if json.loads(old["quote_json"]).get("weather_spec") != event.get("weather_spec"):
                raise ValueError("Weather contract specification cannot change")
            if old["result"]:
                raise ValueError("Market is already settled")
            if quote_at <= utc(old["quote_at"]):
                raise ValueError("Quote observation must advance; depth cannot be replayed")
        # Keep a shadow of consumed depth. An unchanged repeated snapshot does
        # not replenish liquidity consumed by our simulated orders.
        original_levels = {side: [list(level) for level in rows] for side, rows in levels.items()}
        if old:
            previous = json.loads(old["quote_json"])
            for side in ("yes", "no"):
                old_counts = dict(previous[f"{side}_asks"])
                old_remaining = dict(previous["remaining_depth"][side])
                for level in levels[side]:
                    price, count = level
                    level[1] = min(count, old_remaining.get(price, 0) + max(0, count-old_counts.get(price, 0)))
        quote = {**event, "yes_asks": original_levels["yes"], "no_asks": original_levels["no"],
                 "remaining_depth": levels}
        conn.execute("""INSERT INTO paper_markets(ticker, market_type, event_key, close_at, quote_json, quote_at)
            VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(ticker) DO UPDATE SET
            quote_json=excluded.quote_json, quote_at=excluded.quote_at""",
                     (ticker, market_type, event_key, close_at.isoformat(), json.dumps(quote), quote_at.isoformat()))
        if not event["available"]:
            conn.execute("UPDATE paper_orders SET status='canceled', remaining=0 WHERE ticker=? AND remaining>0", (ticker,))
        account = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        if account["halted"] or not event["available"] or quote_at >= close_at or now >= close_at:
            return {"status": "observed", "fills": []}
        fills = []
        orders = conn.execute("""SELECT * FROM paper_orders WHERE ticker=? AND remaining>0
            ORDER BY created_at, rowid""", (ticker,)).fetchall()
        for order in orders:
            # A quote used to make a decision cannot also fill that decision.
            if quote_at <= utc(order["created_at"]):
                continue
            remaining = order["remaining"]
            for level in levels[order["side"]]:
                price = level[0] + self.config.slippage_cents
                if price > order["limit_cents"] or not level[1] or not remaining:
                    continue
                count = min(level[1], remaining)
                fee = self._fee_cents(price, count)
                conn.execute("""INSERT INTO paper_fills(order_id, quote_id, price_cents, quantity, fee_cents, filled_at)
                    VALUES(?, ?, ?, ?, ?, ?)""", (order["order_id"], event["event_id"], price, count, fee, now.isoformat()))
                conn.execute("UPDATE paper_account SET cash_cents=cash_cents-? WHERE id=1", (price * count + fee,))
                remaining -= count
                level[1] -= count
                fills.append({"order_id": order["order_id"], "price_cents": price, "quantity": count, "fee_cents": fee})
            if remaining != order["remaining"]:
                conn.execute("UPDATE paper_orders SET remaining=?, status=? WHERE order_id=?",
                             (remaining, "partial" if remaining else "filled", order["order_id"]))
        conn.execute("UPDATE paper_markets SET quote_json=? WHERE ticker=?", (json.dumps(quote), ticker))
        return {"status": "observed", "fills": fills}

    def _balances(self, conn: sqlite3.Connection) -> dict:
        cash = conn.execute("SELECT cash_cents FROM paper_account WHERE id=1").fetchone()[0]
        resting = conn.execute("SELECT limit_cents, remaining FROM paper_orders WHERE remaining>0").fetchall()
        reserved = sum(row["remaining"] * row["limit_cents"] + self._fee_reserve_cents(row["limit_cents"], row["remaining"])
                       for row in resting)
        cost = conn.execute("""SELECT COALESCE(SUM(f.price_cents*f.quantity+f.fee_cents),0)
            FROM paper_fills f JOIN paper_orders o USING(order_id)
            JOIN paper_markets m USING(ticker) WHERE m.result IS NULL""").fetchone()[0]
        return {"cash_cents": cash, "reserved_cents": reserved,
                "available_cash_cents": cash - reserved, "open_cost_cents": cost}

    def _daily_loss(self, conn: sqlite3.Connection, now: datetime) -> bool:
        pnl = conn.execute("SELECT COALESCE(SUM(pnl_cents),0) FROM paper_settlements WHERE substr(settled_at,1,10)=?",
                           (now.date().isoformat(),)).fetchone()[0]
        return pnl <= -self.config.max_daily_loss_cents

    def _block_reason(self, conn: sqlite3.Connection, market: sqlite3.Row, now: datetime,
                      quantity: int, price: int) -> str | None:
        if conn.execute("SELECT halted FROM paper_account WHERE id=1").fetchone()[0]:
            return "halted"
        if self._daily_loss(conn, now):
            self._halt(conn, {}, now)
            return "daily_loss_limit"
        quote = json.loads(market["quote_json"])
        if market["market_type"] not in self.config.allowed_market_types:
            return "market_type_disabled"
        if not quote["available"]:
            return "market_unavailable"
        if market["result"] or now >= utc(market["close_at"]):
            return "market_closed"
        if (now - utc(market["quote_at"])).total_seconds() > self.config.max_quote_age_seconds:
            return "stale_quote"
        if quantity > self.config.max_contracts_per_order:
            return "per_order_limit"
        balances = self._balances(conn)
        reserve = quantity * price + self._fee_reserve_cents(price, quantity)
        if reserve > balances["available_cash_cents"]:
            return "insufficient_cash"
        if reserve + balances["reserved_cents"] + balances["open_cost_cents"] > self.config.max_exposure_cents:
            return "exposure_limit"
        positions = conn.execute("""SELECT DISTINCT o.ticker, m.event_key FROM paper_orders o
            JOIN paper_markets m USING(ticker) WHERE m.result IS NULL AND
            (o.remaining>0 OR EXISTS(SELECT 1 FROM paper_fills f WHERE f.order_id=o.order_id))""").fetchall()
        if market["ticker"] not in {p["ticker"] for p in positions}:
            if len(positions) >= self.config.max_positions:
                return "position_limit"
            if sum(p["event_key"] == market["event_key"] for p in positions) >= self.config.max_event_positions:
                return "event_position_limit"
        return None

    def _order(self, conn: sqlite3.Connection, event: dict, now: datetime) -> dict:
        market = self._market(conn, event["ticker"])
        if event.get("side") not in {"yes", "no"}:
            raise ValueError("Invalid side")
        quantity = integer(event["quantity"], "quantity", 1)
        price = integer(event["limit_cents"], "limit_cents", 1, 99)
        reason = self._block_reason(conn, market, now, quantity, price)
        if reason:
            return {"status": "rejected", "reason": reason}
        # Prediction linkage is internal to forecasts, not a user-provided ID.
        conn.execute("""INSERT INTO paper_orders(order_id, ticker, side, limit_cents, quantity,
            remaining, status, created_at) VALUES(?, ?, ?, ?, ?, ?, 'resting', ?)""",
                     (event["event_id"], event["ticker"], event["side"], price, quantity, quantity, now.isoformat()))
        return {"status": "resting", "order_id": event["event_id"], "quantity": quantity}

    def _forecast(self, conn: sqlite3.Connection, event: dict, now: datetime) -> dict:
        market = self._market(conn, event["ticker"])
        if market["result"] or now >= utc(market["close_at"]):
            raise ValueError("Forecast must precede market close and settlement")
        p = event["yes_probability"]
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("yes_probability must be finite and in [0, 1]")
        model = label(event.get("model_name"), "model_name")
        version = label(event.get("model_version"), "model_version")
        quote = json.loads(market["quote_json"])
        yes, no = quote["yes_asks"], quote["no_asks"]
        baseline = (yes[0][0] + 100 - no[0][0]) / 200 if yes and no else None
        reason = self._block_reason(conn, market, now, 1, 1)
        if reason in {"market_type_disabled", "market_unavailable", "stale_quote"}:
            baseline = None
        result = {"status": "skipped", "reason": reason or "missing_two_sided_quote"}
        if self.config.research_only:
            # Record the forecast and the market's price; never size a position.
            result = {"status": "skipped", "reason": "research_only"}
        elif not reason and baseline is not None:
            options = []
            for side, prob, asks in (("yes", p, yes), ("no", 1-p, no)):
                limit = asks[0][0] + self.config.slippage_cents
                cost = limit + self._fee_cents(limit, 1)
                edge = prob - cost / 100
                if limit <= 99 and cost < 100 and edge >= self.config.min_edge:
                    options.append((edge, side, limit, cost))
            if options:
                edge, side, limit, cost = max(options)
                balances = self._balances(conn)
                kelly = edge / (1 - cost / 100) * self.config.kelly_fraction
                budget = min(int(balances["available_cash_cents"] * kelly),
                             balances["available_cash_cents"],
                             self.config.max_exposure_cents - balances["reserved_cents"] - balances["open_cost_cents"])
                quantity = min(self.config.max_contracts_per_order, budget // cost)
                if quantity > 0:
                    result = self._order(conn, {**event, "side": side, "limit_cents": limit, "quantity": quantity}, now)
                else:
                    result = {"status": "skipped", "reason": "budget_below_one_contract"}
            else:
                result = {"status": "skipped", "reason": "insufficient_net_edge"}
        conn.execute("""INSERT INTO paper_predictions VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                     (event["event_id"], event["ticker"], model, version, p, baseline, now.isoformat(),
                      result.get("reason", result["status"])))
        if result["status"] == "resting":
            conn.execute("UPDATE paper_orders SET prediction_id=? WHERE order_id=?", (event["event_id"], event["event_id"]))
        return result

    def _weather_forecast(self, conn: sqlite3.Connection, event: dict, now: datetime) -> dict:
        """Generate a baseline forecast from a complete, saved weather snapshot."""
        from src.paper.weather import predict_hourly_high

        market = self._market(conn, event["ticker"])
        quote = json.loads(market["quote_json"])
        if market["market_type"] != "temperature" or not quote.get("weather_spec"):
            raise ValueError("Weather baseline requires a reviewed temperature weather_spec on the quote")
        prediction = predict_hourly_high(event["snapshot"], quote["weather_spec"], now,
                                         self.config.weather_sigma_f, self.config.max_forecast_age_seconds)
        result = self._forecast(conn, {**event, **prediction}, now)
        return {**result, "prediction": prediction}

    def _cancel(self, conn: sqlite3.Connection, event: dict, now: datetime) -> dict:
        row = conn.execute("SELECT * FROM paper_orders WHERE order_id=?", (event["order_id"],)).fetchone()
        if not row:
            raise ValueError("Unknown paper order")
        if row["remaining"]:
            conn.execute("UPDATE paper_orders SET remaining=0, status='canceled' WHERE order_id=?", (event["order_id"],))
        return {"status": "canceled" if row["remaining"] else row["status"], "order_id": row["order_id"]}

    def _settle(self, conn: sqlite3.Connection, event: dict, now: datetime) -> dict:
        market = self._market(conn, event["ticker"])
        if event.get("result") not in {"yes", "no"}:
            raise ValueError("Settlement result must be yes or no")
        if now < utc(market["close_at"]):
            raise ValueError("Cannot settle before market close")
        if market["result"]:
            if market["result"] != event["result"]:
                raise ValueError("Conflicting settlement")
            return dict(conn.execute("SELECT * FROM paper_settlements WHERE ticker=?", (event["ticker"],)).fetchone())
        fills = conn.execute("""SELECT f.*, o.side FROM paper_fills f
            JOIN paper_orders o USING(order_id) WHERE o.ticker=?""", (event["ticker"],)).fetchall()
        cost = sum(f["price_cents"]*f["quantity"] + f["fee_cents"] for f in fills)
        payout = sum(100*f["quantity"] for f in fills if f["side"] == event["result"])
        conn.execute("UPDATE paper_account SET cash_cents=cash_cents+? WHERE id=1", (payout,))
        conn.execute("UPDATE paper_markets SET result=? WHERE ticker=?", (event["result"], event["ticker"]))
        conn.execute("UPDATE paper_orders SET remaining=0, status='canceled' WHERE ticker=? AND remaining>0", (event["ticker"],))
        conn.execute("INSERT INTO paper_settlements VALUES(?, ?, ?, ?, ?)",
                     (event["ticker"], payout, cost, payout-cost, now.isoformat()))
        if self._daily_loss(conn, now):
            self._halt(conn, {}, now)
        return {"ticker": event["ticker"], "payout_cents": payout, "cost_cents": cost,
                "pnl_cents": payout-cost, "settled_at": now.isoformat()}

    def _halt(self, conn: sqlite3.Connection, event: dict, now: datetime) -> dict:
        conn.execute("UPDATE paper_account SET halted=1 WHERE id=1")
        conn.execute("UPDATE paper_orders SET remaining=0, status='canceled' WHERE remaining>0")
        return {"status": "halted"}

    def _unavailable(self, conn: sqlite3.Connection, event: dict, now: datetime) -> dict:
        """Suspend a market and release reservations when eligibility expires."""
        market = self._market(conn, event["ticker"])
        quote = json.loads(market["quote_json"])
        quote["available"] = False
        conn.execute("UPDATE paper_markets SET quote_json=? WHERE ticker=?", (json.dumps(quote), event["ticker"]))
        conn.execute("UPDATE paper_orders SET remaining=0, status='canceled' WHERE ticker=? AND remaining>0", (event["ticker"],))
        return {"status": "unavailable", "ticker": event["ticker"]}

    def market_state(self, ticker: str) -> dict | None:
        """Read existing market state for observation/settlement recovery."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM paper_markets WHERE ticker=?", (ticker,)).fetchone()
            return dict(row) if row else None

    def has_prediction(self, ticker: str, model_name: str, model_version: str) -> bool:
        """Check for an eligible baseline forecast; stale attempts may retry."""
        with self._connect() as conn:
            return conn.execute("""SELECT 1 FROM paper_predictions WHERE ticker=?
                AND model_name=? AND model_version=? AND market_yes_probability IS NOT NULL LIMIT 1""", (ticker, model_name, model_version)).fetchone() is not None

    def report(self) -> dict:
        """Return accounting and paired predictive scores without claiming an edge."""
        with self._connect() as conn:
            conn.execute("BEGIN")
            balances = self._balances(conn)
            account = dict(conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone())
            orders = [dict(row) for row in conn.execute("SELECT * FROM paper_orders ORDER BY created_at, rowid")]
            fills = [dict(row) for row in conn.execute("SELECT * FROM paper_fills ORDER BY id")]
            settlements = [dict(row) for row in conn.execute("SELECT * FROM paper_settlements ORDER BY settled_at")]
            # One earliest forecast per model/version/market, including skipped
            # decisions, so order frequency and fills do not weight Brier scores.
            predictions = [dict(row) for row in conn.execute("""SELECT p.*, m.result, m.event_key FROM paper_predictions p
                JOIN paper_markets m USING(ticker) ORDER BY p.created_at, p.rowid""")]
            # Valuation uses the experiment clock (last processed event), never
            # wall-clock time, so a reopened broker reports identically.
            # Imported here: marks imports utc from this module, so a top-level
            # import would be circular. cli.py uses the same pattern for observe.
            from src.paper.marks import equity_at_market
            valued_at = utc(account["last_event_at"]) if account["last_event_at"] else None
            marks = (equity_at_market(conn, valued_at, self.config.max_quote_age_seconds)
                     if valued_at else {"equity_at_market_cents": None, "market_value_cents": None,
                                        "unvaluable_count": 0, "unvaluable_positions": []})
        groups = {}
        seen = set()
        for p in predictions:
            key = (p["model_name"], p["model_version"], p["ticker"])
            if key in seen or p["market_yes_probability"] is None:
                continue
            seen.add(key)
            if p["result"] is None:
                continue
            group = groups.setdefault((p["model_name"], p["model_version"]), [])
            outcome = int(p["result"] == "yes")
            group.append({"event_key": p["event_key"],
                          "model_brier": (p["yes_probability"]-outcome)**2,
                          "market_brier": (p["market_yes_probability"]-outcome)**2})
        scores = []
        for (name, version), pairs in groups.items():
            # Per-market figures are kept unchanged for continuity. They treat every
            # bracket as an independent observation, which overstates precision when
            # one event is offered as many brackets; the clustered block beside them
            # is the honest reading.
            model = sum(p["model_brier"] for p in pairs)/len(pairs)
            market = sum(p["market_brier"] for p in pairs)/len(pairs)
            scores.append({"model_name": name, "model_version": version, "n_markets": len(pairs),
                           "model_brier": model, "market_brier": market, "brier_improvement": market-model,
                           "event_clustered": _clustered_scores(pairs)})
        realized = sum(s["pnl_cents"] for s in settlements)
        return {"mode": "paper", "run_kind": self.config.run_kind, "config": self.config.to_dict(),
                **balances, "realized_pnl_cents": realized,
                "equity_at_cost_cents": balances["cash_cents"] + balances["open_cost_cents"],
                "fees_paid_cents": sum(f["fee_cents"] for f in fills),
                "halted": bool(account["halted"]), "last_event_at": account["last_event_at"],
                "orders": orders, "fills": fills, "settlements": settlements,
                "equity_at_market_cents": marks["equity_at_market_cents"],
                "market_value_cents": marks["market_value_cents"],
                "unvaluable_position_count": marks["unvaluable_count"],
                "unvaluable_positions": marks["unvaluable_positions"],
                "prediction_count": len(predictions), "scores": scores,
                "evidence_note": "Synthetic/replay results are not prospective evidence. Forward mode checks arrival time, not model provenance. Brier scores use the first forecast per model/version/market. Top-level model_brier/market_brier/brier_improvement count every bracket as one observation and therefore overstate precision; read scores[].event_clustered, which weights each event once and reports a confidence interval from resampling whole events. equity_at_cost_cents is at cost; equity_at_market_cents values open positions at the complementary-side ask and excludes positions whose quote is stale or missing, counted in unvaluable_position_count."}
