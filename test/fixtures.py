"""
Build real git repositories to test against.

The tests run against working trees created here rather than against mocked git
output, so that git's actual behaviour -- pathspec matching, rename detection,
blob identity -- is what is being tested.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import re
from pathlib import Path
from typing import Optional

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
        Path of the script, to be put in ``CROSSREPO_SSH``. A destination holding
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
    (a / "results" / "crossrepo.yml").write_text(
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
    (a / "results" / "nested" / "crossrepo.yml").write_text(
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
    (b / "Results" / "crossrepo.yml").write_text(
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
    (c / "results" / "crossrepo.yml").write_text(
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


LINK_V1 = "gene,score\n" + "".join(f"g{i},{i}\n" for i in range(50))
"""First content of the file the fixture repositories publish as a link."""

LINK_V2 = LINK_V1 + "g50,50\n"
"""Second content of it, standing for a pipeline having run again."""

LINK_OTHER = "gene,score\nz,9\n"
"""Content of a link with the same target path in a different repository."""

LINK_ABSENT = LINK_V1 + "never written anywhere\n"
"""Content stamped for a link whose target is missing.

Distinct from every other fixture's, so that no other repository can put it in
the cache and let a test that means to read a missing file succeed.
"""

LINK_UNSTAMPED = LINK_V1 + "stamped, then written over\n"
"""Content stamped for a link whose target was regenerated without stamping.

Distinct for the same reason: the cache is keyed by content, so a stamp naming
content some other repository publishes would be satisfied from the cache.
"""


def digest(text: str) -> str:
    """
    Hash content the way a manifest stamp records it.

    Computed here rather than with `crossrepo.cache.content_hash`, so that the
    tests check the library against an independent answer.

    Parameters
    ----------
    text :
        Content to hash.

    Returns
    -------
    :
        The sha256 digest in lower case hexadecimal.
    """
    return hashlib.sha256(text.encode()).hexdigest()


def write_link_repo(
    path: Path, content: Optional[str], description: str = "Merged table",
    stamped: bool = True, key: str = "big.csv", stamp_for: Optional[str] = None,
) -> Path:
    """
    Build a repository publishing one file as a symbolic link.

    The target is deliberately untracked, which is the point of publishing this
    way: the file is too large to commit, so git holds the link and the manifest
    holds the stamp saying which content the link stands for.

    Parameters
    ----------
    path :
        Directory to create the repository in.
    content :
        Content to write at the link's target, or `None` to leave the target
        missing, as a clone without the pipeline's output would.
    description :
        What the manifest says the file holds.
    stamped :
        Whether to write a stamp. Without one the file is published as a link
        that nothing identifies, which the scan warns about.
    key :
        Manifest key to publish the link under, so that a pattern can be used
        instead of the file name.
    stamp_for :
        Content to stamp, when that is not the content written. Defaults to
        `content`, which is the honest case; giving something else is how a
        repository whose file was regenerated without stamping is built.

    Returns
    -------
    :
        `path`.
    """
    repo = init(_mkdir(path))
    (repo / ".gitignore").write_text("steps/\n")
    (repo / "steps").mkdir()
    (repo / "results").mkdir()
    if content is not None:
        (repo / "steps" / "big.csv").write_text(content)
    (repo / "results" / "big.csv").symlink_to("../steps/big.csv")
    (repo / "results" / "plain.csv").write_text("k,v\nx,1\n")
    stamp = ""
    if stamped:
        # A missing target still gets a stamp: what a link stands for is what the
        # manifest says, not what happens to be on this machine.
        said = stamp_for if stamp_for is not None else content
        assert said is not None, "a stamp needs content to describe"
        stamp = f'    sha256: "{digest(said)}"\n    size: {len(said)}\n'
    (repo / "results" / "crossrepo.yml").write_text(
        "files:\n"
        "  plain.csv: An ordinary committed table\n"
        f"  {key}:\n    description: {description}\n{stamp}"
    )
    commit(repo, "first results")
    return repo


def restamp(repo: Path, content: str, message: str) -> None:
    """
    Regenerate a link's target and stamp it again, as a pipeline rerun does.

    The new content is written beside the old file and renamed over it, which is
    what a workflow manager does and what keeps a cached hard link to the old
    content pointing at the old content. Truncating the file in place instead
    would change the cached object too.

    Parameters
    ----------
    repo :
        Working tree to change.
    content :
        New content for the target.
    message :
        Commit message for the new stamp.
    """
    target = repo / "steps" / "big.csv"
    fresh = target.with_suffix(".new")
    fresh.write_text(content)
    fresh.replace(target)
    text = (repo / "results" / "crossrepo.yml").read_text()
    text = re.sub(r'sha256: "[0-9a-f]{64}"', f'sha256: "{digest(content)}"', text)
    text = re.sub(r"size: \d+", f"size: {len(content)}", text)
    (repo / "results" / "crossrepo.yml").write_text(text)
    commit(repo, message)


def make_links(base: Path) -> Path:
    """
    Build the repositories that publish result files as symbolic links.

    Kept apart from [](`fixtures.make`) so that the catalog the rest of the
    suite reads does not change. Five repositories cover what a link can be:
    one stamped and regenerated, so it has two versions of content git never
    held; one whose link is written with the same target path but stands for
    different content, which a cache keyed by the link rather than by the
    content would confuse; one with no stamp; one whose target is missing; and
    one whose target holds something other than what was stamped.

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

    proj = write_link_repo(base / "links" / "proj", LINK_V1)
    run(proj, "remote", "add", "origin", "git@github.com:links/proj.git")
    restamp(proj, LINK_V2, "regenerate the big table")
    # A commit that touches the manifest without changing this stamp, which is
    # therefore not a version of this file.
    text = (proj / "results" / "crossrepo.yml").read_text()
    (proj / "results" / "crossrepo.yml").write_text(
        text.replace("Merged table", "Merged per-sample table")
    )
    commit(proj, "describe the table better")

    # The same link text, so a cache keyed by it would serve one for the other.
    write_link_repo(base / "links" / "twin", LINK_OTHER)

    write_link_repo(base / "links" / "bare", LINK_V1, stamped=False)
    write_link_repo(base / "links" / "gone", None, stamp_for=LINK_ABSENT)
    write_link_repo(
        base / "links" / "stale", LINK_V2, stamp_for=LINK_UNSTAMPED
    )
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
