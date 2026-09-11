"""
Content-addressed local cache.

Files are stored under their git blob sha, which is a hash of the content. A
result file that did not change between two commits is therefore stored once,
and byte-identical files in different repositories are stored once. Cached
content is immutable, so a pinned version never changes underfoot.

A second layer of hard links under ``files/`` gives readable paths for the same
bytes, so a cached file can be found in a file browser.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Tuple

from .config import cache_root


def blob_path(sha: str, root: Optional[Path] = None) -> Path:
    """
    Location of a cached object.

    Parameters
    ----------
    sha :
        Git blob sha, or the sha256 object id for content held in Git LFS.
    root :
        Cache root. Defaults to [](`crossrepo.config.cache_root`).

    Returns
    -------
    :
        Path of the object, whether or not it exists.
    """
    root = root or cache_root()
    return root / "blobs" / sha[:2] / sha


def _temp_path(dest: Path) -> Path:
    """
    Name a temporary file for one writer of a cached object.

    Objects are keyed by content, so two processes fetching the same file agree
    on the destination and would, given one temporary name, write over each
    other and publish the interleaving. Naming the temporary file for the writer
    instead means each streams its own copy and the rename picks a winner; a
    rename is atomic, so a reader sees one whole object or the other, never a
    half written one.

    Parameters
    ----------
    dest :
        Final path of the object.

    Returns
    -------
    :
        A sibling path unique to this process and this call.
    """
    return dest.with_name(f"{dest.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def readable_path(
    repo_key: str, version: str, path: str, root: Optional[Path] = None
) -> Path:
    """
    Location of the readable hard link for a cached object.

    The whole repository-relative path is kept, not just the file name, because
    a file name is not unique within a repository. Two directories may hold
    files of the same name that last changed in the same commit, which gives
    them one version key, and naming the links by file name alone would then
    point both at whichever was fetched first.

    Parameters
    ----------
    repo_key :
        Repository identifier, as `crossrepo.model.Entry.repo_key`.
    version :
        Abbreviated commit sha of the version.
    path :
        Repository-relative path to expose the content under, for example
        ``results/sub/stable.csv``.
    root :
        Cache root. Defaults to [](`crossrepo.config.cache_root`).

    Returns
    -------
    :
        Path of the link, whether or not it exists.
    """
    root = root or cache_root()
    return root / "files" / repo_key.replace("/", "__") / version / path


def open_for_write(sha: str, root: Optional[Path] = None) -> Tuple[Path, Path]:
    """
    Prepare to stream an object into the cache.

    Content is written to the temporary path and then renamed, so an interrupted
    write cannot leave a truncated object in the cache. The temporary path is
    unique to the caller, so neither can a concurrent one.

    Parameters
    ----------
    sha :
        Key to store the object under.
    root :
        Cache root. Defaults to [](`crossrepo.config.cache_root`).

    Returns
    -------
    :
        ``(temporary_path, final_path)``. Parent directories are created. The
        temporary path differs on every call; the final path does not.

    See Also
    --------
    [](`crossrepo.gitutil.write_blob_to`)
    """
    dest = blob_path(sha, root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return _temp_path(dest), dest


def open_staging(root: Optional[Path] = None) -> Path:
    """
    Name a temporary path for content whose key is not known yet.

    [](`crossrepo.cache.open_for_write`) wants the key up front, which is the
    ordinary case: what is being fetched is already known by its hash. A part of
    a dataset published as a link is not. Nothing in the repository records the
    parts, so a part read over ssh is hashed as it arrives and only then has a
    name to be stored under.

    Parameters
    ----------
    root :
        Cache root. Defaults to [](`crossrepo.config.cache_root`).

    Returns
    -------
    :
        A path that does not exist, unique to this process and this call, beside
        the objects so that storing what lands there costs no copy. It ends in
        ``.tmp``, so [](`crossrepo.cache.objects`) passes it over, and it is the
        caller's to remove.
    """
    base = (root or cache_root()) / "blobs"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"staging.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"


def store(sha: str, data: bytes, root: Optional[Path] = None) -> Path:
    """
    Store an object held in memory.

    Parameters
    ----------
    sha :
        Key to store the object under.
    data :
        Content to store.
    root :
        Cache root. Defaults to [](`crossrepo.config.cache_root`).

    Returns
    -------
    :
        Path of the cached object. An object already present is not rewritten.
    """
    dest = blob_path(sha, root)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_path(dest)
    try:
        tmp.write_bytes(data)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def store_from_file(sha: str, src: Path, root: Optional[Path] = None) -> Path:
    """
    Store an object that already exists on disk, such as a Git LFS object.

    A hard link is used when the cache and `src` are on one filesystem, so
    nothing is copied. Otherwise the file is streamed, and a large file does not
    have to fit in memory.

    Parameters
    ----------
    sha :
        Key to store the object under.
    src :
        Existing file holding the content.
    root :
        Cache root. Defaults to [](`crossrepo.config.cache_root`).

    Returns
    -------
    :
        Path of the cached object. An object already present is not rewritten.
    """
    dest = blob_path(sha, root)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)          # same filesystem: free
    except FileExistsError:         # another writer got there first
        pass
    except OSError:
        tmp = _temp_path(dest)
        try:
            shutil.copyfile(src, tmp)   # across filesystems: streamed, not buffered
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
    return dest


def _already_exposes(dest: Path, blob: Path) -> bool:
    """
    Test whether a readable path already stands for a given cached object.

    Sharing an inode is proof, and covers both the hard link and the symbolic
    link cases. A separate file is judged by its size instead, which is what the
    copy fallback leaves behind and what a cache copied to another machine
    becomes. Size is enough there because a readable path is fixed by
    repository, version and repository-relative path, so it stands for exactly
    one object and cannot be holding a different one of the same size.

    Parameters
    ----------
    dest :
        Readable path to test.
    blob :
        Cached object it should stand for.

    Returns
    -------
    :
        `True` when `dest` may be reused as it is.
    """
    try:
        d = dest.stat()
        b = blob.stat()
    except OSError:                       # a dangling symlink, say
        return False
    if (d.st_ino, d.st_dev) == (b.st_ino, b.st_dev):
        return True
    return not dest.is_symlink() and d.st_size == b.st_size


def link(blob: Path, dest: Path) -> Path:
    """
    Expose a cached object under a readable name.

    A hard link is preferred, a symbolic link is the first fallback and a copy
    the last, so the function works on filesystems that support neither.

    Parameters
    ----------
    blob :
        Cached object to expose.
    dest :
        Path to create. An existing path is reused when it already stands for
        `blob`, cached content being immutable, and is replaced otherwise, so
        that a link left by an earlier cache layout is never trusted.

    Returns
    -------
    :
        `dest`.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (dest.exists() or dest.is_symlink()) and _already_exposes(dest, blob):
        return dest
    tmp = _temp_path(dest)
    try:
        try:
            os.link(blob, tmp)
        except OSError:
            try:
                tmp.symlink_to(blob)
            except OSError:
                shutil.copyfile(blob, tmp)
        os.replace(tmp, dest)       # atomic, so a reader never sees a gap
    finally:
        tmp.unlink(missing_ok=True)
    return dest


