# Predeclared evaluation protocols

This document explains `src/paper/protocol.py`: what a predeclared protocol is,
why this project uses one, how to declare and verify one, and what its verdicts
do and do not mean. It does not describe any specific experiment's results.

## Why this exists

`src/paper/broker.py`'s `report()` already carries an `evidence_note`: synthetic
and replay results are not prospective evidence, and paired Brier scores compare
correlated bracket markets that are not independent observations.
`src/paper/uncertainty.py` quantifies that correlation honestly, by resampling
whole events instead of individual markets. Neither module stops a researcher
from running an experiment, looking at the numbers, and then choosing whichever
metric, cutoff, or subgroup happens to look good after the fact.

That failure mode is post-hoc analysis. It does not require dishonesty — it is
what happens by default when a metric, a threshold, and a stopping point are
chosen with the results already in view. A predeclared protocol closes this by
fixing those choices *before* data collection, and by making it detectable if
the written-down choices are edited later.

A protocol is not a statistical test and does not compute a p-value or a
confidence interval by itself (`src/paper/uncertainty.py` does that). It is a
constraint on interpretation: it says what will count as the confirmatory
comparison, how much data is required before any conclusion is drawn, and when
to stop collecting. Declaring one does not generate evidence. Meeting one does
not prove an edge exists.

## What a protocol commits to

`src/paper/protocol.declare()` returns an immutable `Protocol` with:

| Field | Meaning |
| --- | --- |
| `hypothesis` | Plain-text statement of what is being tested. |
| `primary_metric` | Exactly **one** confirmatory metric. Passing more than one raises — this is the fishing hole a protocol exists to close. |
| `secondary_metrics` | Other metrics recorded for context. Explicitly non-confirmatory: they are reported alongside a verdict but never change it. |
| `min_events` | Minimum independent **events** — not markets — required before any conclusion may be drawn. Kalshi offers one weather event (one city, one date) as many correlated bracket markets; counting each bracket as a separate observation overstates confidence. See `src/paper/uncertainty.py`. |
| `decision_threshold` | The effect size on `primary_metric`, fixed now, that would count as a positive result. |
| `stopping_rule` | When data collection ends, decided now. Text that reads as "stop once it looks good" is rejected outright. |
| `run_kind` | Which experiment mode (`synthetic`, `replay`, `forward`) this protocol governs. |
| `declared_at`, `declared_by` | When and by whom the protocol was declared. |
| `content_hash` | SHA-256 over every other field, as canonical JSON. This is what makes tampering detectable. |

## Declaring, saving, and verifying a protocol

```python
from src.paper.protocol import declare, save, load, verify, evaluate, Results

protocol = declare(
    hypothesis="The weather baseline model has a lower event-clustered "
                "Brier score than the market on temperature brackets.",
    primary_metric="event_clustered_paired_brier_improvement",
    secondary_metrics=["realized_pnl_cents", "fill_rate"],
    min_events=30,
    decision_threshold=0.01,
    stopping_rule="Stop after 30 independent city-day events or on "
                   "2026-12-31, whichever comes first.",
    run_kind="replay",
    declared_by="nikhi",
)
save(protocol, "data/paper/protocols/weather-baseline-v1.json")
```

Do this **before** collecting the data the protocol will be evaluated against.
Declaring a protocol after looking at results defeats the entire point, and
nothing in this module can detect that particular failure — only human
discipline about *when* `declare()` is called can.

Once saved, the file can be checked for later edits:

```python
declared = load("data/paper/protocols/weather-baseline-v1.json")
result = verify(declared, "data/paper/protocols/weather-baseline-v1.json")
assert result.valid, result.message
```

`verify` checks two things independently:

1. **File self-consistency** — the file's own stored `content_hash` still
   matches a hash freshly recomputed from its own stored fields. This is what
   catches a hand-edited JSON file (e.g. loosening `decision_threshold` after
   seeing a disappointing result) where the hash was not recomputed to match.
2. **Agreement with the in-memory protocol** — the `Protocol` object passed in
   matches the saved file field-for-field. This catches a protocol that
   diverged from its saved declaration in code (e.g. via `dataclasses.replace`
   after the fact), or the wrong file being checked.

Either failure returns `valid=False` with a message that names the specific
field or hash mismatch — never a bare "invalid".

