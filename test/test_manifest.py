"""Tests for the crossrepo.yml that decides what a repository publishes."""

import pytest

from crossrepo import core, manifest
from crossrepo.config import Config

from fixtures import commit, init


# ----------------------------------------------------------------- the gate

def test_a_results_dir_without_a_manifest_publishes_nothing(entries):
    """Committing is no longer enough on its own."""
    assert not any(e.repo == "no-manifest" for e in entries)


def test_a_committed_but_unlisted_file_is_not_published(entries):
    names = {e.name for e in entries}
    assert "notes.md" not in names       # committed beside the published files
    assert "data.csv" not in names       # committed, but its repo has no manifest


def test_the_manifest_itself_is_never_a_result_file(entries):
    assert not any(manifest.is_manifest(e.path) for e in entries)
    assert "crossrepo.yml" not in {e.name for e in entries}


def test_an_uncommitted_manifest_publishes_nothing(repos, cfg):
    """Publishing is a commit, so an unstaged manifest must not count."""
    stray = repos / "other" / "no-manifest" / "results" / "crossrepo.yml"
    stray.write_text("files:\n  data.csv: not committed, so not published\n")
    try:
        assert not any(e.repo == "no-manifest" for e in core.build(cfg))
    finally:
        stray.unlink()


# --------------------------------------------------------- descriptions

def test_the_description_reaches_the_entry(by_spec):
    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    assert entry.description == "Sweep candidates, one row per gene"


def test_a_glob_key_describes_many_files(by_spec):
    entry = by_spec["acme/sweep-scan:results/højde.csv"]
    assert entry.description == "A file name that is not ASCII"


def test_an_exact_key_beats_a_glob(by_spec):
    """"*.csv" also matches stable.csv, but the named entry wins."""
    entry = by_spec["acme/sweep-scan:results/stable.csv"]
    assert entry.description == "A table that never changes"


def test_the_description_survives_the_stored_catalog(cfg):
    core.save(core.build(cfg), cfg)
    stored = {e.path: e for e in core.load_cached(cfg=cfg)}
    assert stored["results/candidates.csv"].description == (
        "Sweep candidates, one row per gene"
    )


# ------------------------------------------ only the one manifest is read

def test_a_manifest_deeper_in_the_tree_is_not_read(by_spec):
    """Only results/crossrepo.yml governs, so its "*.csv" covers both files."""
    one = by_spec["acme/sweep-scan:results/nested/one.csv"]
    two = by_spec["acme/sweep-scan:results/nested/two.csv"]
    assert one.description == "Some other table this project publishes"
    assert two.description == one.description       # the deep manifest named only one


def test_a_manifest_only_deeper_in_the_tree_publishes_nothing(tmp_path):
    repo = init(tmp_path / "deep-only")
    (repo / "results" / "sub").mkdir(parents=True)
    (repo / "results" / "sub" / "crossrepo.yml").write_text("files:\n  x.csv: too deep\n")
    (repo / "results" / "sub" / "x.csv").write_text("a\n")
    commit(repo, "a manifest nobody reads")
    assert core.build(Config(roots=[str(tmp_path)])) == []


def test_only_configured_directories_are_searched(tmp_path):
    """A results directory somewhere else is found only once it is configured."""
    repo = init(tmp_path / "buried")
    (repo / "analysis" / "results").mkdir(parents=True)
    (repo / "analysis" / "results" / "crossrepo.yml").write_text(
        "files:\n  x.csv: buried\n"
    )
    (repo / "analysis" / "results" / "x.csv").write_text("a\n")
    commit(repo, "results, but not where the default looks")

    default = Config(roots=[str(tmp_path)])
    assert core.build(default) == []

    configured = Config(roots=[str(tmp_path)],
                        asset_dirs=["analysis/results"])
    assert [e.path for e in core.build(configured)] == ["analysis/results/x.csv"]


def test_asset_dirs_are_paths_at_any_depth(tmp_path):
    repo = init(tmp_path / "deep")
    nested = repo / "some_dir" / "some_sub_dir" / "some_sub_sub_dir"
    nested.mkdir(parents=True)
    (nested / "crossrepo.yml").write_text("files:\n  deep.csv: buried but published\n")
    (nested / "deep.csv").write_text("a\n")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text("files:\n  near.csv: the usual place\n")
    (repo / "results" / "near.csv").write_text("b\n")
    commit(repo, "two results directories")

    cfg = Config(roots=[str(tmp_path)],
                 asset_dirs=["results", "some_dir/some_sub_dir/some_sub_sub_dir"])
    assert sorted(e.path for e in core.build(cfg)) == [
        "results/near.csv",
        "some_dir/some_sub_dir/some_sub_sub_dir/deep.csv",
    ]


