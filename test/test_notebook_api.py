"""Tests for the python API that mirrors the command line."""

import pytest

import labdata


def test_list_leaves_the_version_out_by_default(cfg):
    table = labdata.list(cfg=cfg)
    assert "version" not in table.columns
    assert "url" not in table.columns
    assert {"repo", "path", "description", "bytes"} <= set(table.columns)
    assert len(table) > 0


def test_list_version_adds_it_last(cfg):
    table = labdata.list(version=True, cfg=cfg)
    assert table.columns[-1] == "version"
    assert all(len(v) == 40 for v in table["version"])


def test_list_url_and_version_are_the_last_two(cfg):
    table = labdata.list(version=True, url=True, cfg=cfg)
    assert [*table.columns[-2:]] == ["version", "url"]


def test_list_filters_by_repo_and_pattern(cfg):
    table = labdata.list("hic-borders", cfg=cfg)
    assert set(table["repo"]) == {"hic-borders"}
    assert set(table["github"]) == {"other-org/hic-borders"}
    paths = set(labdata.list(pattern="*.tsv", cfg=cfg)["path"])
    assert paths == {"Results/borders.tsv"}


def test_an_empty_result_still_has_columns(cfg):
    """Downstream code must not have to special-case nothing matching."""
    table = labdata.list(pattern="*.nothing", cfg=cfg)
    assert len(table) == 0
    assert "path" in table.columns
    assert "version" not in table.columns
    assert "version" in labdata.list(pattern="*.nothing", version=True, cfg=cfg).columns


def test_repos_summarises_one_row_each(cfg):
    table = labdata.repos(cfg=cfg)
    assert [*table.columns] == ["repo", "files", "bytes", "latest"]
    assert "acme/sweep-scan" in set(table["repo"])
    assert (table["files"] > 0).all()


def test_versions_takes_a_repo_and_filename(cfg):
    table = labdata.versions("sweep-scan", "candidates.csv", cfg=cfg)
    assert [*table.columns] == ["version", "date", "bytes", "parts", "tags", "subject"]
    assert [*table["subject"]] == ["add gene C", "add gene B", "first results"]


def test_versions_also_takes_an_entry(cfg, by_spec):
    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    assert len(labdata.versions(entry, cfg=cfg)) == 3


def test_versions_reports_ambiguity(cfg):
    with pytest.raises(LookupError, match="ambiguous"):
        labdata.versions("sweep-scan", "stable.csv", cfg=cfg)


def test_refresh_rebuilds_and_returns_the_catalog(cfg):
    table = labdata.refresh(cfg=cfg)
    assert len(table) > 0
    assert "version" not in table.columns
    assert "version" in labdata.refresh(version=True, cfg=cfg).columns


def test_parts_is_left_to_frame(cfg):
    """How a dataset is stored is a detail; what it holds is the listing."""
    table = labdata.list(cfg=cfg)
    assert "parts" not in table.columns
    full = labdata.frame(labdata.catalog(cfg=cfg))
    assert "parts" in full.columns
    assert [*full.loc[full["name"] == "table.parquet", "parts"]] == [3]


def test_list_columns_are_in_the_order_they_are_read_in(cfg):
    assert [*labdata.list(cfg=cfg).columns] == [
        "owner", "repo", "name", "description", "date", "github", "path",
        "dir", "bytes", "tags", "lfs",
    ]


def test_the_repository_and_the_file_are_each_carried_whole_and_in_halves(cfg):
    table = labdata.list(cfg=cfg).set_index("path")
    row = table.loc["results/sub/stable.csv"]
    assert (row["owner"], row["repo"]) == ("acme", "sweep-scan")
    assert row["github"] == "acme/sweep-scan"        # the two halves joined
    assert (row["dir"], row["name"]) == ("results/sub", "stable.csv")


def test_the_owner_is_the_github_account_not_the_directory(cfg):
    """hic-borders sits under acme/ on disk; its remote says otherwise."""
    table = labdata.list("hic-borders", cfg=cfg)
    assert set(table["owner"]) == {"other-org"}


