"""Tests for the python API that mirrors the command line."""

import pytest

import crossrepo


def test_a_listing_is_four_columns(cfg):
    """What a file is chosen on, and nothing else; the rest is `frame`."""
    table = crossrepo.list(cfg=cfg)
    assert [*table.columns] == ["repo", "path", "get", "description"]
    assert len(table) > 0


def test_the_listing_names_the_owner_with_the_repo(cfg):
    """A listing is what a spec is copied out of, and a spec names the owner."""
    assert all("/" in name for name in crossrepo.list(cfg=cfg)["repo"])


def test_the_listing_refuses_the_options_it_used_to_take(cfg):
    """They shaped columns the listing no longer has."""
    for gone in ("brief", "version", "spec", "url"):
        with pytest.raises(TypeError):
            crossrepo.list(cfg=cfg, **{gone: True})


def test_list_filters_by_repo_and_pattern(cfg):
    table = crossrepo.list("hic-borders", cfg=cfg)
    assert set(table["repo"]) == {"other-org/hic-borders"}
    paths = set(crossrepo.list(pattern="*.tsv", cfg=cfg)["path"])
    assert paths == {"Results/borders.tsv"}


def test_an_empty_result_still_has_columns(cfg):
    """Downstream code must not have to special-case nothing matching."""
    table = crossrepo.list(pattern="*.nothing", cfg=cfg)
    assert len(table) == 0
    assert [*table.columns] == [*crossrepo.LIST_COLUMNS]


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


def test_refresh_rebuilds_the_catalog_and_shows_nothing(cfg):
    """A cell that rescans should not answer with the whole catalog."""
    assert crossrepo.refresh(cfg=cfg) is None
    assert len(crossrepo.list(cfg=cfg)) > 0            # it did rebuild it


def test_parts_is_left_to_frame(cfg):
    """How a dataset is stored is a detail; what it holds is the listing."""
    table = crossrepo.list(cfg=cfg)
    assert "parts" not in table.columns
    full = crossrepo.frame(crossrepo.catalog(cfg=cfg))
    assert "parts" in full.columns
    assert [*full.loc[full["name"] == "table.parquet", "parts"]] == [3]


def test_frame_columns_are_in_the_order_they_are_read_in(cfg):
    assert [*crossrepo.frame(crossrepo.catalog(cfg=cfg)).columns] == [
        "owner", "repo", "name", "description", "get", "date", "github", "path",
        "dir", "bytes", "tags", "lfs", "version", "parts", "spec", "url",
    ]


def test_the_repository_and_the_file_are_each_carried_whole_and_in_halves(cfg):
    table = crossrepo.frame(crossrepo.catalog(cfg=cfg)).set_index("path")
    row = table.loc["results/sub/stable.csv"]
    assert (row["owner"], row["repo"]) == ("acme", "sweep-scan")
    assert row["github"] == "acme/sweep-scan"        # the two halves joined
    assert (row["dir"], row["name"]) == ("results/sub", "stable.csv")


def test_the_owner_is_the_github_account_not_the_directory(cfg):
    """hic-borders sits under acme/ on disk; its remote says otherwise."""
    table = crossrepo.frame(crossrepo.catalog(cfg=cfg))
    assert set(table[table["repo"] == "hic-borders"]["owner"]) == {"other-org"}


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

    cfg = Config(roots=[str(tmp_path)])
    table = crossrepo.frame(crossrepo.catalog(refresh=True, cfg=cfg))
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


def bars(monkeypatch):
    """Record which progress bars were asked for, without drawing any."""
    from crossrepo import core

    seen = {}
    real = core.progress_bar

    def spy(items, description, enabled):
        seen[description] = enabled
        return real(items, description, False)      # no bar in the test output

    monkeypatch.setattr(core, "progress_bar", spy)
    return seen


def test_listing_scans_with_the_same_bar_refresh_uses(cfg, monkeypatch, tmp_path):
    """A listing rescans on its own account, and must not do it silently."""
    monkeypatch.setenv("CROSSREPO_CACHE", str(tmp_path / "cold"))
    seen = bars(monkeypatch)
    assert len(crossrepo.list(cfg=cfg)) > 0
    assert seen.get("scanning clones") is True