def test_a_nested_results_dir_tolerates_slashes_and_casing(tmp_path):
    repo = init(tmp_path / "casing")
    nested = repo / "Analysis" / "Results"
    nested.mkdir(parents=True)
    (nested / "crossrepo.yml").write_text("files:\n  x.csv: published\n")
    (nested / "x.csv").write_text("a\n")
    commit(repo, "capitalised and nested")

    cfg = Config(roots=[str(tmp_path)], asset_dirs=["/analysis/results/"])
    assert [e.path for e in core.build(cfg)] == ["Analysis/Results/x.csv"]


def test_a_manifest_below_a_nested_results_dir_is_still_ignored(tmp_path):
    repo = init(tmp_path / "deeper")
    nested = repo / "a" / "b"
    (nested / "sub").mkdir(parents=True)
    (nested / "crossrepo.yml").write_text('files:\n  "*.csv": from the right place\n')
    (nested / "sub" / "crossrepo.yml").write_text("files:\n  y.csv: too deep\n")
    (nested / "sub" / "y.csv").write_text("a\n")
    commit(repo, "one manifest below another")

    cfg = Config(roots=[str(tmp_path)], asset_dirs=["a/b"])
    entries = core.build(cfg)
    assert [e.path for e in entries] == ["a/b/sub/y.csv"]
    assert entries[0].description == "from the right place"


# ---------------------------------------------------------- which manifest

def test_yml_wins_over_yaml_when_a_repo_carries_both(tmp_path):
    """Precedence follows MANIFEST_NAMES, not the alphabet, which would say .yaml."""
    repo = init(tmp_path / "both-suffixes")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text("files:\n  x.csv: the one that wins\n")
    (repo / "results" / "crossrepo.yaml").write_text("files:\n  x.csv: the other one\n")
    (repo / "results" / "x.csv").write_text("a\n")
    commit(repo, "two spellings of the manifest")

    entries = core.build(Config(roots=[str(tmp_path)]))
    assert [e.description for e in entries] == ["the one that wins"]


def test_a_yaml_manifest_is_read_when_it_is_the_only_one(tmp_path):
    repo = init(tmp_path / "yaml-only")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yaml").write_text("files:\n  x.csv: spelled out\n")
    (repo / "results" / "x.csv").write_text("a\n")
    commit(repo, "the longer suffix")

    entries = core.build(Config(roots=[str(tmp_path)]))
    assert [e.description for e in entries] == ["spelled out"]


# ------------------------------------------------------------ bad input

def test_a_broken_manifest_warns_and_publishes_nothing(tmp_path):
    repo = init(tmp_path / "broken")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text("files: [this, is, a, list]\n")
    (repo / "results" / "hits.csv").write_text("a\n")
    commit(repo, "a manifest that is not a mapping")

    cfg = Config(roots=[str(tmp_path)])
    with pytest.warns(UserWarning, match="files"):
        assert core.build(cfg) == []


def test_a_broken_manifest_deeper_down_is_simply_not_read(tmp_path):
    repo = init(tmp_path / "mixed")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text("files:\n  good.csv: fine\n")
    (repo / "results" / "good.csv").write_text("a\n")
    (repo / "results" / "sub").mkdir()
    (repo / "results" / "sub" / "crossrepo.yml").write_text("files: 3\n")
    (repo / "results" / "sub" / "other.csv").write_text("b\n")
    commit(repo, "one good manifest and one broken below it")

    names = {e.name for e in core.build(Config(roots=[str(tmp_path)]))}
    assert names == {"good.csv"}       # no warning: the broken one is never read


# ------------------------------------------------------------- parsing

def test_parse_reads_both_the_short_and_the_long_form():
    m = manifest.parse(
        "files:\n"
        "  a.csv: short form\n"
        "  b.csv:\n"
        "    description: long form\n"
        "  c.csv:\n",
        "results",
    )
    assert m.files == {"a.csv": "short form", "b.csv": "long form", "c.csv": ""}


def test_parse_rejects_a_manifest_with_no_files_key():
    with pytest.raises(manifest.ManifestError, match="no `files` key"):
        manifest.parse("description: a directory\n", "results")


def test_parse_rejects_a_value_that_is_not_a_description():
    with pytest.raises(manifest.ManifestError, match="description"):
        manifest.parse("files:\n  a.csv: [1, 2]\n", "results")


def test_parse_rejects_invalid_yaml():
    with pytest.raises(manifest.ManifestError, match="not valid YAML"):
        manifest.parse("files:\n  a.csv: 'unclosed\n", "results")


def test_an_empty_manifest_is_allowed_and_publishes_nothing():
    assert manifest.parse("files:\n", "results").files == {}
    assert manifest.parse("", "results").files == {}


# ------------------------------------- files named from the repository root

