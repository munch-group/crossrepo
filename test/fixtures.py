"""
Build real git repositories to test against.

The tests run against working trees created here rather than against mocked git
output, so that git's actual behaviour -- pathspec matching, rename detection,
blob identity -- is what is being tested.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
}
"""Environment giving git a deterministic identity."""


def run(repo: Path, *args: str) -> None:
    """
    Run a git command in a fixture repository.

    Parameters
    ----------
    repo :
        Working tree to run the command in.
    *args :
        Arguments passed to git.
    """
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=ENV,
                   capture_output=True)


def commit(repo: Path, msg: str) -> None:
    """
    Stage everything in a fixture repository and commit it.

    Parameters
    ----------
    repo :
        Working tree to commit in.
    msg :
        Commit message.
    """
    run(repo, "add", "-A")
    run(repo, "commit", "-m", msg)


def init(path: Path) -> Path:
    """
    Create an empty git repository.

    Parameters
    ----------
    path :
        Directory to initialise.

    Returns
    -------
    :
        `path`.
    """
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=ENV,
                   capture_output=True)
    return path


def make(base: Path) -> Path:
    """
    Build the fixture repositories.

    Three repositories are created, covering the cases seen in real projects: an
    untagged repository whose result file has three versions, a repository that
    committed a capitalised ``Results`` directory and carries a tag, and a
    repository holding a Git LFS pointer whose content is not available locally.
    A plain directory that is not a repository is included so that discovery can
    be shown to skip it.

    Parameters
    ----------
    base :
        Directory to build in. It is removed first if it exists.

    Returns
    -------
    :
        `base`.
    """
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)

    # Untagged, three versions of one file, plus a file that never changes.
    a = init(_mkdir(base / "acme" / "sweep-scan"))
    run(a, "remote", "add", "origin", "git@github.com:acme/sweep-scan.git")
    (a / "results").mkdir()
    (a / "results" / "candidates.csv").write_text("gene,score\nA,1\n")
    (a / "results" / "stable.csv").write_text("k,v\nx,1\n")
    (a / "results" / "notes.md").write_text("# notes\n")     # excluded by pattern
    # same file name in two subdirectories, as in real analysis repos
    (a / "results" / "sub").mkdir()
    (a / "results" / "sub" / "stable.csv").write_text("k,v\nx,1\n")
    (a / "scratch.csv").write_text("junk\n")                 # outside results/
    commit(a, "first results")
    (a / "results" / "candidates.csv").write_text("gene,score\nA,1\nB,2\n")
    commit(a, "add gene B")
    (a / "results" / "candidates.csv").write_text("gene,score\nA,1\nB,2\nC,3\n")
    commit(a, "add gene C")
    (a / "results" / "untracked.csv").write_text("not,committed\n")

    # Capitalised Results/, one tag, and content identical to a file in acme.
    b = init(_mkdir(base / "acme" / "hic-borders"))
    run(b, "remote", "add", "origin", "https://github.com/other-org/hic-borders.git")
    (b / "Results").mkdir()
    (b / "Results" / "borders.tsv").write_text("chrom\tstart\n1\t100\n")
    (b / "Results" / "stable.csv").write_text("k,v\nx,1\n")
    commit(b, "borders v1")
    run(b, "tag", "v1.0")

    # A Git LFS pointer with no local object, and no remote at all.
    c = init(_mkdir(base / "other" / "big-thing"))
    (c / "results").mkdir()
    (c / ".gitattributes").write_text("results/*.h5 filter=lfs diff=lfs merge=lfs -text\n")
    (c / "results" / "big.h5").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "de" * 32 + "\nsize 512189753\n"
    )
    commit(c, "add big file via lfs")

    _mkdir(base / "other" / "not-a-repo" / "results")
    (base / "other" / "not-a-repo" / "results" / "x.csv").write_text("a\n")
    return base


def _mkdir(path: Path) -> Path:
    """
    Create a directory and any missing parents.

    Parameters
    ----------
    path :
        Directory to create.

    Returns
    -------
    :
        `path`.
    """
    path.mkdir(parents=True, exist_ok=True)
    return path