def test_the_repo_summary_scans_with_it_too(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("CROSSREPO_CACHE", str(tmp_path / "cold"))
    seen = bars(monkeypatch)
    assert len(crossrepo.repos(cfg=cfg)) > 0
    assert seen.get("scanning clones") is True


def test_both_can_be_asked_to_stay_quiet(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("CROSSREPO_CACHE", str(tmp_path / "cold"))
    seen = bars(monkeypatch)
    crossrepo.list(progress=False, cfg=cfg)
    assert seen.get("scanning clones") is False
    seen.clear()
    monkeypatch.setenv("CROSSREPO_CACHE", str(tmp_path / "colder"))
    crossrepo.repos(progress=False, cfg=cfg)
    assert seen.get("scanning clones") is False


def test_the_repo_summary_draws_nothing_off_a_stored_catalog(cfg, monkeypatch):
    from crossrepo import core

    crossrepo.refresh(progress=False, cfg=cfg)        # populate the stored catalog
    monkeypatch.setattr(
        core, "progress_bar",
        lambda *a, **k: pytest.fail("a stored catalog needs no progress bar"),
    )
    assert len(crossrepo.repos(cfg=cfg)) > 0


def test_spec_is_left_to_frame(cfg):
    """It restates repo, path and version, so it is the widest column there is."""
    assert "spec" not in crossrepo.list(cfg=cfg).columns


def test_the_spec_frame_carries_round_trips(cfg):
    table = crossrepo.frame(crossrepo.catalog(cfg=cfg))
    one = table.loc[table["path"] == "results/candidates.csv", "spec"].iloc[0]
    assert crossrepo.fetch(one, cfg=cfg).read_text().count("\n") == 4


def test_refresh_refuses_the_listing_options(cfg):
    """They went with the return value they were shaping."""
    with pytest.raises(TypeError):
        crossrepo.refresh(spec=True, progress=False, cfg=cfg)


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
    table = crossrepo.frame(crossrepo.catalog(cfg=cfg))
    assert [*table.columns].index("bytes") not in aligned(table)


def test_the_alignment_survives_being_worked_on(cfg):
    """A sort or a column selection must not cost the styling."""
    table = crossrepo.frame(crossrepo.catalog(cfg=cfg))
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


# --------------------------------------------- where reading a file goes

def test_get_says_local_for_a_clone_on_this_machine(cfg):
    assert set(crossrepo.list(cfg=cfg)["get"]) == {"local"}


def test_get_follows_the_description(cfg):
    columns = [*crossrepo.list(cfg=cfg).columns]
    assert columns[columns.index("description") - 1] == "get"


def test_the_version_history_scans_with_a_bar_too(cfg, monkeypatch, tmp_path):
    """The third of the three tables, and it rescans on its own account too."""
    monkeypatch.setenv("CROSSREPO_CACHE", str(tmp_path / "cold"))
    seen = bars(monkeypatch)
    assert len(crossrepo.versions("sweep-scan", "candidates.csv", cfg=cfg)) > 0
    assert seen.get("scanning clones") is True


def test_the_version_history_can_be_asked_to_stay_quiet(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("CROSSREPO_CACHE", str(tmp_path / "cold"))
    seen = bars(monkeypatch)
    crossrepo.versions("sweep-scan", "candidates.csv", progress=False, cfg=cfg)
    assert seen.get("scanning clones") is False


# --------------------------------------------- everything about one file

def fields(text):
    """The printed block as a mapping, its heading dropped."""
    return dict(
        line.split(None, 1) for line in text.splitlines()[2:] if line.strip()
    )


def test_info_prints_what_is_stored(cfg, capsys):
    crossrepo.info("sweep-scan", "candidates.csv", cfg=cfg)
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "acme/sweep-scan:results/candidates.csv"
    got = fields(out)
    assert got["description"] == "Sweep candidates, one row per gene"
    assert got["get"] == "local"
    assert got["path"] == "results/candidates.csv"
    assert got["manifest"] == "results/crossrepo.yml"
    assert len(got["version"]) == 40
    assert got["size"].endswith("B")           # as the terminal writes it


def test_info_returns_nothing(cfg, capsys):
    """It is for looking at, like the command it mirrors."""
    assert crossrepo.info("sweep-scan", "candidates.csv", cfg=cfg) is None
    capsys.readouterr()


def test_info_is_addressed_the_way_get_is(cfg, capsys):
    """Same arguments, so asking and fetching differ in the verb alone."""
    import inspect

    taken = inspect.signature(crossrepo.info).parameters
    for name in ("repo", "filename", "version", "owner", "refresh", "cfg"):
        assert name in taken
    crossrepo.info("acme/sweep-scan", "candidates.csv", cfg=cfg)
    assert "acme/sweep-scan" in capsys.readouterr().out
    crossrepo.info("sweep-scan", "candidates.csv", owner="acme", cfg=cfg)
    assert "acme/sweep-scan" in capsys.readouterr().out


def test_info_takes_a_version(cfg, capsys):
    older = crossrepo.versions("sweep-scan", "candidates.csv", cfg=cfg)
    sha = older["version"].iloc[-1]                  # the oldest of them
    crossrepo.info("sweep-scan", "candidates.csv", sha, cfg=cfg)
    got = fields(capsys.readouterr().out)
    assert got["version"] == sha
    assert got["commit"] == "first results"


def test_info_says_when_nothing_matches(cfg):
    with pytest.raises(LookupError):
        crossrepo.info("sweep-scan", "no-such-file.csv", cfg=cfg)


# ------------------------------------- the terminal and the notebook agree

def test_the_listing_columns_are_one_definition(cfg, capsys):
    """`crossrepo list` and `crossrepo.list()` show the same four, by name."""
    from crossrepo import cli

    assert [*crossrepo.list(cfg=cfg).columns] == [*crossrepo.LIST_COLUMNS]
    assert cli.LIST_COLUMNS is crossrepo.LIST_COLUMNS


def test_info_reads_the_same_in_both(cfg, capsys, by_spec):
    """Both ends print `describe`, so neither can drift from the other."""
    from crossrepo import core

    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    crossrepo.info("sweep-scan", "candidates.csv", cfg=cfg)
    printed = capsys.readouterr().out.rstrip("\n")
    assert printed == "\n".join(core.describe(entry, entry.latest))
