"""Tests for the offline watchlist-authoring CLI subcommands.

These commands (`watchlist-scaffold`, `watchlist-validate`, `watchlist-explain`)
are authoring tools: they must work without an initialized experiment database
and must never create one. Coverage here is CLI plumbing (argument parsing,
JSON/text rendering, exit codes, file I/O); the underlying semantics of
`scaffold`/`validate`/`bracket_coverage`/`explain` are tested in
`tests/paper/test_watchlist.py`.
"""
from datetime import datetime, timezone
import json

import pytest

import src.paper.cli as cli
from src.paper.cli import main
from src.paper.watchlist import scaffold


FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    """Every test runs against a fixed instant, never the system clock."""
    monkeypatch.setattr(cli, "_now", lambda: FIXED_NOW)


def _missing_db(tmp_path) -> str:
    """A --db path that does not exist; watchlist commands must not create it."""
    return str(tmp_path / "nonexistent.db")


class TestScaffold:
    def test_happy_path_open_tails(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        assert main(["--db", db, "watchlist-scaffold", "KNYC", "2026-03-15",
                     "--bracket", ":69.5", "--bracket", "69.5:72.5",
                     "--bracket", "72.5:"]) == 0
        entries = json.loads(capsys.readouterr().out)
        assert len(entries) == 3
        bounds = [(e["weather_spec"]["lower_bound_f"], e["weather_spec"]["upper_bound_f"]) for e in entries]
        assert bounds == [(None, 69.5), (69.5, 72.5), (72.5, None)]
        assert not (tmp_path / "nonexistent.db").exists()

    def test_negative_bound_requires_equals_form(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        assert main(["--db", db, "watchlist-scaffold", "KNYC", "2026-03-15",
                     "--bracket=-5:0"]) == 0
        entries = json.loads(capsys.readouterr().out)
        assert entries[0]["weather_spec"]["lower_bound_f"] == -5.0
        assert entries[0]["weather_spec"]["upper_bound_f"] == 0.0

    def test_malformed_bracket_fails_clearly(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        assert main(["--db", db, "watchlist-scaffold", "KNYC", "2026-03-15",
                     "--bracket", "not-a-bracket"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("Paper command failed:")
        assert not (tmp_path / "nonexistent.db").exists()

    def test_malformed_bracket_extra_colon(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        assert main(["--db", db, "watchlist-scaffold", "KNYC", "2026-03-15",
                     "--bracket", "1:2:3"]) == 2
        assert "Paper command failed:" in capsys.readouterr().err

    def test_unknown_station(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        assert main(["--db", db, "watchlist-scaffold", "ZZZZ", "2026-03-15",
                     "--bracket", "69.5:72.5"]) == 2
        assert "Unknown station_code" in capsys.readouterr().err

    def test_output_writes_file(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        out = tmp_path / "wl.json"
        assert main(["--db", db, "watchlist-scaffold", "KNYC", "2026-03-15",
                     "--bracket", "69.5:72.5", "--output", str(out)]) == 0
        stdout = capsys.readouterr().out
        assert json.loads(out.read_text()) == json.loads(stdout)
        assert not (tmp_path / "nonexistent.db").exists()

    def test_refuses_to_overwrite_without_force(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        out = tmp_path / "wl.json"
        out.write_text("existing content")
        assert main(["--db", db, "watchlist-scaffold", "KNYC", "2026-03-15",
                     "--bracket", "69.5:72.5", "--output", str(out)]) == 2
        assert "already exists" in capsys.readouterr().err
        assert out.read_text() == "existing content"

    def test_force_overwrites(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        out = tmp_path / "wl.json"
        out.write_text("existing content")
        assert main(["--db", db, "watchlist-scaffold", "KNYC", "2026-03-15",
                     "--bracket", "69.5:72.5", "--output", str(out), "--force"]) == 0
        assert json.loads(out.read_text())

    def test_output_cannot_overwrite_db_path(self, tmp_path, capsys):
        db = tmp_path / "same.db"
        assert main(["--db", str(db), "watchlist-scaffold", "KNYC", "2026-03-15",
                     "--bracket", "69.5:72.5", "--output", str(db)]) == 2
        assert "cannot overwrite the paper database" in capsys.readouterr().err
        assert not db.exists()

    def test_no_db_created_on_success(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        assert main(["--db", db, "watchlist-scaffold", "KNYC", "2026-03-15",
                     "--bracket", "69.5:72.5"]) == 0
        capsys.readouterr()
        assert not (tmp_path / "nonexistent.db").exists()


def _build_reviewed_entry(tmp_path):
    """A fully reviewed, valid entry, built from scaffold() to keep it internally consistent."""
    [entry] = scaffold("KNYC", "2026-03-15", [(69.5, 72.5)], now=FIXED_NOW)
    entry["ticker"] = "KXHIGHNY-26MAR15-B70"
    entry["rules_source"] = "https://kalshi.com/markets/kxhighny/rules"
    entry["eligibility"] = {
        "available": True,
        "checked_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-02T00:00:00Z",
        "source": "manually confirmed tradeable on 2026-01-01",
    }
    return entry


class TestValidate:
    def test_valid_watchlist_exits_zero(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        path = tmp_path / "wl.json"
        path.write_text(json.dumps([_build_reviewed_entry(tmp_path)]))
        assert main(["--db", db, "watchlist-validate", str(path)]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["errors"] == []
        assert "coverage" in payload
        assert not (tmp_path / "nonexistent.db").exists()

    def test_scaffold_output_is_invalid(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        path = tmp_path / "wl.json"
        entries = scaffold("KNYC", "2026-03-15", [(69.5, 72.5)], now=FIXED_NOW)
        path.write_text(json.dumps(entries))
        assert main(["--db", db, "watchlist-validate", str(path)]) == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["errors"]

    def test_json_shape_and_is_nan_safe(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        path = tmp_path / "wl.json"
        path.write_text(json.dumps([_build_reviewed_entry(tmp_path)]))
        main(["--db", db, "watchlist-validate", str(path)])
        out = capsys.readouterr().out
        payload = json.loads(out)  # would fail on inf/nan tokens
        assert set(payload) == {"ok", "errors", "warnings", "coverage"}
        for finding in payload["errors"] + payload["warnings"]:
            assert set(finding) == {"index", "field", "severity", "message"}

    def test_missing_file(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        assert main(["--db", db, "watchlist-validate", str(tmp_path / "nope.json")]) == 2
        assert capsys.readouterr().err.startswith("Paper command failed:")

    def test_invalid_json(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        path = tmp_path / "bad.json"
        path.write_text("{not valid json")
        assert main(["--db", db, "watchlist-validate", str(path)]) == 2
        assert capsys.readouterr().err.startswith("Paper command failed:")


class TestExplain:
    def test_prints_plain_text(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        path = tmp_path / "wl.json"
        path.write_text(json.dumps([_build_reviewed_entry(tmp_path)]))
        assert main(["--db", db, "watchlist-explain", str(path)]) == 0
        out = capsys.readouterr().out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)
        assert "NYC-2026-03-15" in out
        assert "bracket(s)" in out

    def test_missing_file(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        assert main(["--db", db, "watchlist-explain", str(tmp_path / "nope.json")]) == 2
        assert capsys.readouterr().err.startswith("Paper command failed:")

    def test_invalid_json(self, tmp_path, capsys):
        db = _missing_db(tmp_path)
        path = tmp_path / "bad.json"
        path.write_text("not json at all")
        assert main(["--db", db, "watchlist-explain", str(path)]) == 2
        assert capsys.readouterr().err.startswith("Paper command failed:")
