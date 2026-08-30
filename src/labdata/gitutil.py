"""
Thin wrappers over git plumbing commands.

Everything here uses plumbing rather than porcelain, so the output does not
depend on the user's git configuration, aliases or locale. Nothing in this
module writes to a repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

SEP = "\x1f"
"""Field separator used in ``--format`` strings; cannot occur in a path."""

REC = "\x1e"
"""Record separator used to delimit commits in ``git log`` output."""

LFS_PREFIX = b"version https://git-lfs"
"""First bytes of a Git LFS pointer file."""


class GitError(RuntimeError):
    """Raised when a git command fails."""


def git(
    repo: Path, *args: str, binary: bool = False, check: bool = True
) -> Union[str, bytes]:
    """
    Run a git command in a repository and return its standard output.

    Parameters
    ----------
    repo :
        Working tree to run the command in.
    *args :
        Arguments passed to git, not including the command name itself.
    binary :
        Return raw bytes instead of decoded text. Use this for file content,
        which is not necessarily valid UTF-8.
    check :
        Raise [](`labdata.gitutil.GitError`) when git exits non-zero. When
        `False`, the standard output produced before the failure is returned.

    Returns
    -------
    :
        Standard output of the command, as `str` unless `binary` is `True`.

    Raises
    ------
    GitError
        If the command fails and `check` is `True`.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def is_repo(path: Path) -> bool:
    """
    Test whether a directory is a git working tree.

    Parameters
    ----------
    path :
        Directory to test.

    Returns
    -------
    :
        `True` if the directory contains a ``.git`` entry.
    """
    return (path / ".git").exists()


def discover_repos(roots: Iterable[Union[str, Path]], depth: int = 2) -> List[Path]:
    """
    Find git working trees under a set of root directories.

    A root that is itself a working tree is returned directly; otherwise its
    subdirectories are searched. Descent stops at each working tree, so nested
    repositories such as submodules are not reported separately.

    Parameters
    ----------
    roots :
        Directories to search. ``~`` is expanded. Roots that do not exist are
        skipped silently.
    depth :
        How many levels below each root to search.

    Returns
    -------
    :
        Absolute paths of the working trees found, without duplicates.

    Examples
    --------

    ```python
    discover_repos(["~/github-backup/munch-group"], depth=1)
    ```
    """
    seen: Dict[Path, None] = {}
    for root in roots:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        if is_repo(root):
            seen[root.resolve()] = None
            continue
        stack = [(root, 0)]
        while stack:
            d, lvl = stack.pop()
            if lvl > depth:
                continue
            try:
                children = [
                    c for c in d.iterdir() if c.is_dir() and not c.name.startswith(".")
                ]
            except PermissionError:
                continue
            for c in children:
                if is_repo(c):
                    seen[c.resolve()] = None
                else:
                    stack.append((c, lvl + 1))
    return list(seen)


def tracked_blobs(repo: Path, subdir: str) -> Dict[str, Tuple[str, int]]:
    """
    List files tracked at HEAD under a directory, with blob shas and sizes.

    Only tracked files are reported, so untracked scratch output sitting in a
    results directory is invisible to the catalog: committing a file is the act
    of publishing it. Symbolic links are skipped.

    Parameters
    ----------
    repo :
        Working tree to inspect.
    subdir :
        Pathspec limiting the listing, for example ``:(icase)results``.

    Returns
    -------
    :
        A mapping of repository-relative path to ``(blob_sha, size)``. Sizes are
        those of the stored blob, so a Git LFS pointer reports the size of the
        pointer; use [](`labdata.gitutil.parse_lfs_pointer`) to resolve it.

    See Also
    --------
    [](`labdata.gitutil.last_commits`)
    """
    out = git(repo, "ls-files", "-s", "-z", "--", subdir, check=False)
    entries: Dict[str, str] = {}
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) < 3 or parts[0] == "120000":  # skip symlinks
            continue
        entries[path] = parts[1]
    if not entries:
        return {}
    sizes = _blob_sizes(repo, list(entries.values()))
    return {p: (sha, sizes.get(sha, -1)) for p, sha in entries.items()}


def _blob_sizes(repo: Path, shas: List[str]) -> Dict[str, int]:
    """
    Look up the size of many blobs in a single git call.

    Parameters
    ----------
    repo :
        Working tree to inspect.
    shas :
        Blob shas to size. Duplicates are collapsed.

    Returns
    -------
    :
        A mapping of blob sha to size in bytes, omitting shas git did not
        recognise.
    """
    stdin = "\n".join(dict.fromkeys(shas)) + "\n"
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch-check"],
        input=stdin.encode(),
        capture_output=True,
        check=False,
    )
    sizes: Dict[str, int] = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "blob":
            sizes[parts[0]] = int(parts[2])
    return sizes


def last_commits(repo: Path, subdir: str) -> Dict[str, Tuple[str, str, str, str]]:
    """
    Find the commit in which each file under a directory last changed.

    A single ``git log --name-only`` walk covers every file, which matters when
    a results directory holds hundreds of them.

    Parameters
    ----------
    repo :
        Working tree to inspect.
    subdir :
        Pathspec limiting the walk, for example ``:(icase)results``.

    Returns
    -------
    :
        A mapping of repository-relative path to
        ``(sha, short_sha, iso_date, subject)``.

    See Also
    --------
    [](`labdata.gitutil.file_history`)
    """
    fmt = f"{REC}%H{SEP}%h{SEP}%cI{SEP}%s"
    out = git(
        repo, "log", "--no-merges", f"--format={fmt}", "--name-only", "--", subdir,
        check=False,
    )
    result: Dict[str, Tuple[str, str, str, str]] = {}
    for chunk in out.split(REC):
        if not chunk.strip():
            continue
        header, _, body = chunk.partition("\n")
        try:
            sha, short, date, subject = header.split(SEP, 3)
        except ValueError:
            continue
        for path in body.splitlines():
            path = path.strip()
            if path and path not in result:  # first hit wins, so most recent
                result[path] = (sha, short, date, subject)
    return result