def test_a_key_from_the_root_publishes_a_file_outside_the_results_dir(tmp_path):
    """A result that lives with the data it came from is published where it is."""
    repo = init(tmp_path / "anchored")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text(
        "files:\n"
        "  near.csv: In the results directory\n"
        "  /data/reference/samples.csv: Kept beside the raw data\n"
    )
    (repo / "results" / "near.csv").write_text("a\n")
    (repo / "data" / "reference").mkdir(parents=True)
    (repo / "data" / "reference" / "samples.csv").write_text("s,1\n")
    (repo / "data" / "reference" / "private.csv").write_text("not,published\n")
    commit(repo, "a result that lives outside results/")

    entries = {e.path: e.description for e in core.build(Config(roots=[str(tmp_path)]))}
    assert entries == {
        "results/near.csv": "In the results directory",
        "data/reference/samples.csv": "Kept beside the raw data",
    }


def test_a_pattern_from_the_root_publishes_every_file_it_matches(tmp_path):
    repo = init(tmp_path / "anchored-glob")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text(
        'files:\n  "/data/raw/*.tsv": Raw tables, one per sample\n'
    )
    (repo / "data" / "raw").mkdir(parents=True)
    for name in ("s1.tsv", "s2.tsv", "notes.md"):
        (repo / "data" / "raw" / name).write_text("a\n")
    (repo / "data" / "elsewhere.tsv").write_text("a\n")
    commit(repo, "raw tables published from the root")

    assert sorted(e.path for e in core.build(Config(roots=[str(tmp_path)]))) == [
        "data/raw/s1.tsv",
        "data/raw/s2.tsv",
    ]


def test_a_directory_from_the_root_is_published_as_one_dataset(tmp_path):
    repo = init(tmp_path / "anchored-dataset")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text(
        "files:\n  /steps/03/by_chrom.parquet: One table split over files\n"
    )
    parts = repo / "steps" / "03" / "by_chrom.parquet"
    parts.mkdir(parents=True)
    for i in range(3):
        (parts / f"part-{i}.parquet").write_text(f"row,{i}\n")
    commit(repo, "a dataset outside results/")

    entries = core.build(Config(roots=[str(tmp_path)]))
    assert [e.path for e in entries] == ["steps/03/by_chrom.parquet"]
    assert entries[0].description == "One table split over files"


def test_a_bare_key_does_not_reach_outside_the_results_dir(tmp_path):
    """`hits.csv` publishes the one in the results directory, not its namesake."""
    repo = init(tmp_path / "scoped")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text(
        'files:\n  hits.csv: The published one\n  "*.tsv": Any table\n'
    )
    (repo / "results" / "hits.csv").write_text("a\n")
    (repo / "work").mkdir()
    (repo / "work" / "hits.csv").write_text("b\n")
    (repo / "work" / "draft.tsv").write_text("c\n")
    commit(repo, "the same name in two places")

    assert [e.path for e in core.build(Config(roots=[str(tmp_path)]))] == [
        "results/hits.csv"
    ]


def test_the_reading_side_filters_apply_outside_the_results_dir_too(tmp_path):
    repo = init(tmp_path / "anchored-filtered")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text(
        'files:\n  "/data/*": Everything the data directory holds\n'
    )
    (repo / "data").mkdir()
    (repo / "data" / "table.csv").write_text("a\n")
    (repo / "data" / "table.log").write_text("b\n")
    commit(repo, "two files, one wanted")

    cfg = Config(roots=[str(tmp_path)], exclude=["*.log"])
    assert [e.path for e in core.build(cfg)] == ["data/table.csv"]


def test_a_key_that_climbs_out_with_dots_is_refused(tmp_path):
    """There is one spelling for a path that leaves the directory."""
    repo = init(tmp_path / "climbing")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text(
        "files:\n  ../data/samples.csv: written the wrong way\n"
    )
    (repo / "data").mkdir()
    (repo / "data" / "samples.csv").write_text("a\n")
    commit(repo, "a key that climbs out")

    with pytest.warns(UserWarning, match=r"climbs out"):
        assert core.build(Config(roots=[str(tmp_path)])) == []


def test_parse_rejects_a_key_that_climbs_out():
    with pytest.raises(manifest.ManifestError, match=r"climbs out"):
        manifest.parse("files:\n  ../x.csv: nope\n", "results")


def test_anchored_prefixes_name_the_least_that_has_to_be_listed():
    m = manifest.parse(
        "files:\n"
        "  near.csv: not from the root\n"
        "  /data/samples.csv: a file\n"
        '  "/data/raw/*.tsv": a pattern\n'
        "  /data: the whole directory\n",
        "results",
    )
    assert m.anchored_prefixes() == ["data"]        # the others lie inside it
    assert manifest.parse("files:\n  a.csv: x\n", "results").anchored_prefixes() == []
    assert manifest.parse(
        'files:\n  "/*.csv": anywhere\n', "results"
    ).anchored_prefixes() == [""]                   # the whole repository
