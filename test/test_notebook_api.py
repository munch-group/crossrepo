"""Tests for the python API that mirrors the command line."""

import pytest

import crossrepo


def test_list_leaves_the_version_out_by_default(cfg):
    table = crossrepo.list(cfg=cfg)
    assert "version" not in table.columns
    assert "url" not in table.columns
    assert {"repo", "path", "description", "bytes"} <= set(table.columns)
    assert len(table) > 0


def test_list_version_adds_it_last(cfg):
    table = crossrepo.list(version=True, cfg=cfg)
    assert table.columns[-1] == "version"
    assert all(len(v) == 40 for v in table["version"])


def test_list_url_and_version_are_the_last_two(cfg):
    table = crossrepo.list(version=True, url=True, cfg=cfg)
    assert [*table.columns[-2:]] == ["version", "url"]


def test_list_filters_by_repo_and_pattern(cfg):
    table = crossrepo.list("hic-borders", cfg=cfg)
    assert set(table["repo"]) == {"hic-borders"}
    assert set(table["github"]) == {"other-org/hic-borders"}
    paths = set(crossrepo.list(pattern="*.tsv", cfg=cfg)["path"])
    assert paths == {"Results/borders.tsv"}


def test_an_empty_result_still_has_columns(cfg):
    """Downstream code must not have to special-case nothing matching."""
    table = crossrepo.list(pattern="*.nothing", cfg=cfg)
    assert len(table) == 0
    assert "path" in table.columns
    assert "version" not in table.columns
    assert "version" in crossrepo.list(pattern="*.nothing", version=True, cfg=cfg).columns


def test_repos_summarises_one_row_each(cfg):
    table = crossrepo.repos(cfg=cfg)
    assert [*table.columns] == ["repo", "files", "bytes", "latest"]
    assert "acme/sweep-scan" in set(table["repo"])
    assert (table["files"] > 0).all()


def test_versions_takes_a_repo_and_filename(cfg):
    table = crossrepo.versions("sweep-scan", "candidates.csv", cfg=cfg)
    assert [*table.columns] == ["version", "date", "bytes", "parts", "tags", "subject"]
    assert [*table["subject"]] == ["add gene C", "add gene B", "first results"]


def test_versions_also_takes_an_entry(cfg, by_spec):
    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    assert len(crossrepo.versions(entry, cfg=cfg)) == 3


def test_versions_reports_ambiguity(cfg):
    with pytest.raises(LookupError, match="ambiguous"):
        crossrepo.versions("sweep-scan", "stable.csv", cfg=cfg)


def test_refresh_rebuilds_and_returns_the_catalog(cfg):
    table = crossrepo.refresh(cfg=cfg)
    assert len(table) > 0
    assert "version" not in table.columns
    assert "version" in crossrepo.refresh(version=True, cfg=cfg).columns


def test_parts_is_left_to_frame(cfg):
    """How a dataset is stored is a detail; what it holds is the listing."""
    table = crossrepo.list(cfg=cfg)
    assert "parts" not in table.columns
    full = crossrepo.frame(crossrepo.catalog(cfg=cfg))
    assert "parts" in full.columns
    assert [*full.loc[full["name"] == "table.parquet", "parts"]] == [3]


def test_list_columns_are_in_the_order_they_are_read_in(cfg):
    assert [*crossrepo.list(cfg=cfg).columns] == [
        "owner", "repo", "name", "description", "date", "github", "path",
        "dir", "bytes", "tags", "lfs",
    ]


def test_the_repository_and_the_file_are_each_carried_whole_and_in_halves(cfg):
    table = crossrepo.list(cfg=cfg).set_index("path")
    row = table.loc["results/sub/stable.csv"]
    assert (row["owner"], row["repo"]) == ("acme", "sweep-scan")
    assert row["github"] == "acme/sweep-scan"        # the two halves joined
    assert (row["dir"], row["name"]) == ("results/sub", "stable.csv")


