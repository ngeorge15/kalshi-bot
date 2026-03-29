# Phase 1: Kalshi API Client & Infrastructure - Research

**Researched:** 2026-03-28
**Domain:** Kalshi REST API v2, RSA-PSS authentication, Python project infrastructure, SQLite schema design
**Confidence:** HIGH (core API surface verified against official docs; pitfalls verified against changelog)

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Build the client with raw `requests` + custom RSA-PSS auth. Do NOT use the `kalshi-python` SDK — it has stale endpoints and awkward auth patching. Implement all HTTP calls directly against the REST API.
- **D-02:** Client is synchronous (`requests` library). No async/await. The bot runs on a schedule, not in real-time — sequential calls are fast enough. Simpler code throughout all phases.
- **D-03:** Load the private key once at `KalshiAuth` initialization time and cache in memory. No per-request file I/O.
- **D-04:** Timestamp in the auth signature is Unix milliseconds as an integer string — matches Kalshi's documented format.
- **D-05:** Module-level singleton pattern. `config.py` loads `.env` + `trading_config.json` at import time and exposes a `config` object. Downstream code accesses via `from src.config import config`. No dependency injection threading through constructors.
- **D-06:** Define the **full schema** in Phase 1 — all tables for all 7 phases (`trades`, `predictions`, `outcomes`, `daily_pnl`, `model_versions`, `improvements`, `evaluator_runs`, `holdout_results`, `schema_version`). Avoids mid-project migrations. `database.py` includes schema versioning so future changes are additive.
- **D-07:** Integration tests (demo API calls — auth, market discovery, orderbook pull, place/cancel order) are opt-in. Run only when `KALSHI_INTEGRATION=true` is set. Default `pytest tests/` uses mocks — fast, no network required. Mark with `@pytest.mark.integration`.

### Claude's Discretion
- HTTP session management (connection pooling, timeouts) — standard `requests.Session` config
- Retry logic implementation (3 retries, exponential backoff per CONVENTIONS.md)
- Rate limiting approach (simple sleep-based per CONVENTIONS.md: respect tier limits)
- Private key file format handling (PEM loading via `cryptography` library)

### Deferred Ideas (OUT OF SCOPE)
None — discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| R1.1 | RSA-PSS authentication wrapper (timestamp + method + path signing) | Signing algorithm verified against official docs; exact header names confirmed |
| R1.2 | Configurable base URL: demo vs production | Both URLs confirmed with exact values |
| R1.3 | Market discovery — list markets filtered by category, status, series ticker | GET /markets query params verified; series discovery pattern confirmed |
| R1.4 | Event discovery — GetEvent endpoint for mutual exclusivity and series structure | GET /events/{ticker} confirmed; response structure documented |
| R1.5 | Orderbook retrieval — yes/no bid depths for a given market ticker | GET /markets/{ticker}/orderbook confirmed; response uses _dollars fixed-point format |
| R1.6 | Order placement — submit limit orders with side, price, quantity | POST /orders schema fully documented |
| R1.7 | Order management — cancel open orders, check order status, list fills | DELETE /portfolio/orders/{id}, GET /portfolio/orders, GET /portfolio/fills all confirmed |
| R1.8 | Portfolio state — current positions, balance, settlement history | GET /portfolio/balance, /portfolio/positions, /portfolio/settlements all confirmed |
| R1.9 | Rate limiting — exponential backoff | urllib3 Retry + HTTPAdapter pattern confirmed |
| R1.10 | Credentials from environment variables or .env file | python-dotenv pattern confirmed |
</phase_requirements>

---

## Summary

The Kalshi v2 REST API uses RSA-PSS request signing with three custom headers. The API is fully documented at docs.kalshi.com, and all endpoints needed for Phase 1 are confirmed. **Critical breaking change:** as of March 12, 2026, Kalshi removed all legacy integer count and integer cents price fields from REST responses. Code must use `_dollars` (fixed-point string) and `_fp` (fixed-point string) field variants — never the old integer `yes_price`/`no_price` on fills or `count` on orders.

The Kalshi market hierarchy is **Series → Events → Markets**. Discovery by sport uses the `series_ticker` filter parameter on `GET /markets`. Key confirmed series tickers: `KXNBAGAME` (NBA game moneylines), `KXNBA` (NBA championship futures), `KXNBAWEST`/`KXNBAEAST` (conference futures), `KXNBAPLAYOFF` (playoff qualifiers), `KXHIGHNY`/`KXHIGHCHI`/`KXHIGHMIA`/`KXHIGHLAX` (temperature). NBA props series ticker is not confirmed from official sources — the `GET /search/tags_by_categories` endpoint must be queried at runtime to discover current series tickers for props.

