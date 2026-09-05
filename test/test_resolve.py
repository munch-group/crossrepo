"""Tests for turning a spec into one entry and one version."""

import pytest

from crossrepo import core
from crossrepo.model import Spec


def test_spec_round_trips():
    s = Spec.parse("munch-group/primate-ils:results/ils_data.h5@4e6472a")
    assert (s.owner, s.repo, s.path, s.version) == (
        "munch-group", "primate-ils", "results/ils_data.h5", "4e6472a",
    )
    assert str(s) == "munch-group/primate-ils:results/ils_data.h5@4e6472a"


def test_spec_without_owner_or_version():
    s = Spec.parse("humanXsweeps:tmrca_stats.hdf")
    assert s.owner is None and s.version is None


def test_malformed_spec_is_rejected():
    with pytest.raises(ValueError, match="expected"):
        Spec.parse("no-colon-here")


def test_resolve_by_bare_filename(entries):
    e = core.resolve_one(entries, Spec.parse("sweep-scan:candidates.csv"))
    assert e.path == "results/candidates.csv"


def test_missing_repo_error_suggests_near_matches(entries):
    with pytest.raises(LookupError, match="nothing in the catalog"):
        core.resolve_one(entries, Spec.parse("sweep:nope.csv"))


def test_same_name_in_two_subdirs_is_ambiguous(entries):
    # stable.csv exists at results/ and results/sub/ in one repo
    with pytest.raises(LookupError, match="ambiguous") as exc:
        core.resolve_one(entries, Spec.parse("sweep-scan:stable.csv"))
    # the message must list full specs, so one can be copied to disambiguate
    assert "results/sub/stable.csv@" in str(exc.value)


def test_full_path_disambiguates_within_a_repo(entries):
    e = core.resolve_one(entries, Spec.parse("sweep-scan:results/sub/stable.csv"))
    assert e.path == "results/sub/stable.csv"


def test_owner_disambiguates(entries):
    e = core.resolve_one(entries, Spec.parse("other-org/hic-borders:stable.csv"))
    assert e.owner == "other-org"