def test_the_owner_is_the_github_account_not_the_directory(cfg):
    """hic-borders sits under acme/ on disk; its remote says otherwise."""
    table = crossrepo.list("hic-borders", cfg=cfg)
    assert set(table["owner"]) == {"other-org"}


def test_a_file_at_the_root_of_a_repo_has_an_empty_dir(tmp_path):
    from fixtures import commit, init
    from crossrepo.config import Config

    repo = init(tmp_path / "flat")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text(
        "files:\n  /top.csv: at the root\n"
    )
    (repo / "top.csv").write_text("a\n")
    commit(repo, "a published file beside the .git")

    table = crossrepo.list(cfg=Config(roots=[str(tmp_path)]))
    assert [*table["path"]] == ["top.csv"]
    assert [*table["dir"]] == [""]
    assert [*table["name"]] == ["top.csv"]


def test_refresh_shows_a_progress_bar_by_default(cfg, monkeypatch):
    """The one call that can take a while should say that it is working."""
    from crossrepo import core

    seen = {}
    real = core.progress_bar

    def spy(items, description, enabled):
        seen[description] = enabled
        return real(items, description, False)      # no bar in the test output

    monkeypatch.setattr(core, "progress_bar", spy)
    crossrepo.refresh(cfg=cfg)
    assert seen.get("scanning clones") is True


def test_refresh_can_be_asked_to_stay_quiet(cfg, monkeypatch):
    from crossrepo import core

    seen = {}
    monkeypatch.setattr(
        core, "progress_bar",
        lambda items, description, enabled: seen.setdefault(description, enabled) or items,
    )
    crossrepo.refresh(progress=False, cfg=cfg)
    assert seen.get("scanning clones") is False


def test_listing_without_refreshing_draws_nothing(cfg, monkeypatch):
    from crossrepo import core

    crossrepo.refresh(progress=False, cfg=cfg)        # populate the stored catalog
    monkeypatch.setattr(
        core, "progress_bar",
        lambda *a, **k: pytest.fail("a stored catalog needs no progress bar"),
    )
    assert len(crossrepo.list(cfg=cfg)) > 0


def test_spec_is_left_out_by_default(cfg):
    """It restates repo, path and version, so it is the widest column there is."""
    assert "spec" not in crossrepo.list(cfg=cfg).columns


def test_spec_can_be_asked_for_and_round_trips(cfg):
    table = crossrepo.list(spec=True, cfg=cfg)
    assert table.columns[-1] == "spec"
    one = table.loc[table["path"] == "results/candidates.csv", "spec"].iloc[0]
    assert crossrepo.fetch(one, cfg=cfg).read_text().count("\n") == 4


def test_the_optional_columns_keep_their_order(cfg):
    table = crossrepo.list(version=True, spec=True, url=True, cfg=cfg)
    assert [*table.columns[-3:]] == ["version", "spec", "url"]


def test_refresh_forwards_spec_too(cfg):
    assert "spec" in crossrepo.refresh(spec=True, progress=False, cfg=cfg).columns


def test_brief_shows_only_what_a_file_is(cfg):
    table = crossrepo.list(brief=True, cfg=cfg)
    assert [*table.columns] == [
        "owner", "repo", "name", "size", "description", "date",
    ]
    assert len(table) == len(crossrepo.list(cfg=cfg))     # the same rows


def test_brief_writes_the_size_for_reading(cfg):
    """`size` is `bytes` in megabytes of a million, as results are talked about."""
    table = crossrepo.list(brief=True, cfg=cfg).set_index("name")
    assert table.loc["big.h5", "size"] == "512.2 MB"        # 512189753 bytes
    assert table.loc["borders.tsv", "size"] == "0.0 MB"     # small, and says so
    assert "bytes" not in table.columns


def test_a_size_over_a_billion_bytes_is_gigabytes():
    assert crossrepo._size(2_400_000_000) == "2.4 GB"
    assert crossrepo._size(999_000_000) == "999.0 MB"
    assert crossrepo._size(-1) == "?"                         # unknown, not -0.0 MB