The Python `cryptography` 43.0.0 is already installed. `python-dotenv` 0.21.0 is installed (latest is 1.2.2 — worth pinning to 1.2.2 in requirements.txt). `requests` 2.32.3 is installed (latest 2.33.0). All core dependencies are available.

**Primary recommendation:** Use `requests.Session` with urllib3 `Retry` adapter for resilient HTTP; implement `KalshiAuth` as an `AuthBase` subclass so it composes cleanly into the session; use `_dollars` fixed-point fields exclusively in all response parsing.

---

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `requests` | 2.32.3 (installed) / pin 2.32.3+ | HTTP client for all Kalshi API calls | Locked by D-02; synchronous, mature, battle-tested |
| `cryptography` | 43.0.0 (installed) | RSA-PSS signing via `hazmat.primitives` | Official Kalshi docs reference this library; already installed |
| `python-dotenv` | 0.21.0 (installed) / pin 1.0.1+ | Load `.env` file into environment | Required by R1.10 and CONVENTIONS.md |
| `pytest` | 7.4.4 (installed) | Test framework | CONVENTIONS.md mandates pytest |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `pytest-mock` | 3.15.1 (not installed) | Cleaner mock syntax in pytest | Recommended for `mocker` fixture; optional, `unittest.mock` works too |
| `responses` | 0.26.0 (not installed) | HTTP request mocking at transport level | Better than patching `requests.get` directly — intercepts at socket level |
| `urllib3` | bundled with requests | `Retry` + `HTTPAdapter` for retry logic | Always available with requests |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `cryptography` | `pycryptodome` | `cryptography` is Kalshi-recommended and already installed |
| `responses` mock | `unittest.mock.patch` | `responses` is more realistic; `patch` is simpler, needs no install |
| `requests.Session` | plain `requests.get` | Session is strictly better: pooling, shared auth headers, retry mount |

**Installation (additional deps only):**
```bash
pip install pytest-mock==3.15.1 responses==0.26.0
```

**Full requirements.txt pins:**
```
requests==2.32.3
cryptography==43.0.0
python-dotenv==1.0.1
pytest==7.4.4
pytest-mock==3.15.1
responses==0.26.0
```

**Version verification:** Versions confirmed against PyPI registry on 2026-03-28.

---

## Architecture Patterns

### Recommended Project Structure

Exact structure from CONVENTIONS.md — do not deviate:

```
kalshi-bot/
├── src/
│   ├── __init__.py
│   ├── kalshi/
│   │   ├── __init__.py
│   │   ├── client.py       # KalshiClient class
│   │   └── auth.py         # KalshiAuth(AuthBase) class
│   ├── db/
│   │   ├── __init__.py
│   │   ├── schema.sql
│   │   └── database.py
│   └── config.py
├── tests/
│   ├── __init__.py
│   └── test_kalshi_client.py
├── data/                   # gitignored
│   └── kalshi_bot.db
├── .env                    # gitignored
├── .env.example
├── .gitignore
├── requirements.txt
└── trading_config.json
```

### Pattern 1: RSA-PSS Auth as `requests.AuthBase`

**What:** Subclass `requests.auth.AuthBase` so auth hooks into the `Session.send()` pipeline cleanly. The `__call__` method receives the `PreparedRequest`, adds the three required headers, and returns it.

**When to use:** Mount once on Session; every request automatically signed. Eliminates the need to pass credentials to every method call.

```python
# Source: Kalshi official docs + cryptography library docs
import base64
import time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from requests.auth import AuthBase

class KalshiAuth(AuthBase):
    def __init__(self, key_id: str, private_key_path: str) -> None:
        self.key_id = key_id
        with open(private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def __call__(self, r):
        timestamp_ms = str(int(time.time() * 1000))
        # Sign: timestamp_ms + HTTP_METHOD + path (no query string, no body)
        path = r.path_url.split("?")[0]  # strip query params from signing string
        msg = f"{timestamp_ms}{r.method}{path}".encode("utf-8")
        sig = self.private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        r.headers["KALSHI-ACCESS-KEY"] = self.key_id
        r.headers["KALSHI-ACCESS-SIGNATURE"] = base64.b64encode(sig).decode("utf-8")
        r.headers["KALSHI-ACCESS-TIMESTAMP"] = timestamp_ms
        return r
```

