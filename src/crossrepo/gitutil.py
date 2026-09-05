"""
Thin wrappers over git plumbing commands.

Everything here uses plumbing rather than porcelain, so the output does not
depend on the user's git configuration, aliases or locale. Nothing in this
module writes to a repository.

Every call names a [](`crossrepo.location.Location`) rather than a path, so the
same functions read a clone on this machine and a clone on a server reached over
ssh. Only the command line differs; the output being parsed is git's own either
way.
"""

from __future__ import annotations

import posixpath
import shutil
import warnings
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .config import SourceWarning
from .location import Location, execute, quote, run, warm

LINK_MODE = "120000"
"""The mode git gives a symbolic link, as it writes it in a tree or the index."""

SEP = "\x1f"
"""Field separator used in ``--format`` strings; cannot occur in a path."""

REC = "\x1e"
"""Record separator used to delimit commits in ``git log`` output."""

REMOTE_LISTING = (
    'if [ -e {root}/.git ]; then printf "%s\\n" {root}; '
    'else for d in {root}/*/; do '
    '[ -e "$d.git" ] && printf "%s\\n" "${{d%/}}"; '
    'done; fi; exit 0'
)
"""
Shell snippet listing the working trees at or directly inside one directory.

Written as one command because the cost of looking at a directory on another
machine is the round trip, not the looking. The trailing ``exit 0`` keeps a
subdirectory that is not a working tree -- the last test failing -- from being
reported as the whole command failing, which is reserved for a host that did not
answer. Hidden directories are left out by the glob, as they are locally.
"""

LFS_PREFIX = b"version https://git-lfs"
"""First bytes of a Git LFS pointer file."""

GIT_OPTS = ("-c", "core.quotePath=false")
"""
Options given to every git call.

``core.quotePath`` makes git print a path holding non-ASCII bytes as an octal
escape wrapped in quotes, so ``results/hojde.csv`` with a Danish o comes back
from ``git log --name-only`` looking nothing like the same path from
``git ls-files -z``, which is never quoted. Joining the two would then drop the
file from the catalog without a word. Turning it off here rather than at each
call site keeps that from coming back with the next command that prints a path.
"""


def _argv(repo: Union[Location, Path, str], *args: str) -> List[str]:
    """
    Build a git command line.

    Parameters
    ----------
    repo :
        Working tree to run in, on this machine or another.
    *args :
        Arguments passed to git, not including the command name itself.

    Returns
    -------
    :
        The full argument vector, carrying `GIT_OPTS`, wrapped in an ssh call
        when the working tree is on another machine.
    """
    repo = Location.of(repo)
    return repo.command(["git", "-C", repo.path, *GIT_OPTS, *args])


class GitError(RuntimeError):
    """Raised when a git command fails."""


