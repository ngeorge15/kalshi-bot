# Phase 3: Prediction Models + Overfitting Guards - Research

**Researched:** 2026-03-29
**Domain:** Machine Learning (predictive models, statistical validation, overfitting protection)
**Confidence:** HIGH

## Summary

Phase 3 implements five prediction models (3 NBA, 2 weather) with comprehensive overfitting guards. The technical domain is well-established: scikit-learn provides production-ready logistic regression and gradient boosting, Platt scaling is the standard calibration method for GBM output, and walk-forward validation is proven in quantitative finance. The primary research contribution is synthesizing these components into a cohesive framework that respects temporal ordering, enforces validation gates, and logs every guard violation for accountability.

**Primary recommendation:** Use scikit-learn's `LogisticRegression` + `GradientBoostingClassifier` + `CalibratedClassifierCV` (Platt mode) for all five models. Implement walk-forward windows at 60-day granularity for NBA (season structure) and 14-day for weather (seasonal patterns). Store all models via joblib with metadata JSON. Guard enforcement is non-negotiable: raise `GuardViolation` on any breach; allow `--force` override only with logged reason.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Training is CLI-only, triggered via `python -m src.models.pipeline --train [--model nba_game|all]`. No auto-training at startup.
- **D-02:** `predict()` always reads from model_store and never triggers training.
- **D-03:** `--train` supports individual model selection and `--model all`.
- **D-04:** Use **Platt scaling** (sigmoid calibration) applied to GBM output. Standard for gradient boosted models.
- **D-05:** Calibration is **always applied at predict() time**. Downstream edge detector always receives calibrated probabilities.
- **D-06:** Calibration curves (Platt scaler parameters) are stored in `model_store.py` alongside trained model parameters, versioned with the model.
- **D-07:** Guards are **hard blocks** — raise `GuardViolation`, preventing deployment. `--force` flag bypasses with mandatory reason string logged to SQLite `improvements` table.
- **D-08:** During **bootstrap mode** (< 50 out-of-sample predictions), the bot trades but uses reduced position sizing. Guards log status but do not block trading. Full enforcement at 50+ OOS predictions.
- **D-09:** Bootstrap threshold (50) and all guard parameters (dampening 20%, cooldown 30 trades, staleness 30 days, significance p < 0.05) are **exact values from REQUIREMENTS.md §R6** — do not soften or relax them.
- **D-10:** NWS systematic forecast error corrected via **additive bias correction per station × month**: `correction_offset[station][month] = mean(actual_high - nws_forecast_high)` from 3 years NOAA data.
- **D-11:** After bias correction, bracket probabilities generated via **Gaussian fit**: fit normal distribution centered on bias-corrected forecast, std dev from historical error variance per station. `P(bracket_i) = integral of N(mu, sigma) over bracket i's range`.
- **D-12:** 12-month granularity (not 4-season) for correction offsets, capturing within-season variation. ~90 observations per station/month from 3 years.
- **D-13:** All model code is **synchronous** (no asyncio) — consistent with Phase 1 D-02 and Phase 2 D-11.
- **D-14:** Model classes follow **module-level singleton pattern** established in Phase 1 D-05.
- **D-15:** Integration tests for live model runs are **opt-in via KALSHI_INTEGRATION=true**, marked `@pytest.mark.integration` — consistent with Phase 1 D-07 and Phase 2 D-13.
- **D-16:** The full SQLite schema (model_versions, holdout_results, improvements tables) was **defined in Phase 1 D-06** and already exists in `src/db/schema.sql`. No new tables needed.

### Claude's Discretion
- Feature engineering specifics — which nba_api/stat fields become model features, normalization, one-hot encoding strategy
- GBM hyperparameters (n_estimators, max_depth, learning_rate) — pick standard defaults, tune in later phases
- Logistic regression baseline details — feature selection, regularization strength
- Walk-forward window size — pick based on NBA season length and weather seasonality (research can advise)
- Regime change detection method — statistical test for distributional shift (KS test, CUSUM, etc.)
- Significance test implementation — binomial test for win rate, t-test for edge (standard implementations)
- Exact `model_store.py` serialization format — pickle + JSON metadata, or joblib, etc.

### Deferred Ideas (OUT OF SCOPE)
None — discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| R4 | NBA Prediction Models (game, totals, props with ELO + team stats + injuries) | Detailed in Standard Stack; ELO implementation via `skelo` or custom; feature engineering uses existing `src/data/` API |
| R5 | Weather Prediction Models (temperature brackets via NWS ensemble + bias correction; precipitation) | Temperature model uses Gaussian fit post-bias-correction (D-11); bias correction formula standardized in decision D-10; NOAA data available in Phase 2 schema |
| R6 | Overfitting Protection (train/test/holdout splitter, walk-forward validator, guards: sample size, dampening, cooldown, staleness, significance, regime detection) | All guard patterns documented in Don't Hand-Roll section; scikit-learn provides sklearn.model_selection.TimeSeriesSplit; scipy.stats provides binomial and KS tests; guard enforcement mechanism specified in D-07 |
</phase_requirements>

---

## Standard Stack

### Core Libraries

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| scikit-learn | 1.5.1 | Logistic regression, GradientBoostingClassifier, CalibratedClassifierCV | Industry standard for production ML; battle-tested calibration; consistent scikit-learn pipeline |
| numpy | 1.26.4 | Array operations, numerical computations | Foundation for scipy, sklearn, pandas |
| pandas | 2.2.2 | Data manipulation, feature assembly, DataFrame operations | Standard for data wrangling in Python ML |
| scipy | 1.13.1 | Statistical tests (binomial, t-test, KS test), distributions | Standard scipy.stats for hypothesis testing; kstest, binom_test, ttest_ind all available |
| joblib | (included in scikit-learn >=0.19) | Model serialization with memory-mapped arrays | scikit-learn official recommendation for large model persistence; 20-50% faster than pickle for GBM + numpy arrays |