**CRITICAL:** Sign only the path, not the query string. Confirmed by official docs: sign `timestamp + method + path`.

### Pattern 2: Session with Retry Adapter

**What:** Mount a urllib3 `Retry` strategy on the session at init time. All requests automatically retry on transient failures.

**When to use:** Always. Per CONVENTIONS.md: 3 retries, exponential backoff (1s, 2s, 4s). This maps to `backoff_factor=1` with `total=3`.

```python
# Source: urllib3 official docs + requests docs
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def _build_session(auth: KalshiAuth) -> requests.Session:
    session = requests.Session()
    session.auth = auth
    retry = Retry(
        total=3,
        backoff_factor=1,          # sleep: 1s, 2s, 4s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "DELETE"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update({"Content-Type": "application/json"})
    return session
```

**Note on 429:** urllib3's `Retry` respects the `Retry-After` header by default when 429 is in `status_forcelist`. This gives free rate-limit compliance.

### Pattern 3: Config Singleton

**What:** `config.py` imports at module level, reads `.env` + `trading_config.json`, and exposes a `config` object. Downstream: `from src.config import config`.

**When to use:** Everywhere. Locked by D-05.

```python
# src/config.py
from dotenv import load_dotenv
import json, os

load_dotenv()  # loads .env into os.environ

class Config:
    DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
    PROD_BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"

    def __init__(self):
        self.api_key_id = os.environ["KALSHI_API_KEY_ID"]
        self.private_key_path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
        self.env = os.getenv("KALSHI_ENV", "demo")
        self.base_url = self.DEMO_BASE_URL if self.env == "demo" else self.PROD_BASE_URL
        with open("trading_config.json") as f:
            self._trading = json.load(f)

config = Config()
```

### Pattern 4: Market Discovery via Series

**What:** Use `GET /markets?series_ticker=KXNBAGAME&status=open` to discover all open markets in a series. Then `GET /events/{event_ticker}` to understand the bracket structure.

**When to use:** Market scan loop in every run cycle.

```python
# Source: Kalshi official docs
def get_markets_by_series(self, series_ticker: str, status: str = "open") -> list[dict]:
    params = {"series_ticker": series_ticker, "status": status, "limit": 200}
    markets = []
    while True:
        resp = self._get("/markets", params=params)
        markets.extend(resp["markets"])
        cursor = resp.get("cursor", "")
        if not cursor:
            break
        params["cursor"] = cursor
    return markets
```

**Always paginate** — the API returns at most 1000 per page (default 100).

### Pattern 5: Fixed-Point Response Parsing (POST March 2026)

**What:** All price and count fields in API responses now use `_dollars` (string fixed-point) and `_fp` (string fixed-point). The old integer `yes_price`, `no_price`, `count` fields on fills/settlements were removed March 12, 2026.

**When to use:** Every response parser. Convert `_dollars` strings to Decimal or float for arithmetic; store as integer cents (× 100) per CONVENTIONS.md.

```python
# Convert API dollar string to integer cents (project convention)
def dollars_to_cents(dollars_str: str) -> int:
    """Convert '0.6500' to 65."""
    from decimal import Decimal
    return int(Decimal(dollars_str) * 100)

# Orderbook price level: [price_str, qty_fp_str]
# e.g., ["0.6500", "100.00"]
yes_bids = [(dollars_to_cents(p), float(q)) for p, q in orderbook["orderbook_fp"]["yes_dollars"]]
```

### Anti-Patterns to Avoid

- **Signing the full URL including query string:** Only sign the path component. Confirmed by official docs.
- **Using integer `yes_price`/`no_price` on fills:** Removed March 12, 2026. Use `yes_price_dollars`.
- **Per-request key file I/O:** Locked by D-03. Load key once at `KalshiAuth.__init__`.
- **Importing from SDK `kalshi-python`:** Locked by D-01. Stale, wrong endpoints.
- **Module-level `requests.get()` calls:** Use `Session` — enables retry, auth, and pooling.
- **Hardcoded base URLs outside config.py:** All URL construction flows through `config.base_url`.
- **Forgetting to strip query string before signing:** `r.path_url` includes `?foo=bar`. Strip at `?`.

---

## Kalshi API Reference

### Base URLs (CONFIRMED)

| Environment | Base URL |
|-------------|----------|
| Demo | `https://demo-api.kalshi.co/trade-api/v2` |
| Production | `https://trading-api.kalshi.com/trade-api/v2` |

