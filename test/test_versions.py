"""Tests for how versions are derived from git history."""

import pytest

from labdata import core


def test_every_file_in_a_repo_carries_the_repo_head(by_spec, repos):
    """One lookup stamps a whole repository, so its files share a version."""
    import subprocess

    head = subprocess.run(
        ["git", "-C", str(repos / "acme" / "sweep-scan"), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    cand = by_spec["acme/sweep-scan:results/candidates.csv"]
    stable = by_spec["acme/sweep-scan:results/stable.csv"]
    assert cand.latest.sha == head
    # stable.csv has not changed since the first commit, but shares the version
    assert stable.latest.sha == head
    assert cand.latest.sha == stable.latest.sha


def test_history_still_shows_when_the_file_itself_changed(by_spec):
    """The catalog stamps HEAD; history is what is worth pinning."""
    cand = by_spec["acme/sweep-scan:results/candidates.csv"]
    stable = by_spec["acme/sweep-scan:results/stable.csv"]
    assert core.versions(cand)[0].subject == "add gene C"
    assert core.versions(stable)[0].subject == "first results"


def test_tags_decorate_but_do_not_define(by_spec):
    assert by_spec["other-org/hic-borders:Results/borders.tsv"].latest.tags == ("v1.0",)
    assert by_spec["acme/sweep-scan:results/candidates.csv"].latest.tags == ()


def test_history_lists_every_version(by_spec):
    hist = core.versions(by_spec["acme/sweep-scan:results/candidates.csv"])
    assert [v.subject for v in hist] == ["add gene C", "add gene B", "first results"]
    assert [v.size for v in hist] == sorted([v.size for v in hist], reverse=True)


def test_find_version_by_sha_prefix_and_tag(by_spec):
    cand = by_spec["acme/sweep-scan:results/candidates.csv"]
    hist = core.versions(cand)
    assert core.find_version(cand, hist[1].sha).subject == "add gene B"
    borders = by_spec["other-org/hic-borders:Results/borders.tsv"]
    assert core.find_version(borders, "v1.0").subject == "borders v1"


def test_unknown_version_lists_the_known_ones(by_spec):
    import pytest

    cand = by_spec["acme/sweep-scan:results/candidates.csv"]
    with pytest.raises(LookupError, match="known:"):
        core.find_version(cand, "nosuchsha")


def test_pinning_an_old_hash_does_not_list_every_version(by_spec, monkeypatch):
    """The catalog holds the current version; a named one is resolved directly."""
    from labdata import core

    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    old = core.versions(entry)[-1]

    def refuse(*args, **kwargs):
        raise AssertionError("find_version must not list the history")

    monkeypatch.setattr(core, "versions", refuse)
    got = core.find_version(entry, old.sha)
    assert got.sha == old.sha
    assert got.subject == "first results"
    assert core.materialize(entry, got).read_text().count("\n") == 2


def test_the_current_version_is_answered_without_touching_git(by_spec, monkeypatch):
    from labdata import core

    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    monkeypatch.setattr(core, "_version_at", lambda *a, **k: pytest.fail("no lookup"))
    assert core.find_version(entry, entry.latest.sha) is entry.latest
    assert core.find_version(entry, "latest") is entry.latest


def test_pinning_a_dataset_version_resolves_directly(by_spec, monkeypatch):
    from labdata import core

    entry = by_spec["acme/sweep-scan:results/table.parquet"]
    old = core.versions(entry)[-1]

    def refuse(*args, **kwargs):
        raise AssertionError("find_version must not list the history")

    monkeypatch.setattr(core, "versions", refuse)
    got = core.find_version(entry, old.sha)
    assert got.parts == 3
    assert core.materialize(entry, got).joinpath("part-1.parquet").read_text() == "row,1\n"


def test_the_version_key_is_a_full_sha_git_understands(by_spec, repos):
    """A spec must identify the version without labdata in hand."""
    import subprocess

    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    version = entry.latest.sha
    assert len(version) == 40
    assert set(version) <= set("0123456789abcdef")
    assert entry.spec.endswith("@" + version)

    # plain git resolves it, with no labdata involved
    got = subprocess.run(
        ["git", "-C", str(repos / "acme" / "sweep-scan"), "cat-file", "-t", version],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert got == "commit"


def test_a_short_prefix_is_still_accepted_as_input(by_spec):
    """Output is full, but a pasted abbreviation still resolves."""
    from labdata import core

    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    assert core.find_version(entry, entry.latest.sha[:7]).sha == entry.latest.sha
