"""Tests for discovery and catalog construction."""

from pathlib import Path

import pytest

from crossrepo import gitutil


def test_finds_repos_not_plain_dirs(cfg):
    found = {p.name for p in gitutil.discover_repos(cfg.roots)}
    assert found == {"sweep-scan", "hic-borders", "big-thing", "no-manifest"}


def test_a_repo_is_the_root_itself_or_sits_directly_in_it(tmp_path):
    """Discovery goes one level down, so a root cannot run away into a tree."""
    from fixtures import init

    init(_mk(tmp_path / "roots" / "near"))
    init(_mk(tmp_path / "roots" / "deeper" / "far"))
    found = {p.name for p in gitutil.discover_repos([str(tmp_path / "roots")])}
    assert found == {"near"}                       # `far` is two levels down

    near = tmp_path / "roots" / "near"
    itself = {p.name for p in gitutil.discover_repos([str(near)])}
    assert itself == {"near"}                      # a root that is a repo counts


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
    assert "notes.md" not in names        # not named in the manifest


def test_lfs_pointer_reports_real_size(by_spec):
    big = by_spec["other/big-thing:results/big.h5"]
    assert big.latest.size == 512189753
    assert big.latest.lfs_oid == "de" * 32


def test_non_ascii_filename_is_cataloged(entries):
    """`ls-files -z` and `log --name-only` must agree on a Danish file name."""
    import unicodedata

    names = {unicodedata.normalize("NFC", e.name) for e in entries}
    assert "højde.csv" in names


def test_non_ascii_filename_can_be_fetched(entries):
    import unicodedata

    from crossrepo import core

    entry = next(
        e for e in entries
        if unicodedata.normalize("NFC", e.name) == "højde.csv"
    )
    assert core.materialize(entry, entry.latest).read_text() == "h,1\n"


def test_an_unreadable_directory_is_not_a_repo(tmp_path, monkeypatch):
    """A directory whose provider never answers must not raise out of is_repo."""
    real_exists = Path.exists

    def stalls(self, *args, **kwargs):
        if "OneDrive" in str(self):
            raise TimeoutError(60, "Operation timed out")
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", stalls)
    assert gitutil.is_repo(tmp_path / "OneDrive - Aarhus universitet") is False


def test_discovery_survives_a_directory_that_never_answers(tmp_path, monkeypatch):
    """One unresponsive folder in a root must not end the whole scan."""
    from fixtures import init

    init(_mk(tmp_path / "sweeps"))
    init(_mk(tmp_path / "borders"))
    _mk(tmp_path / "OneDrive - Aarhus universitet" / "Documents")

    real_exists, real_iterdir = Path.exists, Path.iterdir

    def stalls(self, *args, **kwargs):
        if "OneDrive" in str(self):
            raise TimeoutError(60, "Operation timed out")
        return real_exists(self, *args, **kwargs)

    def stalls_listing(self):
        if "OneDrive" in str(self):
            raise TimeoutError(60, "Operation timed out")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "exists", stalls)
    monkeypatch.setattr(Path, "iterdir", stalls_listing)

    found = {p.name for p in gitutil.discover_repos([str(tmp_path)])}
    assert found == {"sweeps", "borders"}


def _mk(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------- roots that cannot be read

def test_a_root_that_is_not_there_is_reported(tmp_path):
    """A misspelled root must not look like a group that published nothing."""
    from crossrepo import core
    from crossrepo.config import Config, SourceWarning

    with pytest.warns(SourceWarning, match="no such directory"):
        assert core.build(Config(roots=[str(tmp_path / "nope")])) == []


def test_a_root_that_cannot_be_listed_is_reported(tmp_path, monkeypatch):
    """A folder whose provider never answers is named, not silently skipped."""
    from crossrepo.config import SourceWarning

    real_is_dir = Path.is_dir

    def stalls(self, *args, **kwargs):
        if "OneDrive" in str(self):
            raise TimeoutError(60, "Operation timed out")
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", stalls)
    with pytest.warns(SourceWarning, match="Operation timed out"):
        assert gitutil.discover_repos([str(tmp_path / "OneDrive")]) == []
