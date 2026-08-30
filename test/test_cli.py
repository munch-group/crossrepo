"""Tests for the command line interface."""

import json
from pathlib import Path

import pytest

from labdata import cli


@pytest.fixture()
def conf(repos, tmp_path):
    """A config file covering the fixture repositories."""
    p = tmp_path / "config.toml"
    p.write_text(f'roots = ["{repos}"]\ndepth = 3\n')
    return str(p)


def run(conf, *args):
    """Run the CLI with a config file and return its exit status."""
    return cli.main(["--config", conf, *args])


def test_list_shows_only_published_files(conf, capsys):
    assert run(conf, "--refresh", "list") == 0
    out = capsys.readouterr().out
    assert "acme/sweep-scan" in out and "candidates.csv" in out
    assert "untracked.csv" not in out and "notes.md" not in out


def test_list_json_is_machine_readable(conf, capsys):
    assert run(conf, "list", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert all("spec" in e and "latest" in e for e in data)


def test_list_pattern_filters(conf, capsys):
    assert run(conf, "list", "--pattern", "*.tsv") == 0
    out = capsys.readouterr().out
    assert "borders.tsv" in out and "candidates.csv" not in out


def test_repos_summarises(conf, capsys):
    assert run(conf, "repos") == 0
    assert "REPO" in capsys.readouterr().out


def test_get_prints_only_a_usable_path(conf, capsys):
    assert run(conf, "get", "sweep-scan:candidates.csv") == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.read_text().count("\n") == 4


def test_get_out_writes_a_copy(conf, tmp_path, capsys):
    dest = tmp_path / "copy.csv"
    assert run(conf, "get", "sweep-scan:candidates.csv", "-o", str(dest)) == 0
    capsys.readouterr()
    assert dest.read_text().count("\n") == 4


def test_ambiguity_is_reported_as_an_error_not_a_traceback(conf, capsys):
    assert run(conf, "--refresh", "get", "sweep-scan:stable.csv") == 1
    assert "ambiguous" in capsys.readouterr().err


def test_human_readable_sizes():
    assert cli.human(948) == "948B"
    assert cli.human(512189753) == "488.5M"
    assert cli.human(-1) == "?"