Note: R1.2 in REQUIREMENTS.md references `https://api.elections.kalshi.com/trade-api/v2` for production — this appears to be a legacy hostname. The current production hostname is `https://trading-api.kalshi.com/trade-api/v2` per official docs. Verify at runtime against `GET /exchange/status`.

### Authentication Headers (CONFIRMED)

| Header | Value |
|--------|-------|
| `KALSHI-ACCESS-KEY` | API Key ID (UUID from account settings) |
| `KALSHI-ACCESS-SIGNATURE` | base64(RSA-PSS sign(`timestamp_ms + METHOD + path`)) |
| `KALSHI-ACCESS-TIMESTAMP` | Unix timestamp in milliseconds (integer string) |

### Endpoint Map (CONFIRMED)

| Operation | Method | Path | Auth Required |
|-----------|--------|------|---------------|
| List markets | GET | `/markets` | No |
| Get single market | GET | `/markets/{ticker}` | No |
| Get orderbook | GET | `/markets/{ticker}/orderbook` | No |
| Get events | GET | `/events` | No |
| Get single event | GET | `/events/{event_ticker}` | No |
| Get series | GET | `/series/{series_ticker}` | No |
| List series | GET | `/series` | No |
| Create order | POST | `/portfolio/orders` | Yes |
| List orders | GET | `/portfolio/orders` | Yes |
| Get order | GET | `/portfolio/orders/{order_id}` | Yes |
| Cancel order | DELETE | `/portfolio/orders/{order_id}` | Yes |
| Get fills | GET | `/portfolio/fills` | Yes |
| Get balance | GET | `/portfolio/balance` | Yes |
| Get positions | GET | `/portfolio/positions` | Yes |
| Get settlements | GET | `/portfolio/settlements` | Yes |
| Exchange status | GET | `/exchange/status` | No |

### Known Series Tickers (CONFIRMED from public Kalshi URLs)

| Series Ticker | Category | Description |
|---------------|----------|-------------|
| `KXNBAGAME` | NBA | Single-game moneyline markets (e.g., `KXNBAGAME-26feb11wascle`) |
| `KXNBA` | NBA Futures | NBA Championship winner |
| `KXNBAWEST` | NBA Futures | Western Conference champion |
| `KXNBAEAST` | NBA Futures | Eastern Conference champion |
| `KXNBAPLAYOFF` | NBA Futures | Playoff qualifier markets |
| `KXHIGHNY` | Weather | Daily high temp, NYC (Central Park) |
| `KXHIGHCHI` | Weather | Daily high temp, Chicago (Midway) |
| `KXHIGHMIA` | Weather | Daily high temp, Miami |
| `KXHIGHLAX` | Weather | Daily high temp, Los Angeles |

**NBA props series ticker:** NOT confirmed from official sources. Use `GET /search/tags_by_categories` at runtime to discover current sports series. Based on naming conventions, likely `KXNBAPROP` — but verify before hardcoding.

**Temperature market structure:** Each day generates one event with multiple bracket markets (6 brackets per city). Use `GET /events/{event_ticker}` to get all bracket markets within an event.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| HTTP retry logic | Custom retry loop | `urllib3.Retry` + `HTTPAdapter` | Handles backoff math, status codes, method filtering, Retry-After header |
| RSA-PSS signing | Manual crypto | `cryptography.hazmat.primitives.asymmetric.padding.PSS` | Correct salt length, padding implementation edge cases |
| `.env` loading | Manual file parse | `python-dotenv.load_dotenv()` | Handles quotes, exports, multiline values, encoding |
| Pagination | Recursive fetcher | Cursor loop pattern (see code example) | Simple loop; building a generic paginator over-engineers it |
| JSON schema for trading_config | Custom validator | Plain `json.load()` + type assertions | Config is small; full validator is overkill for Phase 1 |
| Mock HTTP in tests | Patch `requests.get` | `responses` library or `unittest.mock.patch` | `responses` intercepts at transport level, more accurate |

**Key insight:** The `cryptography` library's PSS implementation is already correct. Deviating to manual PKCS#1 or using a different salt length will break Kalshi's signature verification — RSA-PSS with `PSS.MAX_LENGTH` is the documented requirement.

---

## Common Pitfalls

### Pitfall 1: Signing the Full URL (Includes Query String)

**What goes wrong:** Auth signature verification fails with a 401. Kalshi returns a cryptic "invalid signature" error.

**Why it happens:** `PreparedRequest.path_url` in requests includes the query string (e.g., `/markets?status=open`). The signing string must be only the path: `/markets`.

