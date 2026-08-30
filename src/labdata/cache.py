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

import os
import shutil
from pathlib import Path
from typing import Optional, Tuple

from .config import cache_root


def blob_path(sha: str, root: Optional[Path] = None) -> Path:
    """
    Location of a cached object.

    Parameters
    ----------
    sha :
        Git blob sha, or the sha256 object id for content held in Git LFS.
    root :
        Cache root. Defaults to [](`labdata.config.cache_root`).

    Returns
    -------
    :
        Path of the object, whether or not it exists.
    """
    root = root or cache_root()
    return root / "blobs" / sha[:2] / sha


def readable_path(
    repo_key: str, version: str, name: str, root: Optional[Path] = None
) -> Path:
    """
    Location of the readable hard link for a cached object.

    Parameters
    ----------
    repo_key :
        Repository identifier, as `labdata.model.Entry.repo_key`.
    version :
        Abbreviated commit sha of the version.
    name :
        File name to expose the content under.
    root :
        Cache root. Defaults to [](`labdata.config.cache_root`).

    Returns
    -------
    :
        Path of the link, whether or not it exists.
    """
    root = root or cache_root()
    return root / "files" / repo_key.replace("/", "__") / version / name


def open_for_write(sha: str, root: Optional[Path] = None) -> Tuple[Path, Path]:
    """
    Prepare to stream an object into the cache.

    Content is written to the temporary path and then renamed, so an interrupted
    write cannot leave a truncated object in the cache.

    Parameters
    ----------
    sha :
        Key to store the object under.
    root :
        Cache root. Defaults to [](`labdata.config.cache_root`).

    Returns
    -------
    :
        ``(temporary_path, final_path)``. Parent directories are created.

    See Also
    --------
    [](`labdata.gitutil.write_blob_to`)
    """
    dest = blob_path(sha, root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest.with_suffix(".tmp"), dest


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
        Cache root. Defaults to [](`labdata.config.cache_root`).

    Returns
    -------
    :
        Path of the cached object. An object already present is not rewritten.
    """
    dest = blob_path(sha, root)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
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
        Cache root. Defaults to [](`labdata.config.cache_root`).

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
    except OSError:
        tmp = dest.with_suffix(".tmp")
        shutil.copyfile(src, tmp)   # across filesystems: streamed, not buffered
        tmp.replace(dest)
    return dest


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
        Path to create. An existing path is left alone, since cached content is
        immutable.

    Returns
    -------
    :
        `dest`.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        return dest
    try:
        os.link(blob, dest)
    except OSError:
        try:
            dest.symlink_to(blob)
        except OSError:
            shutil.copyfile(blob, dest)
    return dest


def usage(root: Optional[Path] = None) -> Tuple[int, int]:
    """
    Measure how much the cache holds.

    Readable hard links are not counted, since they point at objects already
    counted.

    Parameters
    ----------
    root :
        Cache root. Defaults to [](`labdata.config.cache_root`).

    Returns
    -------
    :
        ``(object_count, total_bytes)``.
    """
    root = (root or cache_root()) / "blobs"
    n = total = 0
    for p in root.rglob("*"):
        if p.is_file():
            n += 1
            total += p.stat().st_size
    return n, total
