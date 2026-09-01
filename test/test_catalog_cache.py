"""Tests for the catalog stored on disk between runs."""

from pathlib import Path

import pytest

from labdata import core
from labdata.config import Config


def test_a_truncated_catalog_is_rebuilt_rather_than_fatal(cfg):
    """An interrupted write must cost a rescan, not every later command."""
    core.save(core.build(cfg), cfg)
    p = core._cache_file()
    text = p.read_text(encoding="utf-8")
    p.write_text(text[: len(text) // 2], encoding="utf-8")

    assert core.load_cached(cfg=cfg) is None
    assert core.catalog(cfg=cfg)          # rebuilds instead of raising


def test_a_catalog_that_is_not_json_at_all_is_ignored(cfg):
    core.save(core.build(cfg), cfg)
    core._cache_file().write_text("", encoding="utf-8")
    assert core.load_cached(cfg=cfg) is None


def test_saving_leaves_no_temporary_file_behind(cfg):
    p = core.save(core.build(cfg), cfg)
    assert p.exists()
    assert not list(p.parent.glob("catalog.json.*"))


def test_a_catalog_built_with_other_settings_is_not_reused(cfg, tmp_path):
    assert core.catalog(cfg=cfg)
    (tmp_path / "empty").mkdir(exist_ok=True)          # a real, empty root
    elsewhere = Config(roots=[str(tmp_path / "empty")])
    assert core.catalog(cfg=elsewhere) == []     # not the stored entries
    assert core.catalog(cfg=cfg)                 # and the first settings still work


def test_fingerprint_tracks_what_decides_the_catalog(tmp_path):
    base = Config(roots=[str(tmp_path)])
    assert core.fingerprint(base) == core.fingerprint(Config(roots=[str(tmp_path)]))
    assert core.fingerprint(base) != core.fingerprint(
        Config(roots=[str(tmp_path)], owners=["munch-group"])
    )
    assert core.fingerprint(base) != core.fingerprint(
        Config(roots=[str(tmp_path)], include=["*.csv"])
    )
    assert core.fingerprint(base) != core.fingerprint(
        Config(roots=[str(tmp_path)], labdata_dirs=["out"])
    )


def test_fingerprint_ignores_how_a_root_is_spelled():
    assert core.fingerprint(Config(roots=["~/somewhere"])) == core.fingerprint(
        Config(roots=[str(Path.home() / "somewhere")])
    )


def test_load_cached_without_settings_accepts_any_catalog(cfg):
    core.save(core.build(cfg), cfg)
    assert core.load_cached() is not None


def test_nothing_is_scanned_until_roots_are_configured():
    """The default must not reach into a whole home directory."""
    assert Config().roots == []
    assert core.build(Config()) == []


def test_an_initialised_config_file_explains_what_roots_needs(tmp_path):
    written = Config().write_default(tmp_path / "config.toml")
    text = written.read_text(encoding="utf-8")
    assert "roots = []" in text
    assert '#   roots = ["~/projects"' in text     # a worked example to copy
    assert "user@host:path" in text                # including a root on a server
    assert Config.load(written).roots == []


def test_a_retired_setting_is_ignored_rather_than_refused(tmp_path):
    """A config file written before `depth` went away must still load."""
    old = tmp_path / "old.toml"
    old.write_text('roots = ["~/projects"]\ndepth = 3\n')
    with pytest.warns(UserWarning, match="depth"):
        cfg = Config.load(old)
    assert cfg.roots == ["~/projects"]
    assert not hasattr(cfg, "depth")


def test_a_renamed_setting_is_read_under_its_old_name(tmp_path):
    """`results_dirs` still says what was meant, so it is honoured."""
    old = tmp_path / "old.toml"
    old.write_text('results_dirs = ["results"]\n')
    with pytest.warns(UserWarning, match="`results_dirs` is now `labdata_dirs`"):
        assert Config.load(old).labdata_dirs == ["results"]


def test_a_key_that_was_never_a_setting_is_still_refused(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("nosuchsetting = 1\n")
    with pytest.raises(ValueError, match="unknown keys"):
        Config.load(bad)


def test_progress_bar_leaves_the_work_unchanged(cfg):
    """The bar must be a wrapper, never a filter."""
    plain = core.build(cfg)
    barred = core.build(cfg, progress=True)
    assert [e.spec for e in plain] == [e.spec for e in barred]


def test_progress_bar_passes_through_when_switched_off():
    items = [1, 2, 3]
    assert core.progress_bar(items, "x", False) is items
    assert core.progress_bar([], "x", True) == []      # nothing to show
