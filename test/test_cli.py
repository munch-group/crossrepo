"""Tests for the command line interface."""

import json
from pathlib import Path

import click
import pytest

from crossrepo import cli


@pytest.fixture()
def conf(repos, tmp_path):
    """A config file covering the fixture repositories."""
    p = tmp_path / "config.toml"
    p.write_text(f'roots = ["{repos}/acme", "{repos}/other"]\n')
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


def test_options_are_accepted_after_the_subcommand(conf, capsys):
    assert run(conf, "list", "--refresh", "--pattern", "*.tsv") == 0
    assert "borders.tsv" in capsys.readouterr().out


def test_get_names_the_version_on_stderr_only(conf, capsys):
    assert run(conf, "get", "sweep-scan:candidates.csv") == 0
    cap = capsys.readouterr()
    assert Path(cap.out.strip()).exists()          # stdout stays composable
    assert "candidates.csv@" in cap.err


def test_help_exits_zero(capsys):
    assert cli.main(["--help"]) == 0
    assert "Catalog and fetch" in capsys.readouterr().out


def test_unknown_command_is_a_usage_error(capsys):
    assert cli.main(["nosuchcommand"]) == 2
    assert "No such command" in capsys.readouterr().err


def test_nothing_configured_says_what_to_do(tmp_path, capsys):
    """The default config reads nothing, so it must say so rather than hang."""
    empty = tmp_path / "empty.toml"
    empty.write_text("asset_dirs = [\"results\"]\n")
    assert cli.main(["--config", str(empty), "list"]) == 1
    err = capsys.readouterr().err
    assert "nothing is configured to read" in err
    assert "owners" in err and "roots" in err
    assert "crossrepo config local append" in err
    assert "Traceback" not in err


def test_refresh_also_refuses_without_sources(tmp_path, capsys):
    empty = tmp_path / "empty.toml"
    empty.write_text("asset_dirs = [\"results\"]\n")
    assert cli.main(["--config", str(empty), "refresh"]) == 1
    assert "nothing is configured to read" in capsys.readouterr().err


def test_owners_alone_count_as_configured():
    """A GitHub-only setup needs no local roots at all. Checked without a request."""
    from crossrepo.config import Config

    cli._require_sources(Config(owners=["munch-group"]))       # must not raise
    cli._require_sources(Config(repos=["munch-group/tree-stats"]))
    with pytest.raises(click.ClickException, match="nothing is configured"):
        cli._require_sources(Config())


def test_list_shows_the_four_columns(conf, capsys):
    assert run(conf, "--refresh", "list") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["repo", "path", "get", "description"]
    assert "Sweep candidates, one row per gene" in out
    assert "acme/sweep-scan" in out                 # the owner comes with it


def test_get_url_prints_the_address_instead_of_downloading(conf, capsys):
    assert run(conf, "get", "sweep-scan:candidates.csv", "--url") == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("https://raw.githubusercontent.com/acme/sweep-scan/")
    assert line.endswith("/results/candidates.csv")


def test_get_url_says_so_when_there_is_no_github_origin(conf, capsys):
    assert run(conf, "get", "big-thing:big.h5", "--url") == 1
    assert "no GitHub origin" in capsys.readouterr().err


def test_an_empty_result_says_what_was_looked_at(tmp_path, capsys):
    """Nothing found has several ordinary causes; the message must say which."""
    (tmp_path / "repos").mkdir()
    conf = tmp_path / "c.toml"
    conf.write_text(f'roots = ["{tmp_path / "repos"}", "{tmp_path / "gone"}"]\n')
    assert cli.main(["--config", str(conf), "--refresh", "list"]) == 1
    err = capsys.readouterr().err
    assert "no such directory" in err                 # the root that is not there
    assert "0 repos" in err                           # the one that is, but is empty
    assert "no owners or repos configured" in err     # and no GitHub source
    assert "crossrepo.yml" in err


def test_refresh_also_explains_an_empty_result(tmp_path, capsys):
    (tmp_path / "repos").mkdir()
    conf = tmp_path / "c.toml"
    conf.write_text(f'roots = ["{tmp_path / "repos"}"]\n')
    assert cli.main(["--config", str(conf), "refresh"]) == 1
    assert "What was looked at" in capsys.readouterr().err


