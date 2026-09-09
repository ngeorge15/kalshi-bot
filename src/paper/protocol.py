"""A predeclared research evaluation protocol.

`src/paper/broker.py`'s `report()` carries an explicit `evidence_note`: synthetic
and replay results are not prospective evidence, and paired Brier scores use
correlated markets that are not independent observations. `src/paper/uncertainty.py`
quantifies that correlation honestly by resampling whole events. Neither module
stops a researcher from running an experiment, looking at the numbers, and then
picking whichever metric, cutoff, or subgroup happens to look good after the fact.
That failure mode -- post-hoc analysis -- is what this module exists to make
harder.

A protocol is a small, immutable record of what will be measured, written down
*before* results are examined: one primary metric (the only one that can produce
a confirmatory verdict), a minimum number of independent events, a pre-committed
threshold, and a stopping rule fixed in advance. Declaring a protocol commits to
these choices; `verify` makes it possible to detect, after the fact, whether a
saved declaration was edited once the results were already known.

Nothing in this module inspects, requires, or is influenced by any results at
declaration time. Nothing it computes constitutes, proves, or establishes a
trading edge. A `met` verdict means a threshold fixed before data collection was
reached -- nothing more.

This module is standard-library only and performs no network calls. It does not
import `PaperBroker`; callers compute `Results` from a report (e.g. from
`src/paper/uncertainty.py`'s event-clustered Brier comparison) and pass them in.
"""
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical JSON uses sorted keys and compact separators so two protocols with
# identical field values always hash identically, regardless of construction
# order or how the JSON happens to be pretty-printed on disk.
_CANONICAL_SEPARATORS = (",", ":")

# Mirrors PaperConfig.run_kind in src/paper/config.py. Duplicated rather than
# imported so this module stays standard-library-only and does not depend on
# (or accidentally couple to) the broker/config it is meant to constrain.
_VALID_RUN_KINDS = ("synthetic", "replay", "forward")

# A protocol that required zero independent events could be "met" before any
# data existed at all; that is not a minimum, it is a no-op guard.
_MIN_EVENTS_FLOOR = 1

# Casual phrasing that describes data-dependent, optional stopping ("check
# periodically, stop once the effect looks good") rather than a rule fixed
# before data collection. This is a best-effort lint on free text -- it
# catches the literal failure mode this module exists to forbid, not every
# way a stopping rule could smuggle in peeking. A human reviewer still matters.
_FORBIDDEN_STOPPING_PHRASES = (
    "looks good", "look good", "seems good", "seems promising",
    "until significant", "when significant", "once significant", "as needed",
)

# SHA-256 hex digests are always this many characters; used only as a cheap
# sanity check before comparing hash strings.
_SHA256_HEX_LENGTH = 64


# ---------------------------------------------------------------------------
# Field validation helpers (shared by declare() and load(), via Protocol.__post_init__)
# ---------------------------------------------------------------------------

def _require_nonempty_text(value: object, field_name: str) -> str:
    """Require a non-whitespace string; raise ValueError naming the field."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty, non-whitespace string")
    return value


def _validate_primary_metric(value: object) -> str:
    """Require exactly one primary metric name -- the guard against fishing.

    A protocol with several "primary" metrics lets a researcher report
    whichever one looks best after the fact, which is exactly the failure
    mode predeclaration exists to prevent. Passing a list, tuple, or set is
    rejected outright rather than silently taking its first element.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        raise TypeError(
            "primary_metric must be exactly one metric name, not a collection of "
            f"{len(value)}; declaring multiple primary metrics is the fishing hole "
            "this field exists to close. List every other metric under secondary_metrics."
        )
    return _require_nonempty_text(value, "primary_metric")