A `Protocol` built any way other than `declare()` (or a faithful `load()` of a
file `declare()` produced) defaults to an empty `content_hash` and will never
pass `is_valid()` or `verify()`. There is no way to construct a "declared"
protocol without actually declaring it.

## Evaluating results

`evaluate(protocol, results)` compares measured `Results` — `n_events`,
`primary_metric_value`, and optional `secondary_metric_values` — against the
protocol, and returns a `Verdict` with one of four states:

| State | Meaning |
| --- | --- |
| `invalid_protocol` | The protocol's hash does not match its own fields. Nothing about it can be trusted; a "success" verdict is impossible. |
| `insufficient_data` | Fewer than `min_events` independent events observed. **No conclusion — positive or negative — may be drawn yet, regardless of how large the observed effect is.** |
| `met` | `min_events` was reached and `primary_metric_value` reached `decision_threshold`. |
| `not_met` | `min_events` was reached and `primary_metric_value` did not reach `decision_threshold`. |

These checks run in that order. A protocol whose hash fails never gets to
report `met`, no matter what the data shows. An underpowered result is always
`insufficient_data`, never a disguised `not_met` — the two mean different
things, and conflating them either overstates a null result's confidence or
discards a promising one that simply needs more data.

`Results` is deliberately decoupled from `PaperBroker.report()`'s shape; the
caller computes `n_events` and `primary_metric_value` (typically from
`src/paper/uncertainty.py`'s `paired_brier_by_event()`, or its
`cluster_bootstrap_ci()` if the protocol's author wants `evaluate()` to require
a conservative confidence bound rather than a point estimate — that choice is
encoded in what value the caller passes as `primary_metric_value`, not in this
module).

## Worked example

Declare, before collecting any data:

```python
protocol = declare(
    hypothesis="The weather baseline model has a lower event-clustered "
                "paired Brier score than the market on NYC temperature "
                "brackets over the 2026 fall replay window.",
    primary_metric="event_clustered_paired_brier_improvement",
    secondary_metrics=["realized_pnl_cents"],
    min_events=30,
    decision_threshold=0.01,
    stopping_rule="Stop after 30 independent city-day events or on "
                   "2026-12-31, whichever comes first.",
    run_kind="replay",
    declared_by="nikhi",
    declared_at="2026-09-04T12:00:00Z",
)
save(protocol, "data/paper/protocols/nyc-temp-v1.json")
```

Later, after the replay window closes, aggregate the day's predictions with
`paired_brier_by_event()` and evaluate:

```python
from src.paper.uncertainty import paired_brier_by_event

aggregate = paired_brier_by_event(rows)  # rows built from broker.report()
declared = load("data/paper/protocols/nyc-temp-v1.json")
assert verify(declared, "data/paper/protocols/nyc-temp-v1.json").valid

results = Results(
    n_events=aggregate["n_events"],
    primary_metric_value=aggregate["brier_improvement"],
)
verdict = evaluate(declared, results)
print(verdict.state, verdict.explanation)
```

If only 12 independent city-days occurred by the stopping date, `verdict.state`
is `insufficient_data` — even if `brier_improvement` looks large — because 12
is below the predeclared `min_events` of 30. If 34 city-days occurred and
`brier_improvement` is `0.014`, `verdict.state` is `met`: the predeclared
threshold was reached. That is what `met` means here — a predeclared bar was
cleared on a predeclared sample. It is not a claim of statistical significance
beyond what `primary_metric` and `decision_threshold` themselves encode, and it
does not establish or prove a trading edge.

## What this does not do

- It does not compute a confidence interval, a p-value, or any measure of
  statistical significance. Use `src/paper/uncertainty.py` for that, and fold
  its output into `primary_metric_value` if the protocol's author wants the
  bar to be a conservative bound rather than a point estimate.
- It does not stop a researcher from declaring a protocol after already having
  looked at the data. It only makes a *later* edit to a *saved* declaration
  detectable. Declaring before data collection is a discipline this module
  supports, not one it can enforce by itself.
- It does not evaluate `secondary_metrics` for pass/fail. They are recorded for
  context and are explicitly non-confirmatory — the verdict never depends on
  them, however good or bad they look.
- A `met` verdict is a report that a pre-committed threshold was reached. It is
  not evidence of a measured trading edge on its own, and should be read
  alongside `PaperBroker.report()`'s own `evidence_note`.