**How to avoid:**
```python
path = r.path_url.split("?")[0]  # strip query params BEFORE signing
msg = f"{timestamp_ms}{r.method}{path}".encode("utf-8")
```

**Warning signs:** All authenticated calls fail with 401 or 403 even though credentials are correct.

### Pitfall 2: Using Removed Legacy Integer Fields

**What goes wrong:** `KeyError` or `None` when accessing `fill["yes_price"]` or `fill["no_price"]`.

**Why it happens:** These fields were **removed from the API on March 12, 2026**. The API now returns only `yes_price_dollars` (string, e.g., `"0.6500"`) and `count_fp` (string, e.g., `"10.00"`).

**How to avoid:** Always use `_dollars` and `_fp` field suffixes. Convert to internal cents representation immediately:
```python
price_cents = int(Decimal(fill["yes_price_dollars"]) * 100)
count = Decimal(fill["count_fp"])
```

**Warning signs:** Any code that accesses integer `yes_price`, `no_price`, or `count` fields on fills, settlements, or market position responses.

### Pitfall 3: Timestamp Precision (Milliseconds vs Seconds)

**What goes wrong:** Signature fails or is rejected with "timestamp out of range" error.

**Why it happens:** Kalshi requires Unix milliseconds as an integer string. Using `str(int(time.time()))` (seconds) produces a 10-digit number; correct is 13-digit milliseconds.

**How to avoid:** `str(int(time.time() * 1000))`. Confirmed by D-04 and official docs.

**Warning signs:** Consistent 401 errors with correct key ID; timestamp-related error messages.

### Pitfall 4: Production Base URL Confusion

**What goes wrong:** Using `https://api.elections.kalshi.com/...` which is referenced in older docs and the REQUIREMENTS.md file. May work but is not the current canonical production hostname.

**Why it happens:** Kalshi renamed/redirected their production base URL. REQUIREMENTS.md R1.2 references the old hostname.

**How to avoid:** Use `https://trading-api.kalshi.com/trade-api/v2` for production. For demo: `https://demo-api.kalshi.co/trade-api/v2`. Verify with `GET /exchange/status` to confirm connectivity.

**Warning signs:** Unexpected redirects or connection errors on production endpoint.

### Pitfall 5: Not Paginating Market Discovery

**What goes wrong:** Only seeing 100 markets when there are hundreds open. Missing market opportunities.

**Why it happens:** Default page size is 100, max 1000. The response includes a `cursor` field that must be passed in the next request.

**How to avoid:** Always paginate with a cursor loop (see Pattern 4 code). Check `cursor` in response; continue until empty string returned.

**Warning signs:** Market count seems suspiciously round (exactly 100, 200, etc.).

### Pitfall 6: SQLite `AUTOINCREMENT` Overuse

**What goes wrong:** Slower inserts, fragmentation over time.

**Why it happens:** SQLite's `INTEGER PRIMARY KEY` auto-increments by default. `AUTOINCREMENT` keyword adds extra overhead and is only needed to prevent ID reuse after deletion — which is irrelevant for append-only trade logs.

**How to avoid:** Use `INTEGER PRIMARY KEY` without the `AUTOINCREMENT` keyword for `trades`, `predictions`, and log tables.

### Pitfall 7: `python-dotenv` Version (0.21.0 installed vs 1.x expected)

**What goes wrong:** Some `load_dotenv()` behaviors differ between 0.x and 1.x, especially around overriding existing env vars.

**Why it happens:** Installed version is 0.21.0; current stable is 1.2.2. The API is mostly compatible, but `override=True` default behavior changed.

**How to avoid:** Pin `python-dotenv>=1.0.1` in requirements.txt. Call `load_dotenv(override=False)` so environment variables already set (e.g., in CI) take precedence over `.env`.

---

## Code Examples

### Create Order (Limit, Maker)

```python
# Source: https://docs.kalshi.com/api-reference/orders/create-order
def place_limit_order(
    self,
    ticker: str,
    side: str,        # "yes" or "no"
    action: str,      # "buy" or "sell"
    price_cents: int, # e.g., 65 for $0.65
    count: int,       # contracts
    post_only: bool = True,  # maker bias per PROJECT.md
) -> dict:
    payload = {
        "ticker": ticker,
        "side": side,
        "action": action,
        "type": "limit",
        "yes_price": price_cents if side == "yes" else (100 - price_cents),
        "count": count,
        "post_only": post_only,
        "time_in_force": "good_till_canceled",
    }
    return self._post("/portfolio/orders", json=payload)["order"]
```

### Cancel Order