def git(
    repo: Union[Location, Path, str], *args: str, binary: bool = False,
    check: bool = True,
) -> Union[str, bytes]:
    """
    Run a git command in a repository and return its standard output.

    Parameters
    ----------
    repo :
        Working tree to run the command in, on this machine or another.
    *args :
        Arguments passed to git, not including the command name itself.
    binary :
        Return raw bytes instead of decoded text. Use this for file content,
        which is not necessarily valid UTF-8.
    check :
        Raise [](`crossrepo.gitutil.GitError`) when git exits non-zero. When
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
    repo = Location.of(repo)
    proc = execute(_argv(repo, *args), remote=repo.is_remote)
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def is_repo(path: Union[Location, Path, str]) -> bool:
    """
    Test whether a directory is a git working tree.

    Parameters
    ----------
    path :
        Directory to test, on this machine or another.

    Returns
    -------
    :
        `True` if the directory contains a ``.git`` entry. A directory that
        cannot be looked at is not one: a cloud folder whose provider does not
        answer raises rather than returning, and a scan must not end because one
        directory on the way was unreachable. A host that cannot be reached is
        the same case.
    """
    loc = Location.of(path)
    if loc.is_remote:
        proc = execute(loc.shell(f"test -e {quote(loc.path)}/.git"), remote=True)
        return proc.returncode == 0
    try:
        return (Path(loc.path) / ".git").exists()
    except OSError:
        return False


def _subdirectories(path: Path) -> List[Path]:
    """
    List the directories inside one directory, skipping what cannot be read.

    Both the listing and the test of each entry can fail on their own, on a
    directory that is not readable, a mount that has gone away, or a synced
    folder that is not answering, so each is guarded separately.

    Parameters
    ----------
    path :
        Directory to list.

    Returns
    -------
    :
        Its subdirectories, ignoring hidden ones and anything unreadable.
    """
    try:
        children = list(path.iterdir())
    except OSError:
        return []
    out = []
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                out.append(child)
        except OSError:
            continue
    return out


def discover_repos(roots: Iterable[Union[Location, str, Path]]) -> List[Location]:
    """
    Find git working trees under a set of root directories.

    A root that is itself a working tree is returned directly; otherwise its
    immediate subdirectories are the working trees. Nothing deeper is looked at,
    which keeps the scan quick and predictable: a repository is either the root
    you named or something sitting directly in it.

    A root written ``user@host:path`` is looked at on that machine instead, in
    one round trip rather than one per directory. The connection to each host is
    opened first, so that whatever ssh asks for is asked once, before the scan
    starts printing.

    Parameters
    ----------
    roots :
        Directories to search. ``~`` is expanded, here for a local root and on
        the far side for a remote one. A root that does not exist, or that
        cannot be read, and a host that does not answer, are skipped with a
        [](`crossrepo.config.SourceWarning`), since there is nothing about an
        empty result to say which source was the problem.

    Returns
    -------
    :
        The working trees found, without duplicates, local ones as resolved
        paths.

    Examples
    --------

    ```python
    discover_repos(["~/github-backup/munch-group", "kmt@genome.au.dk:~/projects"])
    ```
    """
    seen: Dict[str, Location] = {}
    reached: Dict[str, Optional[str]] = {}
    for root in roots:
        loc = Location.of(root)
        if loc.is_remote:
            # One connection per host, opened before the listing, so that a
            # passphrase or a two-factor code is asked for once and only once.
            if loc.host not in reached:
                reached[loc.host] = warm(loc.host)
            if reached[loc.host] is not None:
                warnings.warn(
                    f"{loc}: {reached[loc.host]}", SourceWarning, stacklevel=2
                )
                continue
            for found in _remote_repos(loc):
                seen.setdefault(str(found), found)
            continue
        local = Path(loc.path).expanduser()
        try:
            if not local.is_dir():
                warnings.warn(
                    f"{loc}: no such directory", SourceWarning, stacklevel=2
                )
                continue
        except OSError as exc:
            warnings.warn(
                f"{loc}: {exc.strerror or exc}", SourceWarning, stacklevel=2
            )
            continue
        if is_repo(local):
            _remember(seen, local)
            continue
        for child in _subdirectories(local):
            if is_repo(child):
                _remember(seen, child)
    return list(seen.values())


def _remote_repos(root: Location) -> List[Location]:
    """
    List the working trees at or directly inside a directory on another machine.

    One command answers the whole question, because a round trip per directory
    is what makes a remote scan slow, not the work at either end.

    Parameters
    ----------
    root :
        Directory on the far side. Its ``~`` is expanded there.

    Returns
    -------
    :
        The working trees found, as absolute paths on that machine. Empty when
        there are none, and empty with a warning when the host does not answer.
    """
    quoted = quote(root.path)
    script = REMOTE_LISTING.format(root=quoted)
    proc = execute(root.shell(script), remote=True)
    if proc.returncode != 0:
        said = proc.stderr.decode("utf-8", "replace").strip()
        warnings.warn(
            f"{root}: {said or 'unreachable'}", SourceWarning, stacklevel=3
        )
        return []
    return [
        Location(path=line, host=root.host)
        for line in proc.stdout.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]


def _remember(seen: Dict[str, Location], path: Path) -> None:
    """
    Record a working tree under its real path.

    Parameters
    ----------
    seen :
        Mapping used as an ordered set of the working trees found.
    path :
        Working tree to record. It is resolved so that two routes to one
        repository are recorded once; a path that cannot be resolved is recorded
        as it stands.
    """
    try:
        path = path.resolve()
    except OSError:
        pass
    seen.setdefault(str(path), Location(path=str(path)))


def tracked_entries(
    repo: Union[Location, Path, str], subdir: Union[str, Sequence[str]]
) -> Dict[str, Tuple[str, int, str]]:
    """
    List everything tracked at HEAD under a directory, symbolic links included.

    Only tracked files are reported, so untracked scratch output sitting in a
    results directory is invisible to the catalog: committing a file is the act
    of publishing it.

    Parameters
    ----------
    repo :
        Working tree to inspect, on this machine or another.
    subdir :
        Pathspec limiting the listing, for example ``:(icase)results``, or
        several of them, in which case a file matching any is listed.

    Returns
    -------
    :
        A mapping of repository-relative path to ``(blob_sha, size, mode)``.
        A mode of `LINK_MODE` marks a symbolic link, whose blob holds the target
        path rather than any content and whose size is the length of that path.

    See Also
    --------
    [](`crossrepo.gitutil.tracked_blobs`)
    [](`crossrepo.gitutil.link_target`)
    """
    specs = [subdir] if isinstance(subdir, str) else list(subdir)
    out = git(repo, "ls-files", "-s", "-z", "--", *specs, check=False)
    entries: Dict[str, Tuple[str, str]] = {}
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) < 3:
            continue
        entries[path] = (parts[1], parts[0])
    if not entries:
        return {}
    sizes = _blob_sizes(repo, [sha for sha, _mode in entries.values()])
    return {
        p: (sha, sizes.get(sha, -1), mode) for p, (sha, mode) in entries.items()
    }


def tracked_blobs(
    repo: Union[Location, Path, str], subdir: Union[str, Sequence[str]]
) -> Dict[str, Tuple[str, int]]:
    """
    List files tracked at HEAD under a directory, with blob shas and sizes.

    Only tracked files are reported, so untracked scratch output sitting in a
    results directory is invisible to the catalog: committing a file is the act
    of publishing it. Symbolic links are skipped, having no content of their own;
    [](`crossrepo.gitutil.tracked_entries`) reports them.

    Parameters
    ----------
    repo :
        Working tree to inspect, on this machine or another.
    subdir :
        Pathspec limiting the listing, for example ``:(icase)results``, or
        several of them, in which case a file matching any is listed.

    Returns
    -------
    :
        A mapping of repository-relative path to ``(blob_sha, size)``. Sizes are
        those of the stored blob, so a Git LFS pointer reports the size of the
        pointer; use [](`crossrepo.gitutil.parse_lfs_pointer`) to resolve it.

    See Also
    --------
    [](`crossrepo.gitutil.head_commit`)
    """
    return {
        path: (sha, size)
        for path, (sha, size, mode) in tracked_entries(repo, subdir).items()
        if mode != LINK_MODE
    }


def _blob_sizes(repo: Union[Location, Path, str], shas: List[str]) -> Dict[str, int]:
    """
    Look up the size of many blobs in a single git call.

    Parameters
    ----------
    repo :
        Working tree to inspect, on this machine or another.
    shas :
        Blob shas to size. Duplicates are collapsed.

    Returns
    -------
    :
        A mapping of blob sha to size in bytes, omitting shas git did not
        recognise.
    """
    repo = Location.of(repo)
    stdin = "\n".join(dict.fromkeys(shas)) + "\n"
    proc = execute(
        _argv(repo, "cat-file", "--batch-check"),
        remote=repo.is_remote,
        stdin=stdin.encode(),
    )
    sizes: Dict[str, int] = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "blob":
            sizes[parts[0]] = int(parts[2])
    return sizes


def head_commit(repo: Union[Location, Path, str]) -> Optional[Tuple[str, str, str]]:
    """
    Read the commit a repository currently points at.

    Every published file is stamped with this, so one call covers a whole
    repository however many files it publishes.

    Parameters
    ----------
    repo :
        Working tree to inspect, on this machine or another.

    Returns
    -------
    :
        ``(sha, iso_date, subject)``, or `None` for a repository with no
        commits. The sha is full: it is the version key, and a full sha is what
        git and GitHub understand.
    """
    fmt = f"%H{SEP}%cI{SEP}%s"
    out = git(repo, "log", "-1", f"--format={fmt}", check=False).strip()
    if not out:
        return None
    parts = out.split(SEP, 2)
    return tuple(parts) if len(parts) == 3 else None


def file_history(
    repo: Union[Location, Path, str], path: str, follow: bool = True
) -> List[Tuple[str, str, str]]:
    """
    List every commit that changed one file, newest first.

    Parameters
    ----------
    repo :
        Working tree to inspect, on this machine or another.
    path :
        Repository-relative path of the file.
    follow :
        Follow renames, so history reaches back past a rename. Only meaningful
        for a single file; pass `False` for a directory, which ``--follow`` does
        not describe.

    Returns
    -------
    :
        Tuples of ``(sha, iso_date, subject)``, newest first.
    """
    fmt = f"%H{SEP}%cI{SEP}%s"
    args = ["log"] + (["--follow"] if follow else []) + [f"--format={fmt}", "--", path]
    out = git(repo, *args, check=False)
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            sha, date, subject = line.split(SEP, 2)
        except ValueError:
            continue
        rows.append((sha, date, subject))
    return rows


def entry_at(
    repo: Union[Location, Path, str], rev: str, path: str
) -> Optional[Tuple[str, int, str]]:
    """
    Find the blob sha, size and mode of a file as of a revision.

    Parameters
    ----------
    repo :
        Working tree to inspect, on this machine or another.
    rev :
        Revision to read the file at, typically a commit sha.
    path :
        Repository-relative path of the file.

    Returns
    -------
    :
        ``(blob_sha, size, mode)``, or `None` if the file does not exist at
        `rev`. A mode of `LINK_MODE` marks a symbolic link.

    See Also
    --------
    [](`crossrepo.gitutil.blob_at`)
    """
    out = git(repo, "ls-tree", "-l", rev, "--", path, check=False).strip()
    if not out:
        return None
    meta, _, _ = out.partition("\t")
    parts = meta.split()
    if len(parts) < 4:
        return None
    try:
        return parts[2], int(parts[3]), parts[0]
    except ValueError:
        return parts[2], -1, parts[0]


def blob_at(
    repo: Union[Location, Path, str], rev: str, path: str
) -> Optional[Tuple[str, int]]:
    """
    Find the blob sha and size of a file as of a revision.

    Parameters
    ----------
    repo :
        Working tree to inspect, on this machine or another.
    rev :
        Revision to read the file at, typically a commit sha.
    path :
        Repository-relative path of the file.

    Returns
    -------
    :
        ``(blob_sha, size)``, or `None` if the file does not exist at `rev`.

    See Also
    --------
    [](`crossrepo.gitutil.entry_at`)
    """
    got = entry_at(repo, rev, path)
    return None if got is None else (got[0], got[1])


def tree_at(repo: Union[Location, Path, str], rev: str, path: str) -> Optional[str]:
    """
    Find the tree sha of a directory as of a revision.

    A tree sha is a hash of the directory's whole content — every name, mode and
    blob beneath it — so it identifies a dataset split across many files exactly
    as a blob sha identifies a single file, and two identical datasets share one.

    Parameters
    ----------
    repo :
        Working tree to inspect, on this machine or another.
    rev :
        Revision to read at, typically a commit sha.
    path :
        Repository-relative path of the directory.

    Returns
    -------
    :
        The tree sha, or `None` when `path` is not a directory at `rev`.

    See Also
    --------
    [](`crossrepo.gitutil.tree_files`)
    """
    out = git(repo, "ls-tree", "-z", rev, "--", path, check=False)
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, _, _rest = rec.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "tree":
            return parts[2]
    return None


def tree_files(
    repo: Union[Location, Path, str], rev: str, path: str
) -> List[Tuple[str, str, int]]:
    """
    List every file beneath a directory as of a revision.

    Parameters
    ----------
    repo :
        Working tree to inspect, on this machine or another.
    rev :
        Revision to read at, typically a commit sha.
    path :
        Repository-relative path of the directory.

    Returns
    -------
    :
        Tuples of ``(path, blob_sha, size)``, sorted by path. Symbolic links are
        skipped, as they are elsewhere. Sizes are those of the stored blob, so a
        Git LFS pointer reports the size of the pointer.

    See Also
    --------
    [](`crossrepo.gitutil.tree_at`)
    """
    out = git(repo, "ls-tree", "-r", "-l", "-z", rev, "--", path, check=False)
    rows: List[Tuple[str, str, int]] = []
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, _, name = rec.partition("\t")
        parts = meta.split()
        if len(parts) < 4 or parts[1] != "blob" or parts[0] == "120000":
            continue
        try:
            size = int(parts[3])
        except ValueError:
            size = -1
        rows.append((name, parts[2], size))
    return sorted(rows)


def tag_map(repo: Union[Location, Path, str]) -> Dict[str, Tuple[str, ...]]:
    """
    Map commits to the tags pointing at them.

    Annotated tags are peeled to the commit they wrap. Tags decorate versions
    but never define them.

    Parameters
    ----------
    repo :
        Working tree to inspect, on this machine or another.

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


