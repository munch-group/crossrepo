"""Tests for fuzzy ranking of catalog entries."""

import pytest

from crossrepo.core import rank


def paths(entries):
    return [e.path for e in entries]


def test_an_exact_word_ranks_a_name_above_a_description(entries):
    assert paths(rank(entries, "table"))[0] == "results/table.parquet"


def test_every_word_must_match(entries):
    assert paths(rank(entries, "tad borders")) == ["Results/borders.tsv"]
    assert rank(entries, "tad zzzz") == []


def test_a_typo_is_forgiven(entries):
    assert "Results/borders.tsv" in paths(rank(entries, "bordrs"))


def test_an_abbreviation_of_a_repo_matches(entries):
    found = rank(entries, "hicbord")
    assert found and {e.repo for e in found} == {"hic-borders"}


def test_case_does_not_matter(entries):
    assert paths(rank(entries, "TAD")) == paths(rank(entries, "tad"))


def test_short_words_are_not_guessed_at(entries):
    """Two letters match only as written, or nearly anything would."""
    assert rank(entries, "qx") == []


def test_an_empty_query_is_refused(entries):
    with pytest.raises(ValueError):
        rank(entries, "   ")
