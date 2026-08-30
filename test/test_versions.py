"""Tests for how versions are derived from git history."""

from labdata import core


def test_version_is_last_commit_touching_that_file(by_spec):
    cand = by_spec["acme/sweep-scan:results/candidates.csv"]
    stable = by_spec["acme/sweep-scan:results/stable.csv"]
    assert cand.latest.subject == "add gene C"
    # stable.csv has not changed since the first commit, so its version differs
    assert stable.latest.subject == "first results"
    assert cand.latest.short != stable.latest.short


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
    assert core.find_version(cand, hist[1].sha[:7]).subject == "add gene B"
    borders = by_spec["other-org/hic-borders:Results/borders.tsv"]
    assert core.find_version(borders, "v1.0").subject == "borders v1"


def test_unknown_version_lists_the_known_ones(by_spec):
    import pytest

    cand = by_spec["acme/sweep-scan:results/candidates.csv"]
    with pytest.raises(LookupError, match="known:"):
        core.find_version(cand, "nosuchsha")
