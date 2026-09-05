"""Tests for a dataset published as a directory of files."""

import pytest

from crossrepo import core
from crossrepo.model import Spec

DATASET = "acme/sweep-scan:results/table.parquet"


def test_a_listed_directory_is_one_catalog_entry(by_spec):
    entry = by_spec[DATASET]
    assert entry.name == "table.parquet"
    assert entry.description == "One dataset split over files to fit a size limit"
    assert entry.latest.parts == 3


def test_the_parts_are_not_catalogued_separately(entries):
    assert not any(e.path.startswith("results/table.parquet/") for e in entries)
    assert not any(e.name.startswith("part-") for e in entries)


def test_the_size_is_the_total_over_the_parts(by_spec, repos):
    entry = by_spec[DATASET]
    on_disk = sum(
        p.stat().st_size
        for p in (repos / "acme" / "sweep-scan" / "results" / "table.parquet").iterdir()
    )
    assert entry.latest.size == on_disk


def test_the_content_key_is_the_git_tree_sha(by_spec, repos):
    from crossrepo import gitutil

    entry = by_spec[DATASET]
    assert entry.latest.blob == gitutil.tree_at(
        repos / "acme" / "sweep-scan", "HEAD", "results/table.parquet"
    )


def test_the_dataset_carries_the_repo_head_like_everything_else(by_spec):
    other = by_spec["acme/sweep-scan:results/candidates.csv"]
    assert by_spec[DATASET].latest.sha == other.latest.sha


def test_it_resolves_by_bare_name(entries):
    entry = core.resolve_one(entries, Spec.parse("sweep-scan:table.parquet"))
    assert entry.path == "results/table.parquet"


def test_fetching_brings_down_the_whole_directory(by_spec):
    entry = by_spec[DATASET]
    got = core.materialize(entry, entry.latest)
    assert got.is_dir()
    assert sorted(p.name for p in got.iterdir()) == [
        "part-0.parquet", "part-1.parquet", "part-2.parquet"
    ]
    assert (got / "part-1.parquet").read_text() == "row,1\nrow,1b\n"


def test_history_covers_every_version_of_the_dataset(by_spec):
    hist = core.versions(by_spec[DATASET])
    assert [v.subject for v in hist] == ["repartition the table", "first results"]
    assert all(v.parts == 3 for v in hist)
    assert hist[0].blob != hist[1].blob          # the tree sha tracks content


def test_an_older_version_gives_the_older_parts(by_spec):
    entry = by_spec[DATASET]
    older = core.versions(entry)[-1]
    got = core.materialize(entry, older)
    assert (got / "part-1.parquet").read_text() == "row,1\n"


def test_unchanged_parts_are_stored_once_across_versions(by_spec):
    entry = by_spec[DATASET]
    newest, oldest = core.versions(entry)[0], core.versions(entry)[-1]
    a = core.materialize(entry, newest)
    b = core.materialize(entry, oldest)
    assert a != b
    # part-0 did not change, so both versions point at one object on disk
    assert (a / "part-0.parquet").stat().st_ino == (b / "part-0.parquet").stat().st_ino
    # part-1 did change, so they do not
    assert (a / "part-1.parquet").stat().st_ino != (b / "part-1.parquet").stat().st_ino


def test_the_notebook_api_returns_the_directory(cfg, capsys):
    path = core.get("sweep-scan", "table.parquet", cfg=cfg)
    out = capsys.readouterr().out
    assert path.is_dir()
    assert "One dataset split over files" in out


def test_an_unlisted_directory_is_not_a_dataset(entries):
    """results/sub holds a published file but is not itself named."""
    assert not any(e.path == "results/sub" for e in entries)
    assert any(e.path == "results/sub/stable.csv" for e in entries)


def test_the_version_is_the_repo_head_not_a_part_commit(tmp_path):
    """Every published thing is stamped with the commit the repository points at."""
    import os
    import subprocess

    from crossrepo.config import Config

    repo = tmp_path / "same-second"
    repo.mkdir()
    stamp = "2025-01-01T12:00:00+00:00"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
        "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
    }

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                       capture_output=True)

    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env,
                   capture_output=True)
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text("files:\n  t.parquet: a dataset\n")
    (repo / "results" / "t.parquet").mkdir()
    for i in range(3):
        (repo / "results" / "t.parquet" / f"part-{i}.parquet").write_text(f"first {i}\n")
    git("add", "-A"); git("commit", "-m", "one")
    # Only one part changes, so the others still point at the first commit. With
    # both commits on the same second, nothing but git's ordering can say which
    # of the two is the newer.
    (repo / "results" / "t.parquet" / "part-1.parquet").write_text("second\n")
    git("add", "-A"); git("commit", "-m", "two")

    entries = core.build(Config(roots=[str(tmp_path)]))
    dataset = next(e for e in entries if e.name == "t.parquet")
    assert dataset.latest.subject == "two"
    assert core.materialize(dataset, dataset.latest).joinpath(
        "part-1.parquet"
    ).read_text() == "second\n"
