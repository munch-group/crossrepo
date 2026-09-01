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


CODE = "424242"
"""The verification code the stand-in for ssh accepts."""


def fake_ssh(where: Path, home: Path) -> str:
    """
    Write a stand-in for ssh that runs what it is given on this machine.

    The suite must not need a server, but the interesting parts of reading a
    repository over ssh are on this side of the connection: how the command is
    built, how a path is quoted, and that a leading ``~`` is left for the far
    side to expand. All of that is exercised by handing the command to a shell
    with `home` as its home directory, which is what a real ssh would do.

    Parameters
    ----------
    where :
        Directory to write the script in.
    home :
        Directory that ``~`` expands to for commands the script runs.

    Returns
    -------
    :
        Path of the script, to be put in ``LABDATA_SSH``. A destination holding
        ``unreachable`` fails the way an unanswering host does; one holding
        ``twofactor`` the way a host does that wanted something typed and had
        nobody to ask; one holding ``askpass`` asks for `CODE` the way a host
        wanting a second factor does, through whatever ``SSH_ASKPASS`` names;
        and one holding ``nogit`` answers but has no git on its PATH, as a
        cluster login often does not outside an interactive shell.
    """
    script = where / "fake-ssh"
    script.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -o) shift 2 ;;\n"
        "    -*) shift ;;\n"
        "    *) break ;;\n"
        "  esac\n"
        "done\n"
        "host=$1; shift\n"
        "case \"$host\" in\n"
        "  *unreachable*)\n"
        "    echo \"ssh: connect to host $host: Connection refused\" >&2\n"
        "    exit 255 ;;\n"
        "  *twofactor*)\n"
        "    echo \"$host: Permission denied "
        "(publickey,keyboard-interactive).\" >&2\n"
        "    exit 255 ;;\n"
        "  *nogit*)\n"
        "    PATH=/nonexistent\n"
        "    export PATH ;;\n"
        "  *askpass*)\n"
        "    if [ -z \"$SSH_ASKPASS\" ]; then\n"
        "      echo \"$host: Permission denied (keyboard-interactive).\" >&2\n"
        "      exit 255\n"
        "    fi\n"
        "    code=$(\"$SSH_ASKPASS\" \"Verification code: \")\n"
        f"    if [ \"$code\" != '{CODE}' ]; then\n"
        "      echo \"$host: Permission denied (keyboard-interactive).\" >&2\n"
        "      exit 255\n"
        "    fi ;;\n"
        "esac\n"
        f"HOME='{home}'\n"
        "export HOME\n"
        "cd \"$HOME\" || exit 255\n"     # ssh starts a command in the home dir
        "exec sh -c \"$*\"\n"
    )
    script.chmod(0o755)
    return str(script)


def make(base: Path) -> Path:
    """
    Build the fixture repositories.

    Three repositories are created, covering the cases seen in real projects: an
    untagged repository whose result file has three versions, a repository that
    committed a capitalised ``Results`` directory and carries a tag, and a
    repository holding a Git LFS pointer whose content is not available locally.
    A plain directory that is not a repository is included so that discovery can
    be shown to skip it, and a fourth repository commits result files without a
    manifest, so that the manifest gate can be shown to hold. The first
    repository also holds one file name used in two directories with different
    content, one file name that is not ASCII, a manifest too deep in the tree to
    be read, and a ``.parquet`` directory published as one dataset across two
    versions.

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
    (a / "results" / "labdata.yml").write_text(
        "files:\n"
        '  candidates.csv: Sweep candidates, one row per gene\n'
        '  stable.csv: A table that never changes\n'
        '  sub/stable.csv: The same name, one directory down\n'
        '  "h\u00f8jde.csv": A file name that is not ASCII\n'
        '  "*.csv": Some other table this project publishes\n'
        '  table.parquet: One dataset split over files to fit a size limit\n',
        encoding="utf-8",
    )
    (a / "results" / "candidates.csv").write_text("gene,score\nA,1\n")
    (a / "results" / "stable.csv").write_text("k,v\nx,1\n")
    (a / "results" / "notes.md").write_text("# notes\n")     # not in the manifest
    # Same file name in two subdirectories, as in real analysis repos, with
    # different content: both last change in this one commit, so they share a
    # version key and would collide in a cache named by file name alone.
    (a / "results" / "sub").mkdir()
    (a / "results" / "sub" / "stable.csv").write_text("k,v\ny,2\n")
    # A non-ASCII file name, which git reports differently to `ls-files -z` and
    # to `log --name-only` unless core.quotePath is off.
    (a / "results" / "h\u00f8jde.csv").write_text("h,1\n", encoding="utf-8")
    # A manifest deeper in the tree, which is not read: only the one directly in
    # the results directory governs, so both files here fall under its "*.csv".
    (a / "results" / "nested").mkdir()
    (a / "results" / "nested" / "labdata.yml").write_text(
        "files:\n  one.csv: this manifest is too deep to be read\n"
    )
    (a / "results" / "nested" / "one.csv").write_text("s,1\n")
    (a / "results" / "nested" / "two.csv").write_text("h,1\n")
    # A dataset published as a directory, as a big parquet table has to be.
    (a / "results" / "table.parquet").mkdir()
    for i in range(3):
        (a / "results" / "table.parquet" / f"part-{i}.parquet").write_text(f"row,{i}\n")
    (a / "scratch.csv").write_text("junk\n")                 # outside results/
    commit(a, "first results")
    (a / "results" / "candidates.csv").write_text("gene,score\nA,1\nB,2\n")
    commit(a, "add gene B")
    (a / "results" / "candidates.csv").write_text("gene,score\nA,1\nB,2\nC,3\n")
    commit(a, "add gene C")
    # Repartition one part only, so the dataset gains a version and the parts
    # that did not change can be shown to cost nothing.
    (a / "results" / "table.parquet" / "part-1.parquet").write_text("row,1\nrow,1b\n")
    commit(a, "repartition the table")
    (a / "results" / "untracked.csv").write_text("not,committed\n")

    # Capitalised Results/, one tag, and content identical to a file in acme.
    b = init(_mkdir(base / "acme" / "hic-borders"))
    run(b, "remote", "add", "origin", "https://github.com/other-org/hic-borders.git")
    (b / "Results").mkdir()
    (b / "Results" / "labdata.yml").write_text(
        "files:\n"
        "  borders.tsv: TAD borders called from Hi-C\n"
        "  stable.csv: A table that never changes\n"
    )
    (b / "Results" / "borders.tsv").write_text("chrom\tstart\n1\t100\n")
    (b / "Results" / "stable.csv").write_text("k,v\nx,1\n")
    commit(b, "borders v1")
    run(b, "tag", "v1.0")

    # A Git LFS pointer with no local object, and no remote at all.
    c = init(_mkdir(base / "other" / "big-thing"))
    (c / "results").mkdir()
    (c / "results" / "labdata.yml").write_text(
        "files:\n  big.h5: A large table kept in Git LFS\n"
    )
    (c / ".gitattributes").write_text("results/*.h5 filter=lfs diff=lfs merge=lfs -text\n")
    (c / "results" / "big.h5").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "de" * 32 + "\nsize 512189753\n"
    )
    commit(c, "add big file via lfs")

    # A repo with results committed but no manifest: it publishes nothing.
    d = init(_mkdir(base / "other" / "no-manifest"))
    (d / "results").mkdir()
    (d / "results" / "data.csv").write_text("a,b\n1,2\n")
    commit(d, "results, but nothing published")

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