def test_brief_still_takes_the_optional_columns(cfg):
    table = crossrepo.list(brief=True, version=True, url=True, cfg=cfg)
    assert [*table.columns[-2:]] == ["version", "url"]
    assert table.columns[0] == "owner"


def test_brief_of_nothing_still_has_its_columns(cfg):
    table = crossrepo.list(brief=True, pattern="*.nothing", cfg=cfg)
    assert len(table) == 0
    assert [*table.columns] == [*crossrepo.BRIEF_COLUMNS]


# -------------------------------------------------- the table it hands back

def aligned(table):
    """The column numbers the rendered table left aligns."""
    import re

    html = table._repr_html_()
    head = html[: html.index("</style>")]
    return sorted(int(n) for n in re.findall(r"\.col(\d+)\s*\{\s*text-align: left", head))


def text_columns(table):
    """The column numbers holding text, which are the ones that should be."""
    import pandas as pd

    return [
        i for i, dtype in enumerate(table.dtypes)
        if pd.api.types.is_object_dtype(dtype)
    ]


def test_a_listing_is_still_a_data_frame(cfg):
    """Everything that takes a data frame has to keep taking this."""
    import pandas as pd

    assert isinstance(crossrepo.list(cfg=cfg), pd.DataFrame)


def test_a_listing_left_aligns_the_columns_that_hold_text(cfg):
    table = crossrepo.list(cfg=cfg)
    assert aligned(table) == text_columns(table)
    assert aligned(table)                        # and there are some


def test_a_number_column_is_left_where_it_was(cfg):
    """Right aligned is right for numbers; this is only about text."""
    table = crossrepo.list(cfg=cfg)
    assert [*table.columns].index("bytes") not in aligned(table)


def test_the_alignment_survives_being_worked_on(cfg):
    """A sort or a column selection must not cost the styling."""
    table = crossrepo.list(cfg=cfg)
    assert aligned(table.sort_values("bytes")) == text_columns(table)
    narrowed = table[["repo", "description", "bytes"]]
    assert aligned(narrowed) == text_columns(narrowed)


def test_one_column_of_it_is_an_ordinary_series(cfg):
    import pandas as pd

    assert type(crossrepo.list(cfg=cfg)["repo"]) is pd.Series


def test_the_history_and_the_summary_are_tables_too(cfg, by_spec):
    entry = next(iter(by_spec.values()))
    assert aligned(crossrepo.versions(entry, cfg=cfg)) is not None
    assert type(crossrepo.repos(cfg=cfg)) is type(crossrepo.list(cfg=cfg))


def test_a_long_table_is_cut_off_where_a_data_frame_would_be():
    """A styler draws every row it is given; a catalog can be long."""
    import pandas as pd

    table = crossrepo.Table({"text": [f"row {i}" for i in range(500)]})
    with pd.option_context("display.max_rows", 10):
        assert table._repr_html_().count("<tr>") <= 12       # header and a few
    with pd.option_context("display.max_rows", None):
        assert table._repr_html_().count("<tr>") == 501      # asked for all of it


def test_a_table_of_nothing_but_numbers_is_left_alone():
    assert aligned(crossrepo.Table({"x": [1, 2], "y": [3.5, 4.5]})) == []


def test_an_empty_table_still_renders(cfg):
    assert "<table" in crossrepo.list(pattern="*.nothing", cfg=cfg)._repr_html_()


def test_anything_can_be_wrapped_in_one():
    import pandas as pd

    plain = pd.DataFrame({"note": ["a", "b"], "n": [1, 2]})
    assert aligned(crossrepo.Table(plain)) == [0]


def test_the_module_still_says_no_to_what_it_has_not_got():
    """The lazy `Table` is served by a module __getattr__, which must not eat this."""
    with pytest.raises(AttributeError, match="no attribute"):
        crossrepo.no_such_thing