```python
# Source: https://docs.kalshi.com/api-reference/orders/cancel-order
def cancel_order(self, order_id: str) -> dict:
    return self._delete(f"/portfolio/orders/{order_id}")
```

### Get Orderbook

```python
# Source: https://docs.kalshi.com/api-reference/market/get-market-orderbook
# Response uses orderbook_fp with _dollars price levels (post March 2026)
def get_orderbook(self, ticker: str) -> dict:
    resp = self._get(f"/markets/{ticker}/orderbook")
    ob = resp["orderbook_fp"]
    return {
        "yes_bids": [(dollars_to_cents(p), Decimal(q)) for p, q in ob.get("yes_dollars", [])],
        "no_bids": [(dollars_to_cents(p), Decimal(q)) for p, q in ob.get("no_dollars", [])],
    }
```

### Get Balance

```python
# Source: https://docs.kalshi.com/api-reference/portfolio/get-balance
def get_balance(self) -> dict:
    resp = self._get("/portfolio/balance")
    return {
        "balance_cents": resp["balance"],           # integer cents
        "portfolio_value_cents": resp["portfolio_value"],
        "updated_ts": resp["updated_ts"],
    }
```

---

## SQLite Schema Design

Per D-06, define the full schema for all 7 phases upfront. Key design decisions:

**Timestamps:** ISO 8601 UTC strings (`TEXT NOT NULL`)
**Money:** Integer cents (`INTEGER NOT NULL`) matching Kalshi convention
**Booleans:** Integer 0/1 (`INTEGER NOT NULL DEFAULT 0`)
**Foreign keys:** Enable with `PRAGMA foreign_keys = ON` at connection time

**Schema version table:**
```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
```

**Trades table (core):**
```sql
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY,
    order_id        TEXT UNIQUE NOT NULL,
    ticker          TEXT NOT NULL,
    market_type     TEXT NOT NULL,  -- 'nba_game', 'nba_prop', 'nba_future', 'weather_temp', 'weather_precip', 'weather_severe'
    side            TEXT NOT NULL,  -- 'yes' or 'no'
    action          TEXT NOT NULL,  -- 'buy' or 'sell'
    price_cents     INTEGER NOT NULL,
    quantity        REAL NOT NULL,  -- decimal contracts (fractional trading)
    status          TEXT NOT NULL,  -- 'resting', 'executed', 'canceled'
    model_prob      REAL,
    market_price_cents INTEGER,
    edge_cents      INTEGER,
    confidence      REAL,
    created_at      TEXT NOT NULL,
    filled_at       TEXT,
    notes           TEXT
);
```

All other tables (`predictions`, `outcomes`, `daily_pnl`, `model_versions`, `improvements`, `evaluator_runs`, `holdout_results`) should be defined with `IF NOT EXISTS` so the file is idempotent.

**Quantity type note:** Use `REAL` for quantity columns, not `INTEGER`, because fractional trading (`count_fp`) is enabled for some markets as of March 2026.

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Integer `yes_price` on fills | `yes_price_dollars` string | March 12, 2026 | Breaking: must parse string to Decimal |
| Integer `count` on orders | `count_fp` string | March 12, 2026 | Breaking: must parse string to Decimal |
| Integer `yes_total_cost` on settlements | `yes_total_cost_dollars` string | April 2, 2026 | Breaking (just happened) |
| `kalshi-python` SDK | Direct REST (this project) | N/A — SDK is stale | Correct choice per D-01 |

**Deprecated:**
- `yes_price_fixed` and `no_price_fixed` on fills: removed March 12, 2026
- Integer count fields: removed March 12, 2026
- Old production hostname `api.elections.kalshi.com`: may redirect; use `trading-api.kalshi.com`

---

## Open Questions

1. **NBA props series ticker**
   - What we know: NBA props exist on Kalshi; naming convention suggests `KXNBAPROP` or similar
   - What's unclear: Exact series ticker not confirmed from official sources
   - Recommendation: Call `GET /search/tags_by_categories` in the integration test to discover current sports series tickers. Do not hardcode until confirmed.

2. **Demo API key registration**
   - What we know: `.env` is already populated with demo credentials per CONTEXT.md
   - What's unclear: Whether demo API requires separate key registration at demo.kalshi.com vs kalshi.com
   - Recommendation: Test auth with existing credentials in integration test first; if 401, check that the key was registered at the demo environment URL.