def file_history(repo: Path, path: str) -> List[Tuple[str, str, str, str]]:
    """
    List every commit that changed one file, newest first.

    Renames are followed, so history reaches back past a rename of the file.

    Parameters
    ----------
    repo :
        Working tree to inspect.
    path :
        Repository-relative path of the file.

    Returns
    -------
    :
        Tuples of ``(sha, short_sha, iso_date, subject)``, newest first.
    """
    fmt = f"%H{SEP}%h{SEP}%cI{SEP}%s"
    out = git(repo, "log", "--follow", f"--format={fmt}", "--", path, check=False)
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            sha, short, date, subject = line.split(SEP, 3)
        except ValueError:
            continue
        rows.append((sha, short, date, subject))
    return rows


def blob_at(repo: Path, rev: str, path: str) -> Optional[Tuple[str, int]]:
    """
    Find the blob sha and size of a file as of a revision.

    Parameters
    ----------
    repo :
        Working tree to inspect.
    rev :
        Revision to read the file at, typically a commit sha.
    path :
        Repository-relative path of the file.

    Returns
    -------
    :
        ``(blob_sha, size)``, or `None` if the file does not exist at `rev`.
    """
    out = git(repo, "ls-tree", "-l", rev, "--", path, check=False).strip()
    if not out:
        return None
    meta, _, _ = out.partition("\t")
    parts = meta.split()
    if len(parts) < 4:
        return None
    try:
        return parts[2], int(parts[3])
    except ValueError:
        return parts[2], -1


def tag_map(repo: Path) -> Dict[str, Tuple[str, ...]]:
    """
    Map commits to the tags pointing at them.

    Annotated tags are peeled to the commit they wrap. Tags decorate versions
    but never define them.

    Parameters
    ----------
    repo :
        Working tree to inspect.

    Returns
    -------
    :
        A mapping of commit sha to a tuple of tag names.
    """
    out = git(
        repo, "for-each-ref",
        "--format=%(objectname)%09%(*objectname)%09%(refname:short)",
        "refs/tags", check=False,
    )
    m: Dict[str, List[str]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        obj, peeled, name = parts
        target = peeled or obj
        m.setdefault(target, []).append(name)
    return {k: tuple(v) for k, v in m.items()}


def read_blob(repo: Path, blob_sha: str) -> bytes:
    """
    Read the content of a blob into memory.

    Only for content known to be small, such as a Git LFS pointer. Use
    [](`labdata.gitutil.write_blob_to`) for result files, which can be hundreds
    of megabytes.

    Parameters
    ----------
    repo :
        Working tree holding the blob.
    blob_sha :
        Blob sha to read.

    Returns
    -------
    :
        The raw content of the blob.
    """
    return git(repo, "cat-file", "blob", blob_sha, binary=True)


def write_blob_to(repo: Path, blob_sha: str, dest: Path) -> None:
    """
    Stream the content of a blob to a file.

    Git writes straight into `dest`, so peak memory does not grow with the size
    of the file.

    Parameters
    ----------
    repo :
        Working tree holding the blob.
    blob_sha :
        Blob sha to read.
    dest :
        File to write. It is removed again if git fails.

    Raises
    ------
    GitError
        If git cannot read the blob.
    """
    with open(dest, "wb") as fh:
        proc = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", blob_sha],
            stdout=fh, stderr=subprocess.PIPE, check=False,
        )
    if proc.returncode != 0:
        dest.unlink(missing_ok=True)
        raise GitError(
            f"cat-file blob {blob_sha} failed in {repo}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )


def parse_lfs_pointer(data: bytes) -> Optional[Tuple[str, int]]:
    """
    Read a Git LFS pointer file.

    Parameters
    ----------
    data :
        Raw content of a blob, which may or may not be a pointer.

    Returns
    -------
    :
        ``(sha256_oid, real_size)`` if `data` is a pointer, else `None`.

    Examples
    --------

    ```python
    parse_lfs_pointer(read_blob(repo, blob_sha))
    # ('9f2c...', 512189753)
    ```
    """
    if not data.startswith(LFS_PREFIX):
        return None
    oid = None
    size = None
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith("oid sha256:"):
            oid = line.split(":", 1)[1].strip()
        elif line.startswith("size "):
            try:
                size = int(line.split(None, 1)[1])
            except ValueError:
                pass
    if oid and size is not None:
        return oid, size
    return None


def lfs_object_path(repo: Path, oid: str) -> Optional[Path]:
    """
    Locate the real content of a Git LFS object in a repository.

    Parameters
    ----------
    repo :
        Working tree to look in.
    oid :
        The sha256 object id from the pointer file.

    Returns
    -------
    :
        Path of the object in the repository's LFS store, or `None` when the
        object has not been fetched.
    """
    p = repo / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    return p if p.exists() else None
