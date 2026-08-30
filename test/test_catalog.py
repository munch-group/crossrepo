"""Tests for discovery and catalog construction."""

from labdata import gitutil


def test_finds_repos_not_plain_dirs(cfg):
    found = {p.name for p in gitutil.discover_repos(cfg.roots, cfg.depth)}
    assert found == {"sweep-scan", "hic-borders", "big-thing"}


def test_owner_comes_from_remote_not_directory(by_spec):
    # hic-borders sits under acme/ on disk but its remote says other-org
    assert "other-org/hic-borders:Results/borders.tsv" in by_spec
    assert "acme/sweep-scan:results/candidates.csv" in by_spec


def test_owner_falls_back_to_parent_dir_without_remote(entries):
    assert any(e.repo_key == "other/big-thing" for e in entries)


def test_case_insensitive_results_dir(entries):
    assert any(e.path.startswith("Results/") for e in entries)


def test_excludes_untracked_and_outside_and_patterns(entries):
    names = {e.name for e in entries}
    assert "untracked.csv" not in names   # never committed
    assert "scratch.csv" not in names     # outside results/
    assert "notes.md" not in names        # excluded pattern


def test_lfs_pointer_reports_real_size(by_spec):
    big = by_spec["other/big-thing:results/big.h5"]
    assert big.latest.size == 512189753
    assert big.latest.lfs_oid == "de" * 32
