"""Tests for the notebook facing python API."""

import pytest

from crossrepo import core


def test_repo_filename_and_hash_is_silent(by_spec, cfg, capsys):
    e = by_spec["acme/sweep-scan:results/candidates.csv"]
    first = core.versions(e)[-1]
    path = core.get("sweep-scan", "candidates.csv", first.sha, cfg=cfg)
    assert path.read_text().count("\n") == 2      # the first version had two lines
    assert capsys.readouterr().out == ""          # a pinned call says nothing


def test_repo_and_filename_alone_prints_the_hash(cfg, capsys):
    path = core.get("sweep-scan", "candidates.csv", cfg=cfg)
    out = capsys.readouterr().out
    assert path.read_text().count("\n") == 4      # the latest version
    assert "acme/sweep-scan:results/candidates.csv@" in out
    assert 'crossrepo.get("sweep-scan", "candidates.csv", "' in out


def test_the_printed_hash_pins_that_version(cfg, capsys):
    core.get("sweep-scan", "candidates.csv", cfg=cfg)
    sha = capsys.readouterr().out.split("@", 1)[1].split()[0]
    again = core.get("sweep-scan", "candidates.csv", sha, cfg=cfg)
    assert capsys.readouterr().out == ""
    assert "C,3" in again.read_text()


def test_quiet_suppresses_the_note(cfg, capsys):
    core.get("sweep-scan", "candidates.csv", quiet=True, cfg=cfg)
    assert capsys.readouterr().out == ""


def test_owner_qualified_repo_name(cfg, capsys):
    path = core.get("other-org/hic-borders", "borders.tsv", cfg=cfg)
    assert 'crossrepo.get("other-org/hic-borders"' in capsys.readouterr().out
    assert "chrom" in path.read_text()


def test_owner_given_as_a_keyword(cfg, capsys):
    path = core.get("hic-borders", "borders.tsv", owner="other-org", cfg=cfg)
    capsys.readouterr()
    assert "chrom" in path.read_text()


def test_a_tag_works_as_a_version(cfg, capsys):
    path = core.get("hic-borders", "borders.tsv", "v1.0", cfg=cfg)
    assert capsys.readouterr().out == ""
    assert "chrom" in path.read_text()


def test_out_writes_a_copy_and_creates_parents(cfg, tmp_path, capsys):
    dest = tmp_path / "nested" / "dir" / "copy.csv"
    got = core.get("sweep-scan", "candidates.csv", out=dest, cfg=cfg)
    capsys.readouterr()
    assert got == dest
    assert dest.read_text().count("\n") == 4


def test_out_directory_keeps_the_file_name(cfg, tmp_path, capsys):
    into = tmp_path / "into"
    into.mkdir()
    got = core.get("sweep-scan", "candidates.csv", out=into, cfg=cfg)
    capsys.readouterr()
    assert got == into / "candidates.csv"


def test_ambiguous_filename_lists_candidates(cfg):
    with pytest.raises(LookupError, match="ambiguous") as exc:
        core.get("sweep-scan", "stable.csv", cfg=cfg)
    assert "results/sub/stable.csv@" in str(exc.value)


def test_a_path_disambiguates(cfg, capsys):
    path = core.get("sweep-scan", "sub/stable.csv", cfg=cfg)
    assert "results/sub/stable.csv@" in capsys.readouterr().out
    assert path.exists()


def test_unknown_hash_lists_the_known_ones(cfg):
    with pytest.raises(LookupError, match="known:"):
        core.get("sweep-scan", "candidates.csv", "nosuchsha", cfg=cfg)


def test_lfs_without_local_object_stays_actionable(cfg):
    with pytest.raises(FileNotFoundError, match="lfs fetch"):
        core.get("big-thing", "big.h5", cfg=cfg)


def test_out_with_a_trailing_slash_means_a_directory(cfg, tmp_path, capsys):
    got = core.get("sweep-scan", "candidates.csv", out=f"{tmp_path}/fresh/", cfg=cfg)
    capsys.readouterr()
    assert got == tmp_path / "fresh" / "candidates.csv"
    assert got.read_text().count("\n") == 4