def _validate_secondary_metrics(value: object, primary_metric: str) -> tuple[str, ...]:
    """Normalize secondary metrics to a tuple and forbid overlap with the primary metric."""
    if isinstance(value, str):
        raise TypeError("secondary_metrics must be a list/tuple of metric names, not a single string")
    try:
        metrics = tuple(value)
    except TypeError as exc:
        raise TypeError("secondary_metrics must be an iterable of metric names") from exc
    for metric in metrics:
        _require_nonempty_text(metric, "each secondary metric name")
    if primary_metric in metrics:
        raise ValueError(
            "primary_metric cannot also appear in secondary_metrics; a metric is either "
            "the single confirmatory measure or it is explicitly non-confirmatory, not both"
        )
    if len(set(metrics)) != len(metrics):
        raise ValueError("secondary_metrics contains duplicate entries")
    return metrics


def _validate_min_events(value: object) -> int:
    """Require a positive integer count of independent EVENTS, not markets."""
    if isinstance(value, bool) or not isinstance(value, int) or value < _MIN_EVENTS_FLOOR:
        raise ValueError(
            f"min_events must be an integer >= {_MIN_EVENTS_FLOOR}, counting independent "
            "events (e.g. one city-day), not correlated bracket markets within an event"
        )
    return value


def _validate_decision_threshold(value: object) -> float:
    """Require a finite, pre-committed effect size."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("decision_threshold must be a finite number")
    return float(value)


def _validate_stopping_rule(value: object) -> str:
    """Require a nonempty stopping rule that does not read as optional stopping."""
    text = _require_nonempty_text(value, "stopping_rule")
    lowered = text.lower()
    for phrase in _FORBIDDEN_STOPPING_PHRASES:
        if phrase in lowered:
            raise ValueError(
                f"stopping_rule reads as data-dependent optional stopping ({phrase!r} found); "
                "state a rule fixed before data collection, e.g. a target event count, a "
                "calendar window, or a specific external condition unrelated to the result"
            )
    return text


def _parse_utc_iso8601(value: object, field_name: str) -> str:
    """Parse a timezone-aware ISO 8601 timestamp and re-render it in UTC."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid ISO 8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Protocol:
    """An immutable, predeclared research evaluation protocol.

    Construct instances via `declare()`, not directly -- `declare()` computes
    `content_hash` correctly; a bare `Protocol(...)` call defaults to an empty
    hash that will never validate, which is intentional (see `is_valid`).

    Attributes:
        hypothesis: Plain-text statement of what is being tested.
        primary_metric: The single confirmatory metric name (e.g.
            "event_clustered_paired_brier_improvement"). Exactly one; see
            `_validate_primary_metric`.
        secondary_metrics: Other metrics recorded for context. Explicitly
            non-confirmatory: `evaluate()` never lets them change the verdict.
        min_events: Minimum independent events (not markets) required before
            any conclusion, positive or negative, may be drawn.
        decision_threshold: The pre-committed effect size on `primary_metric`
            that counts as a positive result.
        stopping_rule: When data collection ends, fixed in advance. Must not
            describe stopping once results "look good" (see
            `_validate_stopping_rule`).
        run_kind: Which PaperConfig.run_kind ("synthetic", "replay",
            "forward") this protocol applies to.
        declared_at: UTC ISO 8601 timestamp of declaration.
        declared_by: Who or what declared this protocol.
        content_hash: SHA-256 hex digest of every other field as canonical
            (sort_keys) JSON. Set by `declare()`; used by `is_valid`/`verify`
            to detect edits made after declaration.
    """

    hypothesis: str
    primary_metric: str
    secondary_metrics: tuple[str, ...]
    min_events: int
    decision_threshold: float
    stopping_rule: str
    run_kind: str
    declared_at: str
    declared_by: str
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "hypothesis", _require_nonempty_text(self.hypothesis, "hypothesis"))
        object.__setattr__(self, "primary_metric", _validate_primary_metric(self.primary_metric))
        object.__setattr__(
            self, "secondary_metrics",
            _validate_secondary_metrics(self.secondary_metrics, self.primary_metric),
        )
        object.__setattr__(self, "min_events", _validate_min_events(self.min_events))
        object.__setattr__(self, "decision_threshold", _validate_decision_threshold(self.decision_threshold))
        object.__setattr__(self, "stopping_rule", _validate_stopping_rule(self.stopping_rule))
        if self.run_kind not in _VALID_RUN_KINDS:
            raise ValueError(f"run_kind must be one of {_VALID_RUN_KINDS}, got {self.run_kind!r}")
        object.__setattr__(self, "declared_at", _parse_utc_iso8601(self.declared_at, "declared_at"))
        object.__setattr__(self, "declared_by", _require_nonempty_text(self.declared_by, "declared_by"))
        if not isinstance(self.content_hash, str):
            raise ValueError("content_hash must be a string")


