# Phase 3: Discussion Log

**Date:** 2026-03-29
**Mode:** discuss (interactive)

---

## Area 1: Model Training Trigger

**Q1:** When and how should models train/retrain?
- Options: CLI-only | Auto-train at startup | CLI train + auto-fallback
- **Selected:** CLI-only ✓

**Q2:** Should --train support individual model selection, or always retrain all models together?
- Options: Individual selection | All-or-nothing
- **Selected:** Individual selection ✓

---

## Area 2: Probability Calibration

**Q1:** How should model probabilities be calibrated after GBM/LR training?
- Options: Platt scaling | Isotonic regression | Raw output only
- **Selected:** Platt scaling ✓

**Q2:** Should calibration be applied at predict() time (always calibrated output) or optional?
- Options: Always calibrated | Configurable
- **Selected:** Always calibrated ✓

---

## Area 3: Overfitting Guard Strictness

**Q1:** When an overfitting guard trips, what happens?
- Options: Hard block + --force override | Hard block, no override | Soft warning
- **Selected:** Hard block + --force override ✓

**Q2:** During bootstrapping (first 50 trades), should the bot trade at all?
- Options: Trade with reduced sizing | No trading until guards pass
- **Selected:** Trade with reduced sizing ✓

---

## Area 4: Weather Bias Correction

**Q1:** How should the temperature model correct for NWS systematic forecast errors?
- Options: Additive per station + month | Additive per station + season
- **Selected:** Additive per station + month ✓

**Q2:** After bias correction, how should bracket probabilities be generated?
- Options: Gaussian fit to corrected forecast | NWS ensemble quantiles directly | Historical base rates + NWS blend
- **Selected:** Gaussian fit to corrected forecast ✓