def test_the_count_of_repos_with_a_manifest_is_reported(repos, tmp_path, capsys):
    """A results directory without a manifest is the likely cause, so count it."""
    conf = tmp_path / "c.toml"
    conf.write_text(f'roots = ["{repos}/acme", "{repos}/other"]\ninclude = ["*.nothing"]\n')
    assert cli.main(["--config", str(conf), "--refresh", "list"]) == 1
    err = capsys.readouterr().err
    assert "with a results directory" in err
    assert "with results/crossrepo.yml" in err


def test_list_says_nothing_about_versions(conf, capsys):
    """The version, the size and the URL moved to `crossrepo info`."""
    assert run(conf, "--refresh", "list", "-p", "candidates.csv") == 0
    out = capsys.readouterr().out
    assert "candidates.csv" in out
    assert "version" not in out and "http" not in out


def test_list_refuses_the_options_it_used_to_take(conf):
    """`--version`, `--sha` and `--url` shaped columns it no longer has."""
    for gone in ("--version", "--sha", "--url"):
        assert run(conf, "list", gone, "-p", "candidates.csv") != 0


# ------------------------------------------------- everything about one file

def test_info_prints_what_is_stored(conf, capsys):
    assert run(conf, "--refresh", "info", "sweep-scan:candidates.csv") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "acme/sweep-scan:results/candidates.csv"
    fields = dict(
        line.split(None, 1) for line in out.splitlines()[2:] if line.strip()
    )
    assert fields["description"] == "Sweep candidates, one row per gene"
    assert fields["get"] == "local"
    assert fields["path"] == "results/candidates.csv"
    assert len(fields["version"]) == 40
    assert fields["size"].endswith("B")            # as the terminal writes it


def test_info_takes_a_version_the_way_get_does(conf, capsys):
    run(conf, "--refresh", "versions", "sweep-scan:candidates.csv")
    sha = capsys.readouterr().out.splitlines()[4].split()[0]   # past the rule
    assert run(conf, "info", f"sweep-scan:candidates.csv@{sha}") == 0
    out = capsys.readouterr().out
    assert sha in out


def test_info_says_when_nothing_matches(conf):
    assert run(conf, "info", "sweep-scan:no-such-file.csv") != 0


# ------------------------------------- sources that could not be read

def test_refresh_names_a_root_that_is_not_there(repos, tmp_path, capsys):
    """The scan still runs; what it could not reach is said afterwards."""
    conf = tmp_path / "mixed.toml"
    conf.write_text(
        f'roots = ["{repos}/acme", "{repos}/other", "{tmp_path}/nope"]\n'
    )
    assert cli.main(["--config", str(conf), "refresh"]) == 0
    out, err = capsys.readouterr()
    assert "cataloged" in out                      # the good roots were read
    assert "1 configured source could not be read:" in err
    assert f"{tmp_path}/nope: no such directory" in err


def test_two_bad_sources_are_counted_as_two(repos, tmp_path, capsys):
    conf = tmp_path / "worse.toml"
    conf.write_text(
        f'roots = ["{repos}/acme", "{tmp_path}/nope", "{tmp_path}/also-nope"]\n'
    )
    assert cli.main(["--config", str(conf), "refresh"]) == 0
    err = capsys.readouterr().err
    assert "2 configured sources could not be read:" in err


def test_a_scan_behind_another_command_reports_too(repos, tmp_path, capsys):
    """`list --refresh` scans, so it owes the same account of what it missed."""
    conf = tmp_path / "listing.toml"
    conf.write_text(f'roots = ["{repos}/acme", "{tmp_path}/nope"]\n')
    assert cli.main(["--config", str(conf), "list", "--refresh"]) == 0
    err = capsys.readouterr().err
    assert "1 configured source could not be read:" in err


def test_reading_the_stored_catalog_says_nothing(repos, tmp_path, capsys):
    """Nothing was scanned, so there is nothing to report."""
    conf = tmp_path / "listing.toml"
    conf.write_text(f'roots = ["{repos}/acme", "{tmp_path}/nope"]\n')
    assert cli.main(["--config", str(conf), "refresh"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(conf), "list"]) == 0
    assert "could not be read" not in capsys.readouterr().err