HEX = frozenset("0123456789abcdef")
"""Characters a content hash is written with."""


def objects(root: Optional[Path] = None) -> List[Path]:
    """
    List every object the store holds.

    Readable hard links are not listed, since they stand for objects listed
    already, and neither are the temporary files of writes still in flight.

    Parameters
    ----------
    root :
        Cache root. Defaults to [](`crossrepo.config.cache_root`).

    Returns
    -------
    :
        Paths of the stored objects, in a stable order.

    See Also
    --------
    [](`crossrepo.cache.check_object`)
    """
    base = (root or cache_root()) / "blobs"
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*") if p.is_file() and p.suffix != ".tmp")


def usage(root: Optional[Path] = None) -> Tuple[int, int]:
    """
    Measure how much the cache holds.

    Parameters
    ----------
    root :
        Cache root. Defaults to [](`crossrepo.config.cache_root`).

    Returns
    -------
    :
        ``(object_count, total_bytes)``.
    """
    files = objects(root)
    return len(files), sum(p.stat().st_size for p in files)


def _digest(path: Path, algorithm: str) -> str:
    """
    Hash the content of a file without holding it in memory.

    Parameters
    ----------
    path :
        File to hash.
    algorithm :
        ``sha1`` for a git blob id, which is taken over a header and the
        content, or ``sha256`` for a Git LFS object id, which is taken over the
        content alone.

    Returns
    -------
    :
        The digest in hex.
    """
    if algorithm == "sha1":
        # an identifier here, matching git's own scheme, not a security claim
        h = hashlib.sha1(usedforsecurity=False)
        h.update(f"blob {path.stat().st_size}\0".encode("ascii"))
    else:
        h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_hash(path: Path) -> str:
    """
    Hash a file the way a manifest stamp records it.

    This is the digest Git LFS keys an object by, taken over the content alone,
    so content published as a link and the same content committed to Git LFS
    share one cached object.

    Parameters
    ----------
    path :
        File to hash. Content is streamed, so a large file costs no memory.

    Returns
    -------
    :
        The sha256 digest in lower case hexadecimal.

    See Also
    --------
    [](`crossrepo.manifest.Stamp`)
    """
    return _digest(path, "sha256")


