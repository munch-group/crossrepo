"""Tests for warning that a pinned version has been overtaken."""

import pytest

from crossrepo import cli, core


def test_pinning_an_older_content_warns(by_spec, cfg, capsys):
    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    old = core.versions(entry)[-1]                  # the first version of three
    core.get("sweep-scan", "candidates.csv", old.sha, cfg=cfg)
    err = capsys.readouterr().err
    assert "a newer version" in err
    assert entry.latest.sha in err                  # the sha to move the pin to
    assert old.sha in err


def test_pinning_the_current_content_is_silent(by_spec, cfg, capsys):
    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    core.get("sweep-scan", "candidates.csv", entry.latest.sha, cfg=cfg)
    assert capsys.readouterr().err == ""


def test_a_repo_moving_on_is_not_news_if_the_file_did_not_change(by_spec, cfg, capsys):
    """stable.csv last changed in the first commit; later commits do not touch it."""
    entry = by_spec["acme/sweep-scan:results/stable.csv"]
    first = core.versions(entry)[-1]
    assert first.sha != entry.latest.sha            # the repo has moved on
    assert first.blob == entry.latest.blob          # but this file has not
    core.get("sweep-scan", "results/stable.csv", first.sha, cfg=cfg)
    assert capsys.readouterr().err == ""


def test_quiet_does_not_hide_the_warning(by_spec, cfg, capsys):
    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    old = core.versions(entry)[-1]
    core.get("sweep-scan", "candidates.csv", old.sha, quiet=True, cfg=cfg)
    captured = capsys.readouterr()
    assert captured.out == ""                       # the pin line is suppressed
    assert "a newer version" in captured.err        # the warning is not


def test_asking_for_the_latest_never_warns(cfg, capsys):
    core.get("sweep-scan", "candidates.csv", quiet=True, cfg=cfg)
    assert capsys.readouterr().err == ""


def test_a_dataset_warns_on_content_not_on_commits(by_spec, cfg, capsys):
    entry = by_spec["acme/sweep-scan:results/table.parquet"]
    old = core.versions(entry)[-1]
    core.get("sweep-scan", "table.parquet", old.sha, cfg=cfg)
    assert "a newer version" in capsys.readouterr().err


def test_the_cli_warns_too(repos, tmp_path, by_spec, capsys):
    conf = tmp_path / "c.toml"
    conf.write_text(f'roots = ["{repos}/acme", "{repos}/other"]\n')
    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    old = core.versions(entry)[-1]
    assert cli.main(["--config", str(conf), "get",
                     f"sweep-scan:candidates.csv@{old.sha}"]) == 0
    captured = capsys.readouterr()
    assert "a newer version" in captured.err
    assert captured.out.strip()                     # stdout is still just the path


def test_fetch_warns_as_well(by_spec, cfg, capsys):
    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    old = core.versions(entry)[-1]
    core.fetch(f"sweep-scan:results/candidates.csv@{old.sha}", cfg=cfg)
    assert "a newer version" in capsys.readouterr().err