3. **`yes_price` on order creation POST body**
   - What we know: Order creation schema still shows `yes_price` (integer 1-99) as a valid field
   - What's unclear: Whether POST body integer fields were also removed on March 12, 2026 (the removal appeared to be response fields only)
   - Recommendation: Use integer `yes_price` in order creation (POST body) until proven otherwise; parse responses with `_dollars` fields only.

4. **Fractional contract support on demo**
   - What we know: Fractional trading was rolling out per-market starting March 9, 2026
   - What's unclear: Whether demo API markets have `fractional_trading_enabled: true`
   - Recommendation: Store quantities as `REAL` in SQLite (already noted above); use `count_fp` field in order responses regardless.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.10+ | All code | ✓ | 3.12.7 | — |
| pip | Dependency install | ✓ | 24.2 | — |
| uv | Faster installs | ✗ | — | Use pip (available) |
| requests | HTTP client | ✓ | 2.32.3 | — |
| cryptography | RSA-PSS signing | ✓ | 43.0.0 | — |
| python-dotenv | .env loading | ✓ | 0.21.0 (outdated) | Upgrade to 1.0.1+ |
| pytest | Test runner | ✓ | 7.4.4 | — |
| pytest-mock | Test mocking | ✗ | — | unittest.mock (available) |
| responses | HTTP mocking | ✗ | — | unittest.mock.patch (available) |
| sqlite3 | Database | ✓ | 3.45.3 | — |
| git | Version control | ✓ | 2.50.1 | — |

**Missing dependencies with no fallback:** None — all blocking dependencies are available.

**Missing dependencies with fallback:**
- `pytest-mock`: Use `unittest.mock` directly (stdlib, no install needed)
- `responses`: Use `unittest.mock.patch("requests.Session.request")` or `unittest.mock.patch("requests.get")`
- `uv`: Use `pip` (already available)

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 7.4.4 |
| Config file | `pytest.ini` or `pyproject.toml [tool.pytest]` — create in Wave 0 |
| Quick run command | `pytest tests/test_kalshi_client.py -v -m "not integration"` |
| Full suite command | `pytest tests/ -v` |
| Integration run | `KALSHI_INTEGRATION=true pytest tests/ -v -m integration` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| R1.1 | RSA-PSS auth signs correct string, produces valid base64 sig | unit | `pytest tests/test_kalshi_client.py::test_auth_signature -x` | ❌ Wave 0 |
| R1.1 | Auth headers present on every request | unit | `pytest tests/test_kalshi_client.py::test_auth_headers -x` | ❌ Wave 0 |
| R1.2 | Config uses demo URL when KALSHI_ENV=demo | unit | `pytest tests/test_kalshi_client.py::test_base_url_demo -x` | ❌ Wave 0 |
| R1.2 | Config uses prod URL when KALSHI_ENV=production | unit | `pytest tests/test_kalshi_client.py::test_base_url_prod -x` | ❌ Wave 0 |
| R1.3 | get_markets_by_series paginates correctly | unit | `pytest tests/test_kalshi_client.py::test_market_discovery_pagination -x` | ❌ Wave 0 |
| R1.3 | Auth to demo + discover NBA game markets | integration | `KALSHI_INTEGRATION=true pytest tests/test_kalshi_client.py::test_integration_discover_nba_games -x` | ❌ Wave 0 |
| R1.4 | get_event returns event with markets | unit | `pytest tests/test_kalshi_client.py::test_get_event -x` | ❌ Wave 0 |
| R1.5 | get_orderbook parses _dollars format correctly | unit | `pytest tests/test_kalshi_client.py::test_orderbook_parse -x` | ❌ Wave 0 |
| R1.5 | Pull real orderbook from demo | integration | `KALSHI_INTEGRATION=true pytest tests/test_kalshi_client.py::test_integration_orderbook -x` | ❌ Wave 0 |
| R1.6 | place_limit_order sends correct payload | unit | `pytest tests/test_kalshi_client.py::test_place_order_payload -x` | ❌ Wave 0 |
| R1.6 | Place + cancel test limit order on demo | integration | `KALSHI_INTEGRATION=true pytest tests/test_kalshi_client.py::test_integration_place_cancel -x` | ❌ Wave 0 |
| R1.7 | cancel_order calls DELETE /portfolio/orders/{id} | unit | `pytest tests/test_kalshi_client.py::test_cancel_order -x` | ❌ Wave 0 |
| R1.8 | get_balance returns cents fields | unit | `pytest tests/test_kalshi_client.py::test_get_balance -x` | ❌ Wave 0 |
| R1.9 | Session mounts Retry adapter with correct config | unit | `pytest tests/test_kalshi_client.py::test_retry_config -x` | ❌ Wave 0 |
| R1.10 | KalshiClient fails loudly if env var missing | unit | `pytest tests/test_kalshi_client.py::test_missing_env_var -x` | ❌ Wave 0 |