def tree_parts(directory: Path) -> List[Tuple[str, int, str]]:
    """
    List and hash every file a directory holds, as the parts of one dataset.

    Every regular file counts, at any depth. A symbolic link inside the
    directory is passed over rather than followed: what a link reaches is not
    the directory's own content, and following one is how a walk meets a cycle.
    The link that publishes the directory is a different matter and has already
    been followed by the time this is called.

    Parameters
    ----------
    directory :
        Directory to walk. A symbolic link to a directory is walked as the
        directory it names.

    Returns
    -------
    :
        ``(relative_path, size, sha256)`` for each part, ordered by path. Paths
        are written with forward slashes, so a dataset stamped on one kind of
        machine is read the same on another.

    See Also
    --------
    [](`crossrepo.cache.tree_hash`)
    """
    out: List[Tuple[str, int, str]] = []
    for here in _walk(directory):
        rel = here.relative_to(directory).as_posix()
        out.append((rel, here.stat().st_size, content_hash(here)))
    return sorted(out)


def _walk(directory: Path) -> Iterator[Path]:
    """
    Every regular file in a directory tree, links neither followed nor reported.

    Parameters
    ----------
    directory :
        Directory to walk.

    Yields
    ------
    :
        Each regular file found, in no particular order.
    """
    for here in sorted(directory.iterdir()):
        if here.is_symlink():
            continue
        if here.is_dir():
            yield from _walk(here)
        elif here.is_file():
            yield here


def tree_hash(parts: Iterable[Tuple[str, str]]) -> str:
    """
    Hash a directory the way a manifest stamp records it.

    A dataset published as a link needs one digest standing for the whole of it,
    since the manifest holds one stamp. It is taken over the parts rather than
    over a concatenation of their bytes, so that a part renamed or moved between
    subdirectories changes it as surely as a part rewritten does, and so that it
    can be recomputed on the far side of an ssh connection a part at a time
    without holding the dataset anywhere in one piece.

    Parameters
    ----------
    parts :
        ``(relative_path, sha256)`` for each part, in any order. The order here
        does not matter: they are sorted, so two machines that list a directory
        differently still agree on the digest.

    Returns
    -------
    :
        The sha256 digest in lower case hexadecimal.

    Examples
    --------

    ```python
    tree_hash([("part-1.parquet", "b" * 64), ("part-0.parquet", "a" * 64)])
    # '2d3f...'
    ```

    See Also
    --------
    [](`crossrepo.manifest.Stamp`)
    [](`crossrepo.cache.tree_parts`)
    """
    digest = hashlib.sha256()
    for path, sha in sorted(parts):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def check_object(path: Path) -> Optional[str]:
    """
    Test whether a cached object holds the content its key promises.

    The key is the file name, and says which hash to use: forty hex digits is a
    git blob id and sixty-four is a Git LFS object id. Content is streamed, so
    checking a large object costs no memory.

    Parameters
    ----------
    path :
        Object to check, as listed by [](`crossrepo.cache.objects`).

    Returns
    -------
    :
        `None` when the object is sound, else a short account of what is wrong
        with it.

    Examples
    --------

    ```python
    check_object(blob_path("6f5d1ec6703f83e4b69c0b0d3cc3912697ba93c1"))
    # None
    ```

    See Also
    --------
    [](`crossrepo.cache.discard`)
    """
    key = path.name
    if not key or not HEX.issuperset(key):
        return "not named for a content hash"
    if len(key) == 40:
        algorithm = "sha1"
    elif len(key) == 64:
        algorithm = "sha256"
    else:
        return "not named for a git blob id or a Git LFS object id"
    try:
        if _digest(path, algorithm) != key:
            return "content does not hash to its key"
    except OSError as exc:
        return f"cannot be read: {exc.strerror}"
    return None


def discard(path: Path, root: Optional[Path] = None) -> List[Path]:
    """
    Remove a cached object, and the readable links that stand for it.

    The links have to go too. A hard link keeps the content alive after the
    object is unlinked, and a readable path whose size still matches would then
    be reused in place of the content fetched to replace it. Links are found by
    inode, which covers the hard link and symbolic link cases; on a filesystem
    that supports neither, [](`crossrepo.cache.link`) leaves copies, and those are
    not found.

    Parameters
    ----------
    path :
        Object to remove.
    root :
        Cache root. Defaults to [](`crossrepo.config.cache_root`).

    Returns
    -------
    :
        The paths removed. The content is fetched again the next time it is
        asked for.
    """
    root = root or cache_root()
    removed: List[Path] = []
    try:
        target = path.stat()
    except OSError:
        return removed
    files = root / "files"
    if files.is_dir():
        for p in files.rglob("*"):
            try:
                st = p.stat()
            except OSError:
                continue                  # a link left dangling
            if (st.st_ino, st.st_dev) == (target.st_ino, target.st_dev):
                p.unlink()
                removed.append(p)
    path.unlink(missing_ok=True)
    removed.append(path)
    return removed
