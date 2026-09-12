"""Tests for rescanning one owner or one repository instead of all of them."""

import json

import pytest

import crossrepo
from crossrepo import core, remote
from crossrepo.config import Config, SourceWarning

from test_remote import FakeClient, commit


# ---------------------------------------------------------------- what is chosen

def test_the_rule_is_the_one_a_listing_filters_by():
    assert core.selects(None, "munch-group/x-gwas")          # everything, by default
    assert core.selects("x-gwas", "munch-group/x-gwas")
    assert core.selects("X-GWAS", "munch-group/x-gwas")      # ignoring case
    assert core.selects("gwas", "munch-group/x-gwas")        # a fragment
    assert core.selects("munch-group/", "munch-group/x-gwas")
    assert core.selects("munch-group/x-gwas", "munch-group/x-gwas")
    assert not core.selects("y-gwas", "munch-group/x-gwas")
    assert not core.selects("munch-group/", "other-org/x-gwas")


def test_an_owner_and_a_repo_become_one_text():
    assert crossrepo._select() is None
    assert crossrepo._select(repo="x-gwas") == "x-gwas"
    assert crossrepo._select(owner="munch-group") == "munch-group/"
    assert crossrepo._select("munch-group", "x-gwas") == "munch-group/x-gwas"


# ------------------------------------------------------------------ local clones

def test_only_the_named_repository_is_scanned(cfg, entries):
    some = core.build(cfg, select="sweep-scan")
    assert {e.repo_key for e in some} == {"acme/sweep-scan"}
    assert some == [e for e in entries if e.repo_key == "acme/sweep-scan"]


def test_only_the_named_owner_is_scanned(cfg, entries):
    some = core.build(cfg, select="other-org/")
    assert {e.repo_key for e in some} == {"other-org/hic-borders"}
    assert some == [e for e in entries if e.owner == "other-org"]


def test_naming_nothing_scans_everything(cfg, entries):
    assert core.build(cfg, select=None) == entries


def test_a_name_that_matches_nothing_scans_nothing(cfg):
    assert core.build(cfg, select="no-such-repo") == []


# ----------------------------------------------------------- the stored catalog

def test_the_rest_of_the_catalog_is_kept(cfg, entries):
    core.save(entries, cfg)
    merged = core.catalog(refresh=True, cfg=cfg, select="sweep-scan")
    assert merged == entries                          # nothing lost, nothing added
    stored = json.loads(core._cache_file().read_text())
    assert len(stored["entries"]) == len(entries)
    assert stored["config"] == core.fingerprint(cfg)  # and still keyed to it all


def test_the_named_repository_is_the_one_brought_up_to_date(cfg, entries):
    stale = [
        e for e in entries
        if e.repo_key != "acme/sweep-scan" or e.name != "candidates.csv"
    ]
    core.save(stale, cfg)
    merged = core.catalog(refresh=True, cfg=cfg, select="sweep-scan")
    assert merged == entries                          # the missing file is back

    core.save(stale, cfg)
    merged = core.catalog(refresh=True, cfg=cfg, select="hic-borders")
    assert merged == stale                            # a rescan elsewhere changes nothing


def test_a_file_that_is_gone_goes(cfg, entries):
    extra = core.replace(entries[0], path="results/removed.csv")
    core.save([extra, *entries], cfg)
    merged = core.catalog(refresh=True, cfg=cfg, select=extra.repo_key)
    assert merged == entries


def test_with_no_catalog_to_update_nothing_is_stored(cfg):
    core._cache_file().unlink(missing_ok=True)
    with pytest.warns(SourceWarning, match="no catalog to update"):
        found = core.catalog(refresh=True, cfg=cfg, select="sweep-scan")
    assert {e.repo_key for e in found} == {"acme/sweep-scan"}
    assert not core._cache_file().exists()            # not a catalog of one repo


def test_the_stored_catalog_is_still_read_whole(cfg, entries):
    core.save(entries, cfg)
    assert core.catalog(cfg=cfg, select="sweep-scan") == entries


# ------------------------------------------------------------------ over the API

def canned(missing=()):
    """A GitHub client holding one publishing repository per owner."""
    return FakeClient(
        tree=[
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 40},
            {"path": "results/hits.csv", "type": "blob", "sha": "aaa", "size": 10},
        ],
        commits=[commit("a" * 40, "add hits")],
        blobs={"man": b"files:\n  hits.csv: Association hits\n"},
        missing=missing,
    )


def test_an_owner_that_cannot_match_is_not_even_listed():
    client = canned()
    cfg = Config(owners=["munch-group", "other-org"])
    remote.build(cfg, client, select="munch-group/")
    assert client.calls.count("repositories") == 1


def test_every_owner_is_listed_when_only_a_repo_is_named():
    client = canned()
    cfg = Config(owners=["munch-group", "other-org"])
    remote.build(cfg, client, select="demo")
    assert client.calls.count("repositories") == 2    # a name can be under either


def test_a_repository_that_does_not_match_is_not_read():
    client = canned()
    cfg = Config(owners=["munch-group"])
    assert remote.build(cfg, client, select="no-such-repo") == []
    assert client.calls == ["repositories"]           # listed, then nothing


def test_a_named_repository_that_does_not_match_is_not_asked_after():
    client = canned(missing=["munch-group/gone"])
    cfg = Config(repos=["munch-group/gone", "munch-group/demo"])
    entries = remote.build(cfg, client, select="demo")
    assert [e.repo_key for e in entries] == ["munch-group/demo"]
    assert "repos/munch-group/gone" not in client.calls


def test_naming_nothing_reads_everything():
    client = canned()
    cfg = Config(owners=["munch-group", "other-org"])
    assert len(remote.build(cfg, client)) == 2


# ------------------------------------------------------------- the notebook call

def test_refresh_hands_back_nothing_to_look_at(cfg, entries):
    """Rescanning and looking are two calls; this is the first of them."""
    core.save(entries, cfg)
    assert crossrepo.refresh(cfg=cfg, progress=False) is None


def test_refresh_stores_what_it_rescanned(cfg, entries):
    """What a named rescan did is read from the catalog it left behind."""
    core.save(entries, cfg)
    crossrepo.refresh(cfg=cfg, progress=False, repo="sweep-scan")
    stored = core.catalog(cfg=cfg)
    assert {e.repo for e in stored if e.repo == "sweep-scan"} == {"sweep-scan"}
    assert len(stored) == len(entries)          # and the rest is still there


def test_refresh_by_owner_leaves_the_other_owners_alone(cfg, entries):
    core.save(entries, cfg)
    crossrepo.refresh(cfg=cfg, progress=False, owner="other-org")
    assert {e.owner for e in core.catalog(cfg=cfg)} == {e.owner for e in entries}


def test_refresh_no_longer_takes_the_listing_options(cfg, entries):
    """There is no table to put them on, so they are refused rather than ignored."""
    core.save(entries, cfg)
    with pytest.raises(TypeError):
        crossrepo.refresh(cfg=cfg, progress=False, brief=True)


def test_refresh_without_a_name_still_rebuilds_everything(cfg, entries):
    core._cache_file().unlink(missing_ok=True)
    crossrepo.refresh(cfg=cfg, progress=False)
    assert len(core.catalog(cfg=cfg)) == len(entries)


def test_a_name_that_matches_nothing_says_so(cfg, entries):
    core.save(entries, cfg)
    with pytest.warns(SourceWarning, match="nothing matching 'no-such-repo'"):
        crossrepo.refresh(cfg=cfg, progress=False, repo="no-such-repo")
    assert len(core.catalog(cfg=cfg)) == len(entries)   # and the catalog is intact