### Mocking Strategy

**Unit tests (no network):**
- Use `responses` library (if installed) to intercept HTTP at transport level
- Fallback: `unittest.mock.patch` on `requests.Session.send`
- Mock the private key with a freshly generated test key (generate in `conftest.py`)
- Do NOT mock `KalshiAuth` — test the actual signing logic

```python
# conftest.py pattern — generate throwaway RSA key for tests
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
import tempfile, pytest

@pytest.fixture(scope="session")
def test_private_key_path(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("keys")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    path = tmp / "test_key.pem"
    path.write_bytes(pem)
    return str(path)
```

**Integration tests (live demo API):**
- Mark with `@pytest.mark.integration`
- Guard with `pytest.importorskip` or `skipif(not os.getenv("KALSHI_INTEGRATION"))` at module level
- Use real `.env` credentials — demo money, safe to use
- Test the complete happy path: auth → discover market → pull orderbook → place limit order → cancel it

### Sampling Rate

- **Per task commit:** `pytest tests/test_kalshi_client.py -v -m "not integration"` (< 5 seconds)
- **Per wave merge:** `pytest tests/ -v -m "not integration"`
- **Phase gate:** Full unit suite green + at least one manual integration run before `/gsd:verify-work`

### Wave 0 Gaps

- [ ] `tests/__init__.py` — empty file
- [ ] `tests/test_kalshi_client.py` — all unit test stubs listed above
- [ ] `tests/conftest.py` — RSA key fixture, mock config fixture
- [ ] `pytest.ini` or `pyproject.toml [tool.pytest.ini_options]` — register `integration` mark
- [ ] `requirements.txt` — must include `pytest-mock` and `responses` (or confirm unittest.mock suffices)
- [ ] `src/__init__.py`, `src/kalshi/__init__.py`, `src/db/__init__.py` — package init files
- [ ] Framework install: `pip install pytest-mock==3.15.1 responses==0.26.0` — if using those libs

---

## Sources

### Primary (HIGH confidence)
- `https://docs.kalshi.com/llms.txt` — complete endpoint list verified
- `https://docs.kalshi.com/api-reference/orders/create-order` — order creation schema
- `https://docs.kalshi.com/api-reference/orders/get-orders` — order list params
- `https://docs.kalshi.com/api-reference/orders/cancel-order` — cancel endpoint path
- `https://docs.kalshi.com/api-reference/portfolio/get-balance` — balance response fields
- `https://docs.kalshi.com/api-reference/portfolio/get-positions` — position response fields
- `https://docs.kalshi.com/api-reference/portfolio/get-fills` — fill response fields (post-migration)
- `https://docs.kalshi.com/api-reference/market/get-markets` — market filter params
- `https://docs.kalshi.com/api-reference/market/get-market-orderbook` — orderbook response structure
- `https://docs.kalshi.com/changelog` — March 2026 breaking changes confirmed
- `https://cryptography.io/en/latest/hazmat/primitives/asymmetric/rsa/` — RSA-PSS signing API
- `https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html` — Retry configuration
- PyPI registry (queried 2026-03-28) — package versions confirmed

### Secondary (MEDIUM confidence)
- `https://agentbets.ai/guides/kalshi-api-guide/` — RSA-PSS code example, auth headers, base URLs (cross-verified with official docs)
- `https://docs.kalshi.com/getting_started/quick_start_market_data` — series filter usage examples
- Kalshi public market URLs (kalshi.com) — confirmed series tickers KXNBAGAME, KXNBA, KXHIGH* family

### Tertiary (LOW confidence)
- NBA props series ticker: inferred from naming convention — not confirmed from official source
- `api.elections.kalshi.com` may be deprecated production hostname: referenced in older REQUIREMENTS.md; verify at runtime

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all core packages verified against PyPI on research date
- API endpoints: HIGH — verified against official docs.kalshi.com
- March 2026 breaking changes: HIGH — confirmed from changelog; critically impacts all response parsing
- Architecture patterns: HIGH — derived from official docs + locked decisions
- Pitfalls: HIGH — most derived from official docs or changelog; one (production URL) is MEDIUM
- NBA props ticker: LOW — not confirmed from official source

**Research date:** 2026-03-28
**Valid until:** 2026-04-28 (Kalshi API is evolving rapidly — re-check changelog before implementation)