def read_blob(repo: Union[Location, Path, str], blob_sha: str) -> bytes:
    """
    Read the content of a blob into memory.

    Only for content known to be small, such as a Git LFS pointer. Use
    [](`crossrepo.gitutil.write_blob_to`) for result files, which can be hundreds
    of megabytes.

    Parameters
    ----------
    repo :
        Working tree holding the blob, on this machine or another.
    blob_sha :
        Blob sha to read.

    Returns
    -------
    :
        The raw content of the blob.
    """
    return git(repo, "cat-file", "blob", blob_sha, binary=True)


def write_blob_to(repo: Union[Location, Path, str], blob_sha: str, dest: Path) -> None:
    """
    Stream the content of a blob to a file.

    Git writes straight into `dest`, so peak memory does not grow with the size
    of the file.

    Parameters
    ----------
    repo :
        Working tree holding the blob, on this machine or another.
    blob_sha :
        Blob sha to read.
    dest :
        File to write. It is removed again if git fails.

    Raises
    ------
    GitError
        If git cannot read the blob.
    """
    repo = Location.of(repo)
    with open(dest, "wb") as fh:
        proc = execute(
            _argv(repo, "cat-file", "blob", blob_sha),
            remote=repo.is_remote, stdout=fh,
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


def lfs_object_path(
    repo: Union[Location, Path, str], oid: str
) -> Optional[Location]:
    """
    Locate the real content of a Git LFS object in a repository.

    An LFS object is a file in the repository rather than a blob in it, so it is
    the one thing that is looked for rather than asked of git.

    Parameters
    ----------
    repo :
        Working tree to look in, on this machine or another.
    oid :
        The sha256 object id from the pointer file.

    Returns
    -------
    :
        Location of the object in the repository's LFS store, or `None` when the
        object has not been fetched into that repository.

    See Also
    --------
    [](`crossrepo.gitutil.write_file_to`)
    """
    loc = Location.of(repo)
    obj = loc / f".git/lfs/objects/{oid[:2]}/{oid[2:4]}/{oid}"
    if loc.is_remote:
        proc = execute(loc.shell(f"test -f {quote(obj.path)}"), remote=True)
        return obj if proc.returncode == 0 else None
    return obj if Path(obj.path).exists() else None


def link_target(repo: Union[Location, Path, str], blob_sha: str) -> str:
    """
    Read where a committed symbolic link points.

    A symbolic link is stored as a blob whose content is the target path, so
    this is the committed target rather than whatever the working tree happens
    to hold, which is the same rule manifests are read by.

    Parameters
    ----------
    repo :
        Working tree holding the blob, on this machine or another.
    blob_sha :
        Blob sha of the link, as `tracked_entries` reports it for a path whose
        mode is `LINK_MODE`.

    Returns
    -------
    :
        The target as written, which may be relative to the directory the link
        sits in.

    See Also
    --------
    [](`crossrepo.gitutil.resolve_link`)
    """
    return read_blob(repo, blob_sha).decode("utf-8", "replace").strip()


def resolve_link(
    repo: Union[Location, Path, str], path: str, target: str
) -> Optional[Location]:
    """
    Locate the file a committed symbolic link points at.

    A relative target is resolved against the directory the link sits in, which
    is how the filesystem reads it. A target may leave the repository, by
    climbing out with ``..`` or by being absolute: publishing is opt-in through
    the manifest, so what a link reaches is something the repository said out
    loud, and a result written to scratch space outside the working tree is a
    normal thing to publish.

    Parameters
    ----------
    repo :
        Working tree holding the link, on this machine or another.
    path :
        Repository-relative path of the link itself.
    target :
        Target as committed, from [](`crossrepo.gitutil.link_target`).

    Returns
    -------
    :
        Location of the file, or `None` when nothing is there. A link resolves
        on the machine its repository is on, so a target read over ssh is looked
        for on the far side.

    See Also
    --------
    [](`crossrepo.gitutil.write_file_to`)
    """
    root = Location.of(repo)
    if not target:
        return None
    if posixpath.isabs(target):
        full = posixpath.normpath(target)
    else:
        rel = posixpath.join(posixpath.dirname(path), target)
        full = posixpath.normpath(posixpath.join(root.path, rel))
    loc = Location(path=full, host=root.host)
    if loc.is_remote:
        proc = execute(loc.shell(f"test -f {quote(full)}"), remote=True)
        return loc if proc.returncode == 0 else None
    return loc if Path(full).expanduser().is_file() else None


def write_file_to(src: Location, dest: Path) -> None:
    """
    Copy a file that git does not hold, such as a Git LFS object.

    Parameters
    ----------
    src :
        File to read, on this machine or another.
    dest :
        File to write. It is removed again if the copy fails.

    Raises
    ------
    GitError
        If the file cannot be read.
    """
    if not src.is_remote:
        try:
            shutil.copyfile(src.path, dest)
        except OSError as exc:
            dest.unlink(missing_ok=True)
            raise GitError(f"cannot read {src}: {exc}") from None
        return
    with open(dest, "wb") as fh:
        proc = run(src, ["cat", src.path], stdout=fh)
    if proc.returncode != 0:
        dest.unlink(missing_ok=True)
        raise GitError(
            f"cannot read {src}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
