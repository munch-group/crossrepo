"""
Tests for publishing a file from the command line.

Publishing is naming a file in the ``crossrepo.yml``; ``crossrepo share`` is
that written for you, with the checks that say the naming means what it looks
like it means.
"""

from pathlib import Path

import pytest

from crossrepo import cli, manifest

from fixtures import commit, init


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A repository with a file, a directory outside results, and a link."""
    here = init(tmp_path / "proj")
    (here / "results").mkdir()
    (here / "data").mkdir()
    (here / "steps").mkdir()
    (here / ".gitignore").write_text("steps/\n")
    (here / "results" / "hits.csv").write_text("gene,score\nA,1\n")
    (here / "data" / "samples.csv").write_text("s,1\n")
    (here / "steps" / "big.csv").write_text("x,y\n0,1\n")
    (here / "results" / "big.csv").symlink_to("../steps/big.csv")
    (here / "crossrepo.toml").write_text('asset_dirs = ["results"]\n')
    commit(here, "a repository with something worth publishing")
    monkeypatch.chdir(here)
    return here


def run(*args):
    """Run the CLI from inside the repository."""
    return cli.main([*args])


def published(repo: Path, where: str = "results/crossrepo.yml"):
    """What the manifest publishes, by key."""
    got = manifest.parse((repo / where).read_text(), where.rsplit("/", 1)[0])
    return got.files


# ---------------------------------------------------------------- the happy way

def test_share_names_a_tracked_file(repo, capsys):
    assert run("share", "results/hits.csv", "Sweep hits, one row per gene") == 0
    assert published(repo) == {"hits.csv": "Sweep hits, one row per gene"}
    assert "results/hits.csv" in capsys.readouterr().out


def test_share_writes_the_manifest_when_there_is_none(repo, capsys):
    assert not (repo / "results" / "crossrepo.yml").exists()
    run("share", "results/hits.csv", "what it holds")
    assert (repo / "results" / "crossrepo.yml").exists()


def test_share_adds_to_a_manifest_that_is_already_there(repo, capsys):
    run("share", "results/hits.csv", "the first")
    (repo / "results" / "other.csv").write_text("k,v\n")
    commit(repo, "a second file to publish")
    assert run("share", "results/other.csv", "the second") == 0
    assert published(repo) == {
        "hits.csv": "the first", "other.csv": "the second",
    }


def test_share_says_what_a_file_holds_when_asked_again(repo, capsys):
    run("share", "results/hits.csv", "first words")
    run("share", "results/hits.csv", "second words")
    assert published(repo) == {"hits.csv": "second words"}


def test_a_file_outside_the_asset_directory_is_named_from_the_root(repo, capsys):
    """There is one spelling for a path that reaches out of results."""
    assert run("share", "data/samples.csv", "Kept beside the raw data") == 0
    assert published(repo) == {"/data/samples.csv": "Kept beside the raw data"}


def test_a_directory_can_be_published(repo, capsys):
    (repo / "results" / "table.parquet").mkdir()
    (repo / "results" / "table.parquet" / "part-0.parquet").write_text("a\n")
    commit(repo, "a dataset")
    assert run("share", "results/table.parquet", "One table over files") == 0
    assert "table.parquet" in published(repo)


# ------------------------------------------------------------------ the checks

def test_an_untracked_file_is_refused(repo, capsys):
    (repo / "results" / "scratch.csv").write_text("not committed\n")
    assert run("share", "results/scratch.csv", "nope") != 0
    said = capsys.readouterr().err
    assert "not tracked by git" in said
    assert "git add results/scratch.csv" in said
    assert not (repo / "results" / "crossrepo.yml").exists()


def test_a_file_that_is_not_there_is_refused(repo, capsys):
    assert run("share", "results/nothing.csv", "nope") != 0
    assert "is not there" in capsys.readouterr().err


def test_a_path_outside_a_repository_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "loose.csv").write_text("a\n")
    assert run("share", "loose.csv", "nope") != 0
    assert "not in a git repository" in capsys.readouterr().err


# ----------------------------------------------------------------- links

def test_a_link_is_published_and_stamped(repo, capsys):
    assert run("share", "results/big.csv", "Merged per-sample table") == 0
    got = manifest.parse(
        (repo / "results" / "crossrepo.yml").read_text(), "results"
    )
    assert got.files == {"big.csv": "Merged per-sample table"}
    stamp = got.stamp("results/big.csv")
    assert stamp is not None and stamp.size == len("x,y\n0,1\n")
    assert "stamped" in capsys.readouterr().out


def test_a_link_to_something_git_also_tracks_is_refused(repo, capsys):
    """Published twice, once as bytes and once as a stamp that could disagree."""
    (repo / "inside.csv").write_text("q\n")
    (repo / "results" / "bad.csv").symlink_to("../inside.csv")
    commit(repo, "a link to a committed file")
    assert run("share", "results/bad.csv", "nope") != 0
    said = capsys.readouterr().err
    assert "which git tracks too" in said
    assert not (repo / "results" / "crossrepo.yml").exists()


def test_an_ordinary_file_is_not_stamped(repo, capsys):
    run("share", "results/hits.csv", "committed, so git versions it")
    got = manifest.parse(
        (repo / "results" / "crossrepo.yml").read_text(), "results"
    )
    assert got.stamp("results/hits.csv") is None
    assert "stamped" not in capsys.readouterr().out


def test_a_link_to_a_directory_is_stamped_as_a_dataset(repo, capsys):
    out = repo / "steps" / "out.parquet"
    out.mkdir()
    (out / "part-0.parquet").write_text("a,1\n")
    (out / "part-1.parquet").write_text("b,2\n")
    (repo / "results" / "big.parquet").symlink_to("../steps/out.parquet")
    commit(repo, "a dataset published as a link")
    assert run("share", "results/big.parquet", "Partitioned by chromosome") == 0
    stamp = manifest.parse(
        (repo / "results" / "crossrepo.yml").read_text(), "results"
    ).stamp("results/big.parquet")
    assert stamp is not None and stamp.parts == 2


def test_sharing_again_keeps_the_stamp_and_changes_the_words(repo, capsys):
    run("share", "results/big.csv", "first words")
    before = manifest.parse(
        (repo / "results" / "crossrepo.yml").read_text(), "results"
    ).stamp("results/big.csv")
    run("share", "results/big.csv", "second words")
    after = manifest.parse(
        (repo / "results" / "crossrepo.yml").read_text(), "results"
    )
    assert after.files == {"big.csv": "second words"}
    assert after.stamp("results/big.csv") == before