def _fields_for_hash(protocol: Protocol) -> dict:
    """Return every Protocol field except content_hash, as a plain dict."""
    fields = asdict(protocol)
    fields.pop("content_hash", None)
    return fields


def _canonical_json(fields: dict) -> str:
    """Render `fields` as canonical (sorted-key, compact) JSON text.

    `sort_keys=True` makes the text independent of dict/kwarg insertion
    order; `allow_nan=False` refuses to silently serialize a NaN/Infinity
    that should have already failed field validation. This is also the
    normalization point that makes a Python tuple (e.g. `secondary_metrics`)
    and the list JSON deserializes it back into compare as equal -- both
    render to the same JSON array text.
    """
    return json.dumps(fields, sort_keys=True, separators=_CANONICAL_SEPARATORS, allow_nan=False)


def _content_hash(fields: dict) -> str:
    """SHA-256 hex digest of `fields` as canonical (sorted-key, compact) JSON."""
    return hashlib.sha256(_canonical_json(fields).encode("utf-8")).hexdigest()


def _canonicalize(fields: dict) -> dict:
    """Round-trip `fields` through canonical JSON so tuples/lists compare equal.

    `asdict(protocol)` keeps `secondary_metrics` as a tuple; `json.loads` of a
    saved file always returns a list. Both serialize to the same JSON array,
    but `tuple != list` in Python, so a naive dict comparison would report a
    difference that is not really there. Comparing (and diffing) the
    round-tripped form avoids that false positive.
    """
    return json.loads(_canonical_json(fields))


def is_valid(protocol: Protocol) -> bool:
    """Return whether `protocol.content_hash` matches its own current fields.

    This is the core integrity check: a `Protocol` built any way other than
    `declare()` (or a faithful `load()` of a `declare()`'d file) will not have
    a matching hash and is therefore not a real predeclaration.

    Args:
        protocol: The protocol to check.

    Returns:
        bool: True if the stored hash matches a fresh hash of the other fields.
    """
    return protocol.content_hash == _content_hash(_fields_for_hash(protocol))


def declare(
    hypothesis: str,
    primary_metric: str,
    secondary_metrics: Sequence[str],
    min_events: int,
    decision_threshold: float,
    stopping_rule: str,
    run_kind: str,
    declared_by: str,
    declared_at: str | None = None,
) -> Protocol:
    """Declare a research protocol before any results are examined.

    Args:
        hypothesis: Plain-text statement of what is being tested.
        primary_metric: The single confirmatory metric name. Passing a list,
            tuple, or set of names raises -- exactly one primary metric is
            allowed.
        secondary_metrics: Other metrics to record for context; explicitly
            non-confirmatory. May be empty.
        min_events: Minimum independent events (not markets) required before
            any conclusion may be drawn. Must be >= 1.
        decision_threshold: The pre-committed effect size that would count as
            a positive result on `primary_metric`.
        stopping_rule: When data collection ends, decided now. Rejected if it
            reads as "stop once it looks good" (see
            `_validate_stopping_rule`).
        run_kind: One of "synthetic", "replay", "forward".
        declared_by: Who or what is declaring this protocol.
        declared_at: UTC ISO 8601 timestamp of declaration. Defaults to now
            (UTC) if omitted; pass explicitly for reproducible tests.

    Returns:
        Protocol: an immutable record of the predeclaration, with
        `content_hash` set so `verify` can later detect any edit to a saved
        copy.

    Raises:
        TypeError: If `primary_metric` or `secondary_metrics` has the wrong
            shape (e.g. a list where one string, or a string, is required).
        ValueError: If any field fails validation -- see the field
            docstrings on `Protocol` for each rule.

    A returned protocol is a record of intent, not evidence. Declaring it does
    not inspect, require, or get influenced by any results.
    """
    if declared_at is None:
        declared_at = datetime.now(timezone.utc).isoformat()
    provisional = Protocol(
        hypothesis=hypothesis,
        primary_metric=primary_metric,
        secondary_metrics=tuple(secondary_metrics) if not isinstance(secondary_metrics, str) else secondary_metrics,
        min_events=min_events,
        decision_threshold=decision_threshold,
        stopping_rule=stopping_rule,
        run_kind=run_kind,
        declared_at=declared_at,
        declared_by=declared_by,
        content_hash="",
    )
    digest = _content_hash(_fields_for_hash(provisional))
    declared = replace(provisional, content_hash=digest)
    logger.info("Declared protocol %s… (primary_metric=%r, min_events=%d)",
                digest[:12], declared.primary_metric, declared.min_events)
    return declared


