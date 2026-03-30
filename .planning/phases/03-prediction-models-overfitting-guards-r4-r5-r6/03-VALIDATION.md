---
phase: 3
slug: prediction-models-overfitting-guards-r4-r5-r6
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-29
---

# Phase 3 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pytest.ini or pyproject.toml [tool.pytest.ini_options] |
| **Quick run command** | `python -m pytest tests/models/ tests/validation/ -x -q` |
| **Full suite command** | `python -m pytest tests/ -q` |
| **Estimated runtime** | ~30 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python -m pytest tests/models/ tests/validation/ -x -q`
- **After every plan wave:** Run `python -m pytest tests/ -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 3-01-01 | 01 | 1 | R4 | unit | `python -m pytest tests/models/test_nba_game.py -x -q` | ❌ W0 | ⬜ pending |
| 3-01-02 | 01 | 1 | R4 | unit | `python -m pytest tests/models/test_nba_totals.py -x -q` | ❌ W0 | ⬜ pending |
| 3-01-03 | 01 | 1 | R4 | unit | `python -m pytest tests/models/test_nba_props.py -x -q` | ❌ W0 | ⬜ pending |
| 3-01-04 | 01 | 1 | R5 | unit | `python -m pytest tests/models/test_weather_temp.py -x -q` | ❌ W0 | ⬜ pending |
| 3-01-05 | 01 | 1 | R5 | unit | `python -m pytest tests/models/test_weather_precip.py -x -q` | ❌ W0 | ⬜ pending |
| 3-02-01 | 02 | 1 | R4 | unit | `python -m pytest tests/models/test_model_store.py -x -q` | ❌ W0 | ⬜ pending |
| 3-03-01 | 03 | 2 | R6 | unit | `python -m pytest tests/validation/test_splitter.py -x -q` | ❌ W0 | ⬜ pending |
| 3-03-02 | 03 | 2 | R6 | unit | `python -m pytest tests/validation/test_walk_forward.py -x -q` | ❌ W0 | ⬜ pending |
| 3-03-03 | 03 | 2 | R6 | unit | `python -m pytest tests/validation/test_guards.py -x -q` | ❌ W0 | ⬜ pending |
| 3-04-01 | 04 | 3 | R4,R5,R6 | integration | `python -m pytest tests/integration/test_phase3_e2e.py -x -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/models/__init__.py` — package init
- [ ] `tests/models/test_nba_game.py` — stubs for R4 NBA game model
- [ ] `tests/models/test_nba_totals.py` — stubs for R4 NBA totals model
- [ ] `tests/models/test_nba_props.py` — stubs for R4 player prop model
- [ ] `tests/models/test_weather_temp.py` — stubs for R5 weather temperature model
- [ ] `tests/models/test_weather_precip.py` — stubs for R5 weather precipitation model
- [ ] `tests/models/test_model_store.py` — stubs for R4 model versioning/persistence
- [ ] `tests/validation/__init__.py` — package init
- [ ] `tests/validation/test_splitter.py` — stubs for R6 train/test/holdout splitter
- [ ] `tests/validation/test_walk_forward.py` — stubs for R6 walk-forward validator
- [ ] `tests/validation/test_guards.py` — stubs for R6 overfitting guards
- [ ] `tests/integration/test_phase3_e2e.py` — end-to-end prediction flow stubs

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Holdout never touched during training | R6 | Requires audit of training code paths | Inspect splitter.py holdout write-protection; verify no training loop reads holdout split |
| Calibrated probabilities sum correctly | R4, R5 | Numerical calibration output requires visual inspection | Run predict() on sample data; verify probabilities are in [0,1] and plausible |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
