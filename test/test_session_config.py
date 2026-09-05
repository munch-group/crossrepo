"""Tests for registering settings for a session instead of passing them."""

import pytest

import labdata
from labdata import config as config_mod
from labdata.config import Config, active_config, use_config
from labdata.core import build


@pytest.fixture(autouse=True)
def unregistered():
    """Leave nothing registered behind, whatever a test does."""
    yield
    config_mod._registered = None


@pytest.fixture
def from_file(monkeypatch):
    """A stand-in for the configuration file, so no real one is read."""
    stored = Config(owners=["munch-group"], labdata_dirs=["results", "data"])
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, path=None: stored))
    return stored


def test_the_file_is_used_while_nothing_is_registered(from_file):
    assert active_config() is from_file


def test_registering_makes_it_the_active_one(from_file):
    cfg = Config(repos=["munch-group/x-gwas"])
    use_config(cfg)
    assert active_config() is cfg


def test_none_unregisters(from_file):
    use_config(Config(repos=["munch-group/x-gwas"]))
    use_config(None)
    assert active_config() is from_file


def test_a_block_puts_back_the_file(from_file):
    cfg = Config(repos=["munch-group/x-gwas"])
    with use_config(cfg) as active:
        assert active is cfg
        assert active_config() is cfg
    assert active_config() is from_file


def test_a_block_puts_back_what_was_registered(from_file):
    first = Config(repos=["munch-group/x-gwas"])
    second = Config(repos=["munch-group/y-gwas"])
    use_config(first)
    with use_config(second):
        assert active_config() is second
    assert active_config() is first


def test_a_block_puts_the_previous_back_after_an_error(from_file):
    with pytest.raises(RuntimeError):
        with use_config(Config(repos=["munch-group/x-gwas"])):
            raise RuntimeError("boom")
    assert active_config() is from_file


def test_overrides_layer_on_the_file(from_file):
    use_config(repos=["munch-group/x-gwas"])
    active = active_config()
    assert active.repos == ["munch-group/x-gwas"]
    assert active.owners == ["munch-group"]                 # kept from the file
    assert active.labdata_dirs == ["results", "data"]       # kept from the file
    assert from_file.repos == []                            # and the file is untouched


def test_overrides_layer_on_a_given_config(from_file):
    cfg = Config(roots=["~/projects"], owners=["other-org"])
    use_config(cfg, owners=["munch-group"])
    active = active_config()
    assert active.roots == ["~/projects"]
    assert active.owners == ["munch-group"]
    assert cfg.owners == ["other-org"]                      # and cfg is untouched


def test_an_override_that_is_not_a_setting_is_refused(from_file):
    with pytest.raises(TypeError, match="repo is not a setting"):
        use_config(repo=["munch-group/x-gwas"])
    assert active_config() is from_file


def test_something_that_is_not_a_config_is_refused(from_file):
    with pytest.raises(TypeError, match="cfg should be a Config, not str"):
        use_config("~/projects")
    assert active_config() is from_file


def test_undoing_by_hand_is_not_undone_again_on_the_way_out(from_file):
    first = Config(repos=["munch-group/x-gwas"])
    later = Config(repos=["munch-group/z-gwas"])
    use_config(first)
    registration = use_config(Config(repos=["munch-group/y-gwas"]))
    with registration:
        registration.undo()
        assert active_config() is first
        use_config(later)
    assert active_config() is later


def test_it_shows_the_settings_it_put_in_effect(from_file):
    cfg = Config(repos=["munch-group/x-gwas"])
    assert repr(use_config(cfg)) == repr(cfg)
    assert "munch-group/x-gwas" in repr(use_config(repos=["munch-group/x-gwas"]))


def test_unregistering_shows_what_the_file_gives(from_file):
    use_config(Config(repos=["munch-group/x-gwas"]))
    assert repr(use_config(None)) == repr(from_file)


def test_it_shows_the_settings_of_the_moment_it_is_asked(from_file):
    cfg = Config(repos=["munch-group/x-gwas"])
    registration = use_config(cfg)
    assert repr(registration) == repr(cfg)
    registration.undo()
    assert repr(registration) == repr(from_file)      # the settings, not the history


def test_a_notebook_cell_echoes_the_settings(from_file):
    pretty = pytest.importorskip("IPython.lib.pretty").pretty
    cfg = Config(repos=["munch-group/x-gwas"])
    registration = use_config(cfg)
    assert pretty(registration) == repr(cfg)
    assert "object at 0x" not in pretty(registration)


def test_a_registered_config_is_what_a_scan_reads(cfg, entries):
    use_config(cfg)
    assert build() == entries


def test_an_explicit_config_still_wins(cfg, entries):
    use_config(Config(roots=[]))
    assert build() == []
    assert build(cfg) == entries


def test_it_is_reachable_from_the_package(cfg):
    assert labdata.use_config is use_config
    assert labdata.active_config is active_config
    labdata.use_config(cfg)
    assert labdata.active_config() is cfg