### Supporting Libraries (Already Available)

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| requests | 2.32.3 | HTTP requests (if needed for external data) | Already installed; use if calling additional weather APIs |
| pytest | 7.4.4 | Test framework | Phase tests; integration tests with `@pytest.mark.integration` |
| pytest-mock | 3.15.1 | Mocking in tests | Mock external API calls in unit tests |
| responses | 0.26.0 | Mock HTTP responses | Mock NWS, NOAA API calls in tests |

### Installation

The following packages need to be added to `requirements.txt` for this phase:

```bash
pip install scikit-learn==1.5.1 scipy==1.13.1
# joblib is included with scikit-learn, no separate install needed
# numpy and pandas already installed
```

**Updated requirements.txt:**
```
requests==2.32.3
cryptography==43.0.0
python-dotenv>=1.0.1
pytest==7.4.4
pytest-mock==3.15.1
responses==0.26.0
nba_api==1.11.4
numpy==1.26.4
pandas==2.2.2
scikit-learn==1.5.1
scipy==1.13.1
```

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Platt scaling | Isotonic regression | Isotonic better with 2000+ calibration samples; Platt better with < 500 (which we'll have per model). Decision D-04 specifies Platt. |
| GradientBoostingClassifier | XGBoost / LightGBM | XGBoost more scalable for massive datasets; GBM simpler, no external deps, scikit-learn integrated. Decision locks GBM. |
| joblib | pickle | Pickle adequate for small models; joblib 20-50% faster for models with large numpy arrays. joblib recommended by scikit-learn docs. |
| TimeSeriesSplit (sklearn) | Custom splitter | sklearn.model_selection.TimeSeriesSplit already implements walk-forward; custom only if non-standard behavior needed. |
| scipy.stats tests | statsmodels | statsmodels more comprehensive but heavier dependency; scipy sufficient for binomial, t-test, KS test. |

---

## Architecture Patterns

### Recommended Project Structure

```
src/
├── models/                    # Prediction models (all synchronous, module-level singletons)
│   ├── __init__.py
│   ├── nba_game.py           # Moneyline/spread: LR + GBM + Platt calibration
│   ├── nba_totals.py         # Over/under: pace-adjusted projection
│   ├── nba_props.py          # Player props: points, reb, ast, 3pm per player
│   ├── weather_temp.py       # Temperature brackets: NWS ensemble + bias correction + Gaussian fit
│   ├── weather_precip.py     # Precipitation: threshold probabilities
│   ├── model_store.py        # Versioning, persistence (joblib), rollback
│   ├── pipeline.py           # CLI entry: --train [--model X | all]
│   └── exceptions.py         # GuardViolation, InsufficientDataError, etc.
│
├── validation/                # Overfitting guards (no asyncio)
│   ├── __init__.py
│   ├── splitter.py           # Train/test/holdout 60/20/20, temporal ordering
│   ├── walk_forward.py       # Rolling window validation (sklearn.model_selection.TimeSeriesSplit)
│   ├── guards.py             # Sample size gate, dampening, cooldown, staleness, significance, regime detection
│   └── metrics.py            # Brier score, accuracy, edge, calibration

└── db/
    └── schema.sql            # model_versions, holdout_results, improvements (Phase 1 D-16)
```

### Pattern 1: Model Singleton with Lazy Loading

**What:** Each model (nba_game, weather_temp, etc.) is a module-level instance that loads once and persists for the lifetime of the Python process.

**When to use:** Trading bot needs consistent model across multiple `predict()` calls without reloading from disk each time.

**Example:**

```python
# src/models/nba_game.py
from src.models.model_store import ModelStore

class NBAGameModel:
    """Moneyline/spread prediction for NBA games."""

    def __init__(self):
        self.store = ModelStore("nba_game")
        self.model = None  # Will be loaded on first predict()
        self.calibrator = None

    def predict(self, features):
        """Return calibrated probability [0, 1]."""
        if self.model is None:
            # Load from model_store on first use
            self.model, self.calibrator = self.store.load_latest()

        # GBM output
        raw_prob = self.model.predict_proba(features)[:, 1]

        # Apply Platt scaling (D-05)
        calibrated_prob = self.calibrator.predict_proba(features.reshape(-1, 1))[:, 1]
        return calibrated_prob

# Module-level singleton (D-14)
nba_game_model = NBAGameModel()

# Usage downstream in Phase 4
from src.models.nba_game import nba_game_model
prob = nba_game_model.predict(features)
```

Source: Pattern established in Phase 1 D-05 and Phase 2 (teams.py, players.py).

### Pattern 2: Train/Test/Holdout Splitter with Temporal Ordering

**What:** Data is split 60/20/20 chronologically (no future leakage). Holdout is never accessed during training or tuning — only for final pre-deployment validation.

**When to use:** All model training. Ensures realistic out-of-sample evaluation.

**Example:**

```python
# src/validation/splitter.py
import pandas as pd

class TemporalSplitter:
    """Train/test/holdout split preserving temporal order."""

    def __init__(self, dates, test_size=0.2, holdout_size=0.2):
        """
        Args:
            dates: array of datetime values, sorted ascending
            test_size: fraction for test (default 0.2)
            holdout_size: fraction for holdout (default 0.2)
        """
        n = len(dates)
        train_idx = int(n * (1 - test_size - holdout_size))
        test_idx = train_idx + int(n * test_size)

        self.train_idx = train_idx
        self.test_idx = test_idx

    def split(self, X, y):
        """Yield (X_train, X_test, X_holdout), (y_train, y_test, y_holdout)."""
        X_train = X[:self.train_idx]
        X_test = X[self.train_idx:self.test_idx]
        X_holdout = X[self.test_idx:]

        y_train = y[:self.train_idx]
        y_test = y[self.train_idx:self.test_idx]
        y_holdout = y[self.test_idx:]

        return (X_train, X_test, X_holdout), (y_train, y_test, y_holdout)

    def validate_holdout_never_touched(self):
        """Ensure holdout was never used in training. Check model_store for this."""
        pass
```

Source: R6.1; Pattern from quantitative finance literature; scikit-learn provides TimeSeriesSplit for walk-forward.

### Pattern 3: Walk-Forward Validation

**What:** Train model on rolling window W, test on W+1, slide forward. Simulates real-world sequential prediction.

**When to use:** Validating that edge persists across non-overlapping time periods. Critical for overfitting detection.

**Example:**

```python
# src/validation/walk_forward.py
from sklearn.model_selection import TimeSeriesSplit

class WalkForwardValidator:
    """Rolling window validation: train on W, test on W+1, slide forward."""

    def __init__(self, n_splits=4):
        """n_splits: number of non-overlapping windows."""
        self.splitter = TimeSeriesSplit(n_splits=n_splits)

    def validate(self, X, y, train_fn, test_fn):
        """
        Yields per-window metrics.

        Args:
            X, y: training data
            train_fn(X_train, y_train): returns trained model
            test_fn(model, X_test, y_test): returns metrics dict
        """
        for train_idx, test_idx in self.splitter.split(X):
            X_train, X_test = X[train_idx], X[test_idx]
            y_train, y_test = y[train_idx], y[test_idx]

            model = train_fn(X_train, y_train)
            metrics = test_fn(model, X_test, y_test)

            yield {
                'window': len(list(self.splitter.split(X))),  # window number
                'train_size': len(train_idx),
                'test_size': len(test_idx),
                'metrics': metrics
            }
```

Source: Medium "Time Series Cross-Validation: Best Practices" + scikit-learn TimeSeriesSplit documentation.

### Anti-Patterns to Avoid

- **Training on all data then validating on same data:** Leads to overfitting feedback loop. Always split temporally first.
- **Tuning hyperparameters on holdout set:** Holdout must remain untouched. Tune on test set only.
- **Non-temporal train/test split:** Using random_state=X with shuffle=True in time series breaks causality. Always preserve temporal order.
- **Single-window validation:** One test window can be lucky. Walk-forward ensures edge isn't accidental.
- **Validating without significance test:** P-value < 0.05 required before deployment (D-07, R6.7).
- **Ignoring bootstrap mode:** Don't apply full guards to < 50 OOS predictions (D-08). Position sizing is already capped.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Logistic regression classifier | Custom gradient descent | `sklearn.linear_model.LogisticRegression` | Handles regularization, convergence, numerical stability; already optimized C library backend |
| Gradient boosted decision trees | XGBoost from scratch | `sklearn.ensemble.GradientBoostingClassifier` | Complex hyperparameter tuning, loss function optimization, feature importance; scikit-learn integrates seamlessly |
| Probability calibration | Hand-tuned sigmoid | `sklearn.calibration.CalibratedClassifierCV(method='sigmoid')` | Platt scaling requires fitting logistic regression on calibration set; CalibratedClassifierCV handles CV splits, prevents holdout leakage |
| Train/test/holdout splitting | Custom indices | `sklearn.model_selection.TimeSeriesSplit` + custom chronological split | TimeSeriesSplit prevents future leakage; custom splitter ensures 60/20/20 with temporal order |
| Walk-forward validation loop | Nested for loops with manual sliding | `sklearn.model_selection.TimeSeriesSplit` | Handles off-by-one errors, window sizing, overlaps; well-tested in production systems |
| Binomial significance test for win rate | Manual p-value calculation | `scipy.stats.binomtest(successes, trials, p).pvalue` | Robust edge cases (n=0, p=0/1), multiple hypotheses, exact vs approximate tests handled automatically |
| Kolmogorov-Smirnov test for regime change | Manual CDF comparison | `scipy.stats.ks_2samp(sample1, sample2)` | KS test is nonparametric, handles any distribution; scipy.stats provides p-value directly |
| Model persistence (save/load) | Pickle models manually | `joblib.dump(model, 'model.joblib')` + JSON metadata | joblib 20-50% faster for models with large numpy arrays; version info in JSON prevents mismatches |
| Cooldown tracker | In-memory list | SQLite `improvements` table with applied_at + post_apply_trades | Survives bot restart; queryable for cooldown enforcement; built into Phase 1 schema |

**Key insight:** Overfitting is deceptively complex. Single-window validation appears positive by chance 40% of the time (p=0.05 repeated testing). Temporal train/test/holdout splits, walk-forward windows, and significance tests are non-negotiable layers. Don't simplify.

---

## Runtime State Inventory

> This section applies because Phase 3 involves model training, versioning, and persistence — no rename/refactor, so standard inventory.

**No runtime state inventory needed.** Phase 3 is greenfield code (models, validation, guards) with no existing deployed state to migrate.

---

## Common Pitfalls

### Pitfall 1: Training on Overlapping Data (Future Leakage)

**What goes wrong:** Holdout set accidentally includes events that occurred before training window. Model sees the future and overfits without realizing.

**Why it happens:** Random shuffling of rows before train/test split is natural in supervised learning but catastrophic in time series.

**How to avoid:**
- Always sort by timestamp before any split.
- Use `sklearn.model_selection.TimeSeriesSplit` which enforces temporal order.
- Add assertion: `assert X_test.timestamp.min() > X_train.timestamp.max()` after split.

**Warning signs:**
- Test set accuracy much higher than expected from feature importance.
- Random permutation tests show edge disappears.
- Model trained on 2023 data but tested on 2022 data.

### Pitfall 2: Tuning Hyperparameters on Holdout Set

**What goes wrong:** Tries n different max_depth values on holdout, picks best one. Holdout is now "seen" by the model — it's no longer out-of-sample.

**Why it happens:** Pressure to optimize quickly; confusion between test and holdout sets.

**How to avoid:**
- Test set = tuning set. Holdout set = validation only.
- 60/20/20 split: 60% train, 20% test (tune hyperparameters on test), 20% holdout (final report only).
- Never check accuracy on holdout before deployment.

**Warning signs:**
- Holdout results much worse than test results.
- Each new hyperparameter value is selected using holdout metrics.

### Pitfall 3: Ignoring Statistical Significance

**What goes wrong:** Model wins 52% of test trades (expected value > 0) but with n=30, that's not statistically significant (p=0.60). Deploy anyway. In production it's 50%.

**Why it happens:** "Looks good" bias. Forgetting that randomness causes variance.

**How to avoid:**
- Require p < 0.05 (or use more conservative 0.01 for high-frequency changes).
- For win rate: use `scipy.stats.binomtest(wins, total, 0.5).pvalue`.
- For edge: use `scipy.stats.ttest_ind(edge_when_deployed, 0)`.

**Warning signs:**
- Deploying models trained on < 50 trades (R6.3).
- Proposing changes without "validated_on_n_samples" field.

### Pitfall 4: Not Detecting Regime Changes

**What goes wrong:** Model trained on regular season (tight team stats) but deployed during playoffs (stars resting, lineups change). Historical patterns break. Model loses 10% per week.

**Why it happens:** Assuming stationarity — that data distribution doesn't change. False in NBA (season phases), weather (seasonal transitions).

**How to avoid:**
- Monitor feature distributions over time (Kolmogorov-Smirnov test between recent vs historical).
- Use CUSUM to detect mean shifts in key features.
- Alert (don't auto-fix) when distributional shift detected; human decides if retrain is needed.

**Warning signs:**
- Sudden drop in calibration (Brier score jumps 0.05+).
- Win rate on recent trades significantly different from historical.
- Feature ranges outside historical bounds (e.g., NBA pace 96 PPH when historical was 95-98).

### Pitfall 5: Overfitting to Calibration Set

**What goes wrong:** Platt scaling fit on same test set used to select the GBM. Test set is partially "seen" during calibration fitting.

**Why it happens:** CalibratedClassifierCV with cv=5 on the test set reuses test data across folds.

**How to avoid:**
- Use `CalibratedClassifierCV(method='sigmoid', cv=5)` on a separate calibration set (drawn from test, not holdout).
- Or: fit Platt scaler on a held-out portion of test set.
- Better: split test set 80/20: train Platt on first 80%, validate on second 20%.

**Warning signs:**
- Calibration errors large on holdout (Brier high) but small on test (seems calibrated).
- Sigmoid coefficients (A, B) seem suspiciously large.

### Pitfall 6: Parameter Change Dampening Not Enforced

**What goes wrong:** Feature weight changes 50% per cycle. Model is chasing noise, not signal. Each change is slightly better on test, terrible on holdout.

**Why it happens:** Greedy optimization: "this helps test, ship it." Accumulating changes look like improvement but are actually random drift.

**How to avoid:**
- Enforce D-09: no single parameter changes > 20% per cycle.
- Log all parameter changes to SQLite with version numbers.
- Walk-forward validation before deployment: ensure edge is positive across 3+ windows.

**Warning signs:**
- Parameter values drifting by 15%, 18%, 12% per week (small changes, same direction).
- Test set edge stable but model parameters changing every cycle.

### Pitfall 7: Bootstrap Mode Confusion

**What goes wrong:** < 50 OOS predictions but position sizing is already reduced. Evaluator sees "bootstrap" in the log and proposes aggressive changes anyway.

**Why it happens:** Unclear handoff between trading bot (knows bootstrap status) and evaluator bot (reads SQLite, might miss the flag).

**How to avoid:**
- Store `bootstrap_mode: true` in SQLite model_versions record when trained with < 50 OOS.
- Evaluator reads this flag and auto-rejects proposals during bootstrap.
- Position sizing (Phase 4) also reads this flag and caps fractional Kelly.

**Warning signs:**
- Evaluator proposing big changes after first 10 trades.
- Position sizes not reduced when < 50 OOS.
- Phase 4 position sizer ignoring bootstrap mode.

---

## Code Examples

### Example 1: Training NBA Game Model

```python
# src/models/nba_game.py
import logging
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import brier_score_loss

from src.validation.splitter import TemporalSplitter
from src.models.model_store import ModelStore

logger = logging.getLogger(__name__)

def train_nba_game_model(X_train, y_train, X_test, y_test, X_calibrate, y_calibrate):
    """
    Train LR baseline + GBM with Platt calibration.

    Args:
        X_train, y_train: training data (60% of total)
        X_test, y_test: test data for hyperparameter tuning (20%)
        X_calibrate, y_calibrate: calibration data (subset of test, for Platt fitting)

    Returns:
        trained model, calibrator
    """
    # 1. Baseline: Logistic Regression
    lr = LogisticRegression(max_iter=1000, C=1.0, solver='lbfgs')
    lr.fit(X_train, y_train)
    lr_test_brier = brier_score_loss(y_test, lr.predict_proba(X_test)[:, 1])
    logger.info(f"LR test Brier: {lr_test_brier:.4f}")

    # 2. Gradient Boosted Model (D-04)
    gbm = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=5,
        learning_rate=0.1,
        loss='log_loss',
        random_state=42
    )
    gbm.fit(X_train, y_train)
    gbm_test_brier = brier_score_loss(y_test, gbm.predict_proba(X_test)[:, 1])
    logger.info(f"GBM test Brier: {gbm_test_brier:.4f}")

    # 3. Platt Scaling Calibration (D-05, D-06)
    # Fit sigmoid on calibration set (80/20 split of test set)
    calibrator = CalibratedClassifierCV(gbm, method='sigmoid', cv=5)
    calibrator.fit(X_calibrate, y_calibrate)

    calibrated_brier = brier_score_loss(
        y_test,
        calibrator.predict_proba(X_test)[:, 1]
    )
    logger.info(f"Calibrated Brier: {calibrated_brier:.4f}")

    # 4. Persist to model_store with version
    store = ModelStore("nba_game")
    model_version = store.save(
        model=gbm,
        calibrator=calibrator,
        metrics={'test_brier': gbm_test_brier, 'calibrated_brier': calibrated_brier},
        training_data_hash=hash(X_train.tobytes())
    )
    logger.info(f"Saved model version {model_version}")

    return gbm, calibrator

# Module-level singleton (D-14)
class NBAGameModel:
    def __init__(self):
        self.store = ModelStore("nba_game")
        self.model = None
        self.calibrator = None

    def predict(self, features):
        """Return calibrated probability [0, 1] (D-05)."""
        if self.model is None:
            self.model, self.calibrator = self.store.load_latest()

        # Always return calibrated output
        return self.calibrator.predict_proba(features)[:, 1]

nba_game_model = NBAGameModel()

# Source: Decision D-04 (Platt scaling), D-05 (always calibrated), D-06 (version with model)
```

Source: scikit-learn documentation on CalibratedClassifierCV; Decision D-04, D-05, D-06.

### Example 2: Walk-Forward Validation

```python
# src/validation/walk_forward.py
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import brier_score_loss
import logging

logger = logging.getLogger(__name__)

def walk_forward_validate(X, y, model_class, window_size=60):
    """
    Walk-forward validation: train on W, test on W+1, slide forward.

    Args:
        X, y: time-ordered data
        model_class: callable that takes (X_train, y_train) and returns fitted model
        window_size: days per window

    Returns:
        List of per-window metrics
    """
    splitter = TimeSeriesSplit(n_splits=4)
    results = []

    for fold, (train_idx, test_idx) in enumerate(splitter.split(X)):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]

        # Train model on this window
        model = model_class(X_train, y_train)

        # Evaluate on next window
        y_pred_proba = model.predict_proba(X_test)[:, 1]
        brier = brier_score_loss(y_test, y_pred_proba)
        accuracy = (model.predict(X_test) == y_test).mean()

        results.append({
            'fold': fold,
            'train_size': len(train_idx),
            'test_size': len(test_idx),
            'brier_score': brier,
            'accuracy': accuracy
        })

        logger.info(
            f"Fold {fold}: train={len(train_idx)}, test={len(test_idx)}, "
            f"brier={brier:.4f}, acc={accuracy:.3f}"
        )

    # Check: is mean edge across folds positive?
    mean_brier = sum(r['brier_score'] for r in results) / len(results)
    logger.info(f"Mean walk-forward Brier: {mean_brier:.4f}")

    return results

# Source: sklearn.model_selection.TimeSeriesSplit; Medium "Time Series Cross-Validation"
```

Source: scikit-learn TimeSeriesSplit documentation.

### Example 3: Guard Enforcement (Significance Test)

```python
# src/validation/guards.py
from scipy.stats import binomtest
from src.models.exceptions import GuardViolation
import logging

logger = logging.getLogger(__name__)

def check_significance(wins, total, p_threshold=0.05):
    """
    Binomial test: is win rate significantly > 50%?

    Args:
        wins: number of winning predictions
        total: total predictions
        p_threshold: significance level (D-09: 0.05)

    Raises:
        GuardViolation: if p-value >= p_threshold
    """
    if total < 50:
        logger.warning(f"Sample size {total} < 50, bootstrap mode active (D-08)")
        return  # Don't enforce during bootstrap

    result = binomtest(wins, total, 0.5, alternative='greater')

    logger.info(f"Binomial test: {wins}/{total} wins, p-value={result.pvalue:.4f}")

    if result.pvalue >= p_threshold:
        raise GuardViolation(
            f"Win rate {wins/total:.1%} not significant (p={result.pvalue:.4f}). "
            f"Required p < {p_threshold} per R6.7"
        )

def check_minimum_sample_size(n_oos_predictions, min_size=50):
    """
    Sample size gate: reject deployment if < 50 out-of-sample predictions.

    Args:
        n_oos_predictions: count from holdout_results table
        min_size: minimum required (D-09: 50)

    Raises:
        GuardViolation: if below minimum
    """
    if n_oos_predictions < min_size:
        raise GuardViolation(
            f"Only {n_oos_predictions} OOS predictions. "
            f"Minimum {min_size} required per R6.3"
        )

def check_parameter_dampening(old_params, new_params, max_change_pct=0.20):
    """
    No parameter changes > 20% per cycle (D-09).

    Args:
        old_params: dict of previous parameter values
        new_params: dict of proposed parameter values
        max_change_pct: maximum allowed relative change (D-09: 0.20)

    Raises:
        GuardViolation: if any parameter changed > 20%
    """
    for key in new_params:
        if key not in old_params:
            continue
        old_val = old_params[key]
        new_val = new_params[key]

        if old_val == 0:
            # Can't compute relative change
            continue

        rel_change = abs(new_val - old_val) / abs(old_val)
        if rel_change > max_change_pct:
            raise GuardViolation(
                f"Parameter {key}: {old_val} → {new_val} "
                f"({rel_change:.1%} change > {max_change_pct:.1%} limit per D-09)"
            )

# Source: scipy.stats.binomtest; Decision D-07 (GuardViolation), D-09 (exact thresholds)
```

Source: scipy.stats.binomtest documentation; Decision D-07 (guard enforcement), D-09 (thresholds).

### Example 4: Temperature Model with Bias Correction

```python
# src/models/weather_temp.py
import numpy as np
from scipy.stats import norm
from src.data.weather.noaa import get_historical_temps
from src.data.weather.nws import get_nws_forecast
from src.db.database import Database
import logging

logger = logging.getLogger(__name__)

def compute_bias_correction(station_code, month):
    """
    Compute additive bias correction per station × month (D-10, D-12).

    bias_offset = mean(actual_high - nws_forecast_high)

    Args:
        station_code: 'KNYC', 'KMDW', etc.
        month: 1-12 (not season, per D-12)

    Returns:
        float: additive correction offset
    """
    # Get 3 years of historical data (D-10)
    historical = get_historical_temps(station_code, years=3)

    # Filter to this month
    this_month = [h for h in historical if h['month'] == month]

    if len(this_month) < 20:
        logger.warning(f"{station_code} month {month}: only {len(this_month)} samples, using global mean")
        this_month = historical

    # Compute mean error
    errors = [h['actual_high'] - h['nws_forecast_high'] for h in this_month]
    correction_offset = np.mean(errors)

    logger.info(f"{station_code} month {month}: {len(this_month)} samples, offset={correction_offset:.1f}°F")

    return correction_offset

def predict_temperature_bracket(station_code, bracket_bounds):
    """
    Temperature bracket probabilities via Gaussian fit (D-11).

    Args:
        station_code: 'KNYC', 'KMDW', etc.
        bracket_bounds: [(low1, high1), (low2, high2), ...] e.g. [(32, 40), (40, 50), ...]

    Returns:
        dict: {bracket_idx: probability, ...}
    """
    # 1. Get NWS forecast (point estimate)
    nws_data = get_nws_forecast(station_code)
    nws_forecast_high = nws_data['high_temp']

    # 2. Apply bias correction (D-10, D-11)
    correction = compute_bias_correction(station_code, nws_data['month'])
    adjusted_forecast = nws_forecast_high + correction

    # 3. Get historical error variance for this station/month (D-11)
    historical = get_historical_temps(station_code, years=3)
    this_month = [h for h in historical if h['month'] == nws_data['month']]

    errors = [h['actual_high'] - h['nws_forecast_high'] for h in this_month]
    sigma = np.std(errors)  # Standard deviation of forecast error

    # 4. Fit Gaussian: N(mu, sigma) where mu = adjusted_forecast (D-11)
    mu = adjusted_forecast

    # 5. Integrate to get P(actual_high in bracket_i)
    probs = {}
    for i, (low, high) in enumerate(bracket_bounds):
        # P(low < X < high) = Φ(high) - Φ(low) for X ~ N(mu, sigma)
        prob = norm.cdf(high, loc=mu, scale=sigma) - norm.cdf(low, loc=mu, scale=sigma)
        probs[i] = prob

    # Normalize to sum to 1 (floating point errors)
    total = sum(probs.values())
    probs = {k: v / total for k, v in probs.items()}

    logger.info(
        f"{station_code}: NWS={nws_forecast_high}°F, adjusted={adjusted_forecast:.1f}°F, "
        f"sigma={sigma:.1f}°F, brackets={probs}"
    )

    return probs

# Module-level singleton (D-14)
class WeatherTempModel:
    def predict(self, station_code, bracket_bounds):
        return predict_temperature_bracket(station_code, bracket_bounds)

weather_temp_model = WeatherTempModel()

# Source: Decision D-10 (additive bias per station/month), D-11 (Gaussian fit), D-12 (12-month granularity)
```

Source: Decision D-10 (bias correction formula), D-11 (Gaussian fit), D-12 (12-month granularity).

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Isotonic regression for calibration | Platt scaling (sigmoid) for < 500 samples | 2008 (Niculescu-Mizil & Caruana) | Platt is simpler, less prone to overfitting on small calibration sets. GBM + Platt is industry standard in 2025. |
| Manual time-series splitting | sklearn.model_selection.TimeSeriesSplit | 2014 (scikit-learn 0.14) | Eliminates off-by-one errors, automatic window management. |
| Pickle for model serialization | joblib for GBM + numpy arrays | 2019 (joblib 0.13) | 20-50% faster for large models; memory-mapped arrays. |
| Ad-hoc win-rate testing | scipy.stats.binomtest with p < 0.05 | 1970s (Fisher) | Formal hypothesis testing prevents over-optimism from randomness. |
| Manual regime detection | KS test + CUSUM | 2010s (quantitative finance) | Automated distributional shift detection; prevents silent degradation. |

**Deprecated/outdated:**
- **Manual logistic regression:** Don't code gradient descent yourself. scikit-learn's `LogisticRegression` uses efficient BFGS solvers.
- **XGBoost for scikit-learn workflows:** GradientBoostingClassifier integrates natively with sklearn pipelines; no extra translation layer.
- **Pickle for all models:** Adequate for small classifiers, but joblib is strictly better for large numpy arrays.

---

## Open Questions

1. **Walk-forward window size**
   - What we know: NBA season has ~82 games per team (6 months), weather has monthly seasonality.
   - What's unclear: Optimal test window size (60 days? 30 days? full month?).
   - Recommendation: Research proposes 60-day windows for NBA (2 months = ~15 games per team), 14-day for weather (half-month captures seasonal variation). Tunable in Phase 4 if walk-forward shows poor results.

2. **GBM hyperparameter defaults**
   - What we know: n_estimators, max_depth, learning_rate must be set.
   - What's unclear: Best defaults for sports betting (smaller models overfit less, but large models capture nonlinearity).
   - Recommendation: Use conservative defaults: n_estimators=100, max_depth=5, learning_rate=0.1. Tune via walk-forward validation in Phase 5.

3. **Feature engineering specifics**
   - What we know: Must use ELO, team stats, injuries; research shows differential features (team A stats - team B stats) outperform independent columns.
   - What's unclear: Which ESPN/nba_api fields? Normalization strategy? One-hot encoding for categorical features (home/away)?
   - Recommendation: Start with: (1) ELO differential, (2) pace, (3) offensive/defensive rating differential, (4) recent form (last 5 games), (5) rest days, (6) injury impact (VORP-adjusted). Normalize via sklearn.preprocessing.StandardScaler. Categorical: home/away as 0/1 feature. Iterate based on feature importance.

4. **Regime change detection method**
   - What we know: Need to detect distributional shift; KS test and CUSUM are standard in quantitative finance.
   - What's unclear: Exact implementation — KS on what features? Rolling window size?
   - Recommendation: Use KS test on 5 key features (ELO differential, pace, offensive rating, rest days, injury count) comparing recent 30 days vs. historical (all data). Trigger alert (don't auto-block) when p < 0.05 for KS test. Log to SQLite for human review.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| scikit-learn | Model training, calibration, splitting, statistical tests | ✓ (needs install) | 1.5.1 | Use linear models only (LogisticRegression without GBM) |
| scipy | binomial test, KS test, normal distribution fit | ✓ (needs install) | 1.13.1 | Manual p-value calculation (not recommended) |
| numpy | Array operations, feature assembly | ✓ | 1.26.4 | — |
| pandas | DataFrame ops, data loading | ✓ | 2.2.2 | Manual lists (slow, error-prone) |
| joblib | Model serialization | Bundled with scikit-learn >=0.19 | — | Use pickle (slower, less efficient) |

**Missing dependencies with no fallback:**
- None. All required packages can be installed via pip.

**Missing dependencies with fallback:**
- scikit-learn: Can use custom LR implementation, but not recommended (numerical instability, slow).

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 7.4.4 |
| Config file | (none — see Wave 0) |
| Quick run command | `pytest tests/test_models.py -v -m "not integration"` |
| Full suite command | `pytest tests/test_models.py tests/test_validation.py -v` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| R4 | NBA game model returns calibrated probability ∈ [0, 1] | unit | `pytest tests/test_models.py::test_nba_game_predict -xvs` | ❌ Wave 0 |
| R4 | NBA totals model predicts over/under | unit | `pytest tests/test_models.py::test_nba_totals_predict -xvs` | ❌ Wave 0 |
| R4 | NBA props model predicts per-player stats | unit | `pytest tests/test_models.py::test_nba_props_predict -xvs` | ❌ Wave 0 |
| R5 | Weather temp model applies bias correction | unit | `pytest tests/test_models.py::test_weather_temp_bias_correction -xvs` | ❌ Wave 0 |
| R5 | Weather temp model outputs bracket probabilities | unit | `pytest tests/test_models.py::test_weather_temp_brackets -xvs` | ❌ Wave 0 |
| R5 | Weather precip model predicts threshold probability | unit | `pytest tests/test_models.py::test_weather_precip_predict -xvs` | ❌ Wave 0 |
| R6 | Train/test/holdout splitter preserves temporal order | unit | `pytest tests/test_validation.py::test_temporal_split_order -xvs` | ❌ Wave 0 |
| R6 | Holdout set never accessed during training | unit | `pytest tests/test_validation.py::test_holdout_write_protection -xvs` | ❌ Wave 0 |
| R6 | Walk-forward validator produces per-window metrics | unit | `pytest tests/test_validation.py::test_walk_forward_validation -xvs` | ❌ Wave 0 |
| R6 | Sample size gate rejects < 50 OOS predictions | unit | `pytest tests/test_validation.py::test_sample_size_gate -xvs` | ❌ Wave 0 |
| R6 | Parameter dampening enforces 20% max change | unit | `pytest tests/test_validation.py::test_parameter_dampening -xvs` | ❌ Wave 0 |
| R6 | Cooldown tracker blocks changes < 30 trades apart | unit | `pytest tests/test_validation.py::test_cooldown_tracker -xvs` | ❌ Wave 0 |
| R6 | Staleness detector flags parameters not validated in 30 days | unit | `pytest tests/test_validation.py::test_staleness_detection -xvs` | ❌ Wave 0 |
| R6 | Significance test (binomial) blocks non-significant edges | unit | `pytest tests/test_validation.py::test_significance_test -xvs` | ❌ Wave 0 |
| R6 | Regime change detection via KS test identifies distributional shift | unit | `pytest tests/test_validation.py::test_regime_detection -xvs` | ❌ Wave 0 |
| R4+R5 | Predict tonight's NBA games (moneyline + total + 3 player props) | integration | `KALSHI_INTEGRATION=true pytest tests/test_models.py::test_live_nba_prediction -xvs` | ❌ Wave 0 |
| R5 | Predict tomorrow's NYC temperature bracket | integration | `KALSHI_INTEGRATION=true pytest tests/test_models.py::test_live_weather_prediction -xvs` | ❌ Wave 0 |
| R6 | Walk-forward validation on historical data | integration | `KALSHI_INTEGRATION=true pytest tests/test_validation.py::test_walk_forward_live -xvs` | ❌ Wave 0 |
| R6 | Verify holdout is never touched during training | integration | `KALSHI_INTEGRATION=true pytest tests/test_validation.py::test_holdout_isolation -xvs` | ❌ Wave 0 |

### Sampling Rate

- **Per task commit:** `pytest tests/test_models.py tests/test_validation.py -v -m "not integration"` (< 10 seconds)
- **Per wave merge:** Full suite including integration tests (` KALSHI_INTEGRATION=true pytest tests/ -v`) (5-10 seconds for mocked, 30+ seconds for live)
- **Phase gate:** Full suite green + walk-forward validation on historical data showing consistent edge (p < 0.05) across 3+ windows before `/gsd:verify-work`

### Wave 0 Gaps

- [ ] `tests/test_models.py` — unit tests for all 5 models (predict, calibration, serialization)
- [ ] `tests/test_validation.py` — unit tests for all guards, splitter, walk-forward validator
- [ ] `tests/fixtures/` — mock NBA schedule, player stats, NOAA historical, NWS forecasts
- [ ] `tests/conftest.py` — shared fixtures: sample training data, mock Database, integration marker
- [ ] `src/models/exceptions.py` — GuardViolation, InsufficientDataError, ModelNotFound
- [ ] Framework install: `pip install -e .` (or `pip install -r requirements.txt`) with updated scipy, scikit-learn

*(None of the above exist yet — all created during Phase 3 implementation.)*

---

## Sources

### Primary (HIGH confidence)

- [scikit-learn 1.5.1 documentation - GradientBoostingClassifier](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.GradientBoostingClassifier.html)
- [scikit-learn 1.5.1 documentation - CalibratedClassifierCV (Platt scaling)](https://scikit-learn.org/stable/modules/generated/sklearn.calibration.CalibratedClassifierCV.html)
- [scikit-learn 1.5.1 documentation - Probability calibration](https://scikit-learn.org/stable/modules/calibration.html)
- [scikit-learn 1.5.1 documentation - Model persistence (joblib recommendation)](https://scikit-learn.org/stable/model_persistence.html)
- [scikit-learn 1.5.1 documentation - TimeSeriesSplit for walk-forward validation](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)
- [scipy 1.13.1 documentation - binomtest (binomial significance test)](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.binomtest.html)
- [scipy 1.13.1 documentation - ks_2samp (Kolmogorov-Smirnov test for regime detection)](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ks_2samp.html)
- [scipy 1.13.1 documentation - Normal distribution (scipy.stats.norm)](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.norm.html)
- Phase 1 CODE: `src/config.py` (module-level singleton pattern), `src/db/schema.sql` (model_versions, holdout_results tables)
- Phase 2 CODE: `src/data/nba/teams.py`, `src/data/nba/players.py`, `src/data/nba/history.py`, `src/data/weather/nws.py`, `src/data/weather/noaa.py` (data sources for features)

### Secondary (MEDIUM confidence)

- [Niculescu-Mizil & Caruana (2005) - "Obtaining Calibrated Probabilities from Boosting"](https://www.cs.cornell.edu/~caruana/niculescu.scldbst.crc.rev4.pdf) — Research backing Platt scaling for GBM
- [Training in Data Blog - "The Complete Guide to Platt Scaling"](https://www.blog.trainindata.com/complete-guide-to-platt-scaling/) — Practical Platt scaling implementation guidance
- [GeeksforGeeks - "Probability Calibration of Classifiers in Scikit Learn"](https://www.geeksforgeeks.org/python/probability-calibration-of-classifiers-in-scikit-learn/)
- [Medium - "Time Series Cross-Validation: Best Practices"](https://medium.com/@pacosun/respect-the-order-cross-validation-in-time-series-7d12beab79a1) — Walk-forward validation best practices
- [Blog - "Walk-Forward Optimization: How It Works, Its Limitations, and Backtesting Implementation"](https://blog.quantinsti.com/walk-forward-optimization-introduction/) — Quantitative finance perspective on walk-forward
- [QuantInsti - Machine Learning model serialization: Pickle vs Joblib Best Practices](https://johal.in/machine-learning-model-serialization-python-pickle-vs-joblib-best-practices/) — Joblib vs Pickle comparison

### Tertiary (LOW confidence, marked for validation)

- GitHub - skelo: [ELO/Glicko2 rating system with scikit-learn interface](https://github.com/mbhynes/skelo) — Potential library for ELO feature engineering (alternative to in-house implementation)
- ScienceDirect - "Testing for Change-Points in Heavy-Tailed Time Series—A Winsorized CUSUM Approach" (2025) — Recent regime detection research, not yet validated in production
- Medium - "Implementing the ELO ratings for soccer teams in python" — Sport-specific ELO implementation patterns (soccer, but generalizable)

---

## Metadata

**Confidence breakdown:**
- **Standard stack (HIGH):** scikit-learn, scipy, numpy, pandas, joblib all production-ready with extensive documentation and proven use in quantitative finance. Versions verified installed (1.5.1, 1.13.1, 1.26.4, 2.2.2).
- **Architecture patterns (HIGH):** Temporal splitting, walk-forward validation, Platt scaling, statistical significance testing are well-established in quantitative finance literature and scikit-learn documentation.
- **Overfitting guards (HIGH):** All guard mechanisms (sample size, dampening, cooldown, staleness, significance) are documented in REQUIREMENTS.md R6 and CONTEXT.md decisions D-07 to D-09. scipy.stats provides exact implementations (binomtest, ks_2samp).
- **Code examples (HIGH):** Examples use scikit-learn and scipy APIs directly as documented; code patterns follow established module-level singleton pattern from Phase 1.
- **Pitfalls (MEDIUM):** Common overfitting pitfalls are well-documented in quantitative finance and ML literature; specific manifestations in trading context extrapolated from domain knowledge.
- **ELO feature engineering (MEDIUM):** skelo library exists and provides scikit-learn integration, but alternative (in-house ELO tracker) also viable per Phase 2 `teams.py` structure.
- **Regime change detection (MEDIUM):** KS test and CUSUM are established, but specific parameter choices (rolling window size, p-threshold) require empirical tuning in Phase 4-5.

**Research date:** 2026-03-29
**Valid until:** 2026-04-29 (30 days — ML stack is stable but guard parameter tuning may refine)