def test_a_file_at_the_root_of_a_repo_has_an_empty_dir(tmp_path):
    from fixtures import commit, init
    from labdata.config import Config

    repo = init(tmp_path / "flat")
    (repo / "results").mkdir()
    (repo / "results" / "labdata.yml").write_text(
        "files:\n  /top.csv: at the root\n"
    )
    (repo / "top.csv").write_text("a\n")
    commit(repo, "a published file beside the .git")

    table = labdata.list(cfg=Config(roots=[str(tmp_path)]))
    assert [*table["path"]] == ["top.csv"]
    assert [*table["dir"]] == [""]
    assert [*table["name"]] == ["top.csv"]


def test_refresh_shows_a_progress_bar_by_default(cfg, monkeypatch):
    """The one call that can take a while should say that it is working."""
    from labdata import core

    seen = {}
    real = core.progress_bar

    def spy(items, description, enabled):
        seen[description] = enabled
        return real(items, description, False)      # no bar in the test output

    monkeypatch.setattr(core, "progress_bar", spy)
    labdata.refresh(cfg=cfg)
    assert seen.get("scanning clones") is True


def test_refresh_can_be_asked_to_stay_quiet(cfg, monkeypatch):
    from labdata import core

    seen = {}
    monkeypatch.setattr(
        core, "progress_bar",
        lambda items, description, enabled: seen.setdefault(description, enabled) or items,
    )
    labdata.refresh(progress=False, cfg=cfg)
    assert seen.get("scanning clones") is False


def test_listing_without_refreshing_draws_nothing(cfg, monkeypatch):
    from labdata import core

    labdata.refresh(progress=False, cfg=cfg)        # populate the stored catalog
    monkeypatch.setattr(
        core, "progress_bar",
        lambda *a, **k: pytest.fail("a stored catalog needs no progress bar"),
    )
    assert len(labdata.list(cfg=cfg)) > 0


def test_spec_is_left_out_by_default(cfg):
    """It restates repo, path and version, so it is the widest column there is."""
    assert "spec" not in labdata.list(cfg=cfg).columns


def test_spec_can_be_asked_for_and_round_trips(cfg):
    table = labdata.list(spec=True, cfg=cfg)
    assert table.columns[-1] == "spec"
    one = table.loc[table["path"] == "results/candidates.csv", "spec"].iloc[0]
    assert labdata.fetch(one, cfg=cfg).read_text().count("\n") == 4


def test_the_optional_columns_keep_their_order(cfg):
    table = labdata.list(version=True, spec=True, url=True, cfg=cfg)
    assert [*table.columns[-3:]] == ["version", "spec", "url"]


def test_refresh_forwards_spec_too(cfg):
    assert "spec" in labdata.refresh(spec=True, progress=False, cfg=cfg).columns


def test_brief_shows_only_what_a_file_is(cfg):
    table = labdata.list(brief=True, cfg=cfg)
    assert [*table.columns] == [
        "owner", "repo", "name", "size", "description", "date",
    ]
    assert len(table) == len(labdata.list(cfg=cfg))     # the same rows


def test_brief_writes_the_size_for_reading(cfg):
    """`size` is `bytes` in megabytes of a million, as results are talked about."""
    table = labdata.list(brief=True, cfg=cfg).set_index("name")
    assert table.loc["big.h5", "size"] == "512.2 MB"        # 512189753 bytes
    assert table.loc["borders.tsv", "size"] == "0.0 MB"     # small, and says so
    assert "bytes" not in table.columns


def test_a_size_over_a_billion_bytes_is_gigabytes():
    assert labdata._size(2_400_000_000) == "2.4 GB"
    assert labdata._size(999_000_000) == "999.0 MB"
    assert labdata._size(-1) == "?"                         # unknown, not -0.0 MB


def test_brief_still_takes_the_optional_columns(cfg):
    table = labdata.list(brief=True, version=True, url=True, cfg=cfg)
    assert [*table.columns[-2:]] == ["version", "url"]
    assert table.columns[0] == "owner"


def test_brief_of_nothing_still_has_its_columns(cfg):
    table = labdata.list(brief=True, pattern="*.nothing", cfg=cfg)
    assert len(table) == 0
    assert [*table.columns] == [*labdata.BRIEF_COLUMNS]