def save(protocol: Protocol, path: str | Path) -> None:
    """Persist a declared protocol as deterministic, indented JSON.

    Saving does not re-declare or recompute anything; it writes exactly what
    `declare()` already produced, hash included, so `verify` can later detect
    hand-edits.

    Args:
        protocol: A protocol from `declare()` (or a previously verified
            `load()`).
        path: Destination file path. Parent directories are created as needed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(protocol), sort_keys=True, indent=2) + "\n")
    logger.info("Saved protocol %s… to %s", protocol.content_hash[:12], path)


def load(path: str | Path) -> Protocol:
    """Load a protocol from JSON.

    Reconstructing a `Protocol` re-runs the same field validation `declare()`
    uses, so a file with an out-of-range field fails loudly here. This does
    NOT by itself confirm the stored `content_hash` still matches the stored
    fields -- call `verify()` for that before trusting a loaded protocol.

    Args:
        path: Path to a JSON file previously written by `save()`.

    Returns:
        Protocol: the reconstructed, re-validated protocol.

    Raises:
        ValueError: If the file's fields are missing, extra, or individually
            invalid.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    raw = json.loads(Path(path).read_text())
    try:
        return Protocol(**raw)
    except TypeError as exc:
        raise ValueError(f"Saved protocol at {path} has missing or unexpected fields: {exc}") from exc


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of checking a protocol against its saved, on-disk copy.

    Attributes:
        valid: True only if the file's own hash matches its own content AND
            that content matches the given in-memory protocol exactly.
        message: Human-readable explanation. On failure, this names the
            specific mismatch rather than saying "invalid" generically.
        mismatched_fields: Field names that differ between the given protocol
            and the saved file, if that is the failure mode; empty otherwise.
    """

    valid: bool
    message: str
    mismatched_fields: tuple[str, ...] = ()


def verify(protocol: Protocol, path: str | Path) -> VerificationResult:
    """Detect whether a saved protocol was edited after declaration.

    Performs two independent checks, either of which can fail:

    1. **File self-consistency.** The file's stored `content_hash` must match
       a hash freshly recomputed from the file's own stored fields. This is
       what catches someone hand-editing the JSON (e.g. loosening
       `decision_threshold` after seeing results) without recomputing the hash.
    2. **Agreement with the given protocol.** The in-memory `protocol` passed
       in must match the file's fields exactly. This catches silent
       divergence between code and the saved declaration -- e.g. a protocol
       object mutated (via `dataclasses.replace`) after being declared, or the
       wrong file being checked.

    Args:
        protocol: The in-memory protocol to check against the saved copy.
        path: Path to the saved protocol JSON.

    Returns:
        VerificationResult: `valid=True` only if both checks pass. On
        failure, `message` explains which check failed and, where
        applicable, exactly which fields differ.
    """
    path = Path(path)
    if not path.exists():
        return VerificationResult(False, f"No saved protocol found at {path}.")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return VerificationResult(False, f"Saved protocol at {path} is not valid JSON: {exc}")
    stored_hash = raw.get("content_hash")
    file_fields = {key: value for key, value in raw.items() if key != "content_hash"}
    recomputed_hash = _content_hash(file_fields)
    if stored_hash != recomputed_hash:
        return VerificationResult(
            False,
            f"Saved protocol at {path} was modified after declaration: its stored "
            f"content_hash ({stored_hash!r}) does not match a hash recomputed from its own "
            f"stored fields ({recomputed_hash!r}). Treat any conclusion drawn from this file "
            "as invalid -- it is no longer the record that was declared.",
        )
    given_fields = _canonicalize(_fields_for_hash(protocol))
    if given_fields != file_fields:
        mismatched = tuple(sorted(
            key for key in given_fields
            if given_fields.get(key) != file_fields.get(key)
        ))
        return VerificationResult(
            False,
            f"The given protocol does not match the saved file at {path} in field(s): "
            f"{', '.join(mismatched)}. Re-declare, or load() the saved copy instead of "
            "using a protocol that has diverged from it.",
            mismatched,
        )
    return VerificationResult(
        True, f"Protocol {protocol.content_hash[:12]}… verified against {path}: "
        "content hash is self-consistent and fields agree.",
    )


# ---------------------------------------------------------------------------
# Results and evaluation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Results:
    """Measured outcomes to compare against a predeclared protocol.

    Deliberately decoupled from any particular broker/report shape. The
    caller computes these numbers -- typically from
    `src/paper/uncertainty.py`'s event-clustered Brier comparison, e.g.
    `paired_brier_by_event(rows)["n_events"]` and `["brier_improvement"]" --
    and constructs a `Results` before calling `evaluate()`. A caller that
    wants `evaluate()` to require a conservative bound rather than a point
    estimate can pass a bootstrap CI's lower bound (e.g.
    `cluster_bootstrap_ci(rows)["ci_low"]`) as `primary_metric_value` itself;
    this module does not prescribe which one a protocol's author must use --
    that choice belongs in the protocol's `primary_metric` name.

    Attributes:
        n_events: Count of independent events observed -- e.g. distinct
            event_key values (one city-day), not distinct market tickers.
            Correlated bracket markets sharing one event are one observation.
        primary_metric_value: The measured value of the protocol's single
            `primary_metric`, computed exactly as the protocol describes it.
        secondary_metric_values: Measured values for any `secondary_metrics`,
            keyed by name. Carried through into the verdict for context only;
            `evaluate()` never lets these change the verdict state.
    """

    n_events: int
    primary_metric_value: float
    secondary_metric_values: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.n_events, bool) or not isinstance(self.n_events, int) or self.n_events < 0:
            raise ValueError("n_events must be a nonnegative integer count of independent events")
        if (isinstance(self.primary_metric_value, bool)
                or not isinstance(self.primary_metric_value, (int, float))
                or not math.isfinite(self.primary_metric_value)):
            raise ValueError("primary_metric_value must be a finite number")
        for name, value in self.secondary_metric_values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"secondary metric {name!r} value must be a finite number")


class VerdictState(str, Enum):
    """Distinct outcomes of comparing observed Results against a Protocol.

    INSUFFICIENT_DATA and NOT_MET are deliberately distinct states -- they
    mean different things and must never be conflated. INSUFFICIENT_DATA
    means no conclusion is possible yet, regardless of how the effect looks.
    NOT_MET means enough data existed and the predeclared threshold was not
    reached.
    """

    INSUFFICIENT_DATA = "insufficient_data"
    MET = "met"
    NOT_MET = "not_met"
    INVALID_PROTOCOL = "invalid_protocol"


@dataclass(frozen=True)
class Verdict:
    """The result of evaluating observed Results against a predeclared Protocol.

    A `MET` state means the predeclared `primary_metric` reached the
    predeclared `decision_threshold` over at least `min_events` independent
    events. It is a report that a pre-committed threshold was reached -- it
    does not establish or prove a trading edge, and it says nothing about
    statistical significance beyond what the protocol's author built into
    `primary_metric` and `decision_threshold` themselves.

    Attributes:
        state: One of INSUFFICIENT_DATA, MET, NOT_MET, INVALID_PROTOCOL.
        explanation: Human-readable reason for the verdict.
        protocol_hash: The protocol's `content_hash`, for audit trails.
        n_events: Events observed, if evaluation reached that check.
        primary_metric_value: The measured primary metric, if evaluation
            reached that check.
        decision_threshold: The protocol's threshold, echoed back for context.
        secondary_metric_values: Carried through from `Results`, unchanged.
            Non-confirmatory: never used to decide `state`.
    """

    state: VerdictState
    explanation: str
    protocol_hash: str
    n_events: int | None = None
    primary_metric_value: float | None = None
    decision_threshold: float | None = None
    secondary_metric_values: dict = field(default_factory=dict)


def evaluate(protocol: Protocol, results: Results) -> Verdict:
    """Compare observed Results against a predeclared Protocol.

    Checks, in order: (1) the protocol's own integrity -- an unverifiable
    protocol can never produce a confirmatory verdict; (2) whether
    `min_events` has been reached -- an underpowered result can never produce
    a confirmatory OR disconfirmatory verdict, only INSUFFICIENT_DATA; then
    (3) whether `primary_metric_value` reaches `decision_threshold`.

    Args:
        protocol: A previously declared protocol. Callers should `verify()`
            it against its saved copy before calling this, so a tampered
            protocol is caught with a specific message rather than only
            producing a generic INVALID_PROTOCOL verdict here.
        results: The measured outcomes to compare against it.

    Returns:
        Verdict: exactly one of:
            - INVALID_PROTOCOL: `protocol.content_hash` does not match its
              own fields. Nothing about the protocol can be trusted, so a
              "success" verdict is impossible regardless of `results`.
            - INSUFFICIENT_DATA: fewer than `min_events` independent events
              have been observed. No conclusion, positive or negative, may
              be drawn yet -- this is never reported as NOT_MET.
            - MET / NOT_MET: whether `primary_metric_value` reaches
              `decision_threshold`.

    A MET verdict reports that a threshold fixed before data collection was
    reached. It does not, by itself, establish or prove an edge.
    """
    if not is_valid(protocol):
        return Verdict(
            state=VerdictState.INVALID_PROTOCOL,
            explanation=(
                "The protocol's content_hash does not match its own fields. It was either "
                "never produced by declare()/load(), or it (or its saved file) was edited "
                "after declaration. No verdict -- positive or negative -- can be drawn from "
                "an unverified protocol."
            ),
            protocol_hash=protocol.content_hash,
        )
    if results.n_events < protocol.min_events:
        return Verdict(
            state=VerdictState.INSUFFICIENT_DATA,
            explanation=(
                f"{results.n_events} independent event(s) observed; the protocol predeclared "
                f"a minimum of {protocol.min_events} before any conclusion may be drawn. This "
                "holds no matter how large the observed effect is -- insufficient_data is not "
                "the same conclusion as not_met."
            ),
            protocol_hash=protocol.content_hash,
            n_events=results.n_events,
            primary_metric_value=results.primary_metric_value,
            decision_threshold=protocol.decision_threshold,
            secondary_metric_values=dict(results.secondary_metric_values),
        )
    met = results.primary_metric_value >= protocol.decision_threshold
    note = " Secondary metrics are reported for context only and are non-confirmatory." \
        if results.secondary_metric_values else ""
    explanation = (
        f"Primary metric '{protocol.primary_metric}' = {results.primary_metric_value:.6g} "
        f"{'meets' if met else 'falls short of'} the predeclared decision_threshold "
        f"{protocol.decision_threshold:.6g}, over {results.n_events} independent events "
        f"(>= predeclared min_events {protocol.min_events}). This reflects a predeclared "
        f"threshold being reached, not statistical proof of an edge.{note}"
    )
    return Verdict(
        state=VerdictState.MET if met else VerdictState.NOT_MET,
        explanation=explanation,
        protocol_hash=protocol.content_hash,
        n_events=results.n_events,
        primary_metric_value=results.primary_metric_value,
        decision_threshold=protocol.decision_threshold,
        secondary_metric_values=dict(results.secondary_metric_values),
    )
