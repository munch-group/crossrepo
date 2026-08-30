"""
Building the catalog, resolving specs and fetching content.

There is no manifest in the producing repository. A file is in the catalog if it
is tracked by git, sits under a results directory, and matches the include and
exclude patterns. Its version is the commit in which it last changed.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from pathlib import Path
from typing import List, Optional

from . import cache, gitutil
from .config import Config, cache_root
from .model import Entry, Spec, Version

REMOTE_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$")
"""Pattern pulling ``owner`` and ``repo`` out of an ssh or https remote URL."""


def repo_identity(root: Path):
    """
    Determine the owner and name of a repository.

    The origin remote is authoritative, so a repository cloned into a directory
    of a different name, or mirrored under a directory named for a different
    organisation, is still identified correctly. Directory names are the
    fallback for a repository without a remote.

    Parameters
    ----------
    root :
        Working tree of the repository.

    Returns
    -------
    :
        ``(owner, repo)``. The owner is an empty string when neither the remote
        nor the parent directory supplies one.
    """
    url = gitutil.git(root, "config", "--get", "remote.origin.url", check=False).strip()
    if url:
        m = REMOTE_RE.search(url)
        if m:
            return m.group(1), m.group(2)
    parent = root.parent.name
    return (parent if parent not in ("", "/") else ""), root.name


def _wanted(name: str, size: int, cfg: Config) -> bool:
    """
    Test a file against the include, exclude and size filters.

    Parameters
    ----------
    name :
        File name without its directory.
    size :
        Size in bytes, or a negative number when unknown.
    cfg :
        Settings supplying the filters.

    Returns
    -------
    :
        `True` if the file belongs in the catalog.
    """
    if any(fnmatch.fnmatch(name, pat) for pat in cfg.exclude):
        return False
    if cfg.include and not any(fnmatch.fnmatch(name, pat) for pat in cfg.include):
        return False
    if cfg.min_bytes and 0 <= size < cfg.min_bytes:
        return False
    if cfg.max_bytes and size > cfg.max_bytes:
        return False
    return True


def _resolve_size(root: Path, blob_sha: str, size: int):
    """
    Look through a Git LFS pointer to the size of the real content.

    Only blobs small enough to be a pointer are read, so this costs nothing for
    ordinary files.

    Parameters
    ----------
    root :
        Working tree holding the blob.
    blob_sha :
        Blob sha to inspect.
    size :
        Size of the stored blob.

    Returns
    -------
    :
        ``(real_size, lfs_oid)``, where `lfs_oid` is `None` for a file not held
        in Git LFS.
    """
    if size < 0 or size > 1024:          # pointers are around 130 bytes
        return size, None
    data = gitutil.read_blob(root, blob_sha)
    ptr = gitutil.parse_lfs_pointer(data)
    if ptr:
        oid, real = ptr
        return real, oid
    return size, None


def scan_repo(root: Path, cfg: Config) -> List[Entry]:
    """
    Catalog the result files of one repository.

    Parameters
    ----------
    root :
        Working tree of the repository.
    cfg :
        Settings supplying the results directories and filters.

    Returns
    -------
    :
        One entry per result file, at its most recent version.

    See Also
    --------
    [](`labdata.core.build`)
    """
    owner, repo = repo_identity(root)
    tags = gitutil.tag_map(root)
    entries: List[Entry] = []
    seen = set()

    for results_dir in cfg.results_dirs:
        pathspec = f":(icase){results_dir}"
        blobs = gitutil.tracked_blobs(root, pathspec)
        if not blobs:
            continue
        commits = gitutil.last_commits(root, pathspec)

        for path, (blob_sha, size) in sorted(blobs.items()):
            if path in seen:
                continue
            name = path.split("/")[-1]
            real_size, lfs_oid = _resolve_size(root, blob_sha, size)
            if not _wanted(name, real_size, cfg):
                continue
            info = commits.get(path)
            if info is None:
                continue
            sha, short, date, subject = info
            seen.add(path)
            entries.append(
                Entry(
                    owner=owner,
                    repo=repo,
                    path=path,
                    root=root,
                    latest=Version(
                        sha=sha, short=short, date=date, subject=subject,
                        blob=blob_sha, size=real_size, lfs_oid=lfs_oid,
                        tags=tags.get(sha, ()),
                    ),
                )
            )
    return entries


def build(cfg: Optional[Config] = None) -> List[Entry]:
    """
    Catalog every repository under the configured roots.

    Repositories that cannot be read are skipped rather than failing the scan,
    so one broken working tree does not hide the rest.

    Parameters
    ----------
    cfg :
        Settings. Defaults to [](`labdata.config.Config.load`).

    Returns
    -------
    :
        Entries sorted by repository and path.

    Examples
    --------

    ```python
    entries = build(Config(roots=["~/github-backup/munch-group"], depth=1))
    len(entries)
    ```

    See Also
    --------
    [](`labdata.core.catalog`)
    """
    cfg = cfg or Config.load()
    entries: List[Entry] = []
    for root in gitutil.discover_repos(cfg.roots, cfg.depth):
        try:
            entries.extend(scan_repo(root, cfg))
        except gitutil.GitError:
            continue
    entries.sort(key=lambda e: (e.repo_key.lower(), e.path))
    return entries


def _cache_file() -> Path:
    """
    Location of the on-disk catalog.

    Returns
    -------
    :
        Path of ``catalog.json``, whether or not it exists.
    """
    return cache_root() / "catalog.json"


def save(entries: List[Entry]) -> Path:
    """
    Write a catalog to disk.

    Parameters
    ----------
    entries :
        Entries to store.

    Returns
    -------
    :
        The path written.
    """
    p = _cache_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"built": time.time(), "entries": [e.to_dict() for e in entries]}, indent=1
        ),
        encoding="utf-8",
    )
    return p


def load_cached(max_age: Optional[float] = None) -> Optional[List[Entry]]:
    """
    Read the catalog written by [](`labdata.core.save`).

    Parameters
    ----------
    max_age :
        Reject a catalog older than this many seconds. `None` accepts any age.

    Returns
    -------
    :
        The stored entries, or `None` when there is no catalog or it is too old.
    """
    p = _cache_file()
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    if max_age is not None and time.time() - data.get("built", 0) > max_age:
        return None
    out = []
    for d in data["entries"]:
        v = d["latest"]
        out.append(
            Entry(
                owner=d["owner"], repo=d["repo"], path=d["path"], root=Path(d["root"]),
                latest=Version(
                    sha=v["sha"], short=v["short"], date=v["date"],
                    subject=v["subject"], blob=v["blob"], size=v["size"],
                    lfs_oid=v.get("lfs_oid"), tags=tuple(v.get("tags", ())),
                ),
            )
        )
    return out


def catalog(
    refresh: bool = False, max_age: float = 3600.0, cfg: Optional[Config] = None
) -> List[Entry]:
    """
    Get the catalog, rescanning only when needed.

    Parameters
    ----------
    refresh :
        Rescan the repositories even if a fresh catalog is on disk.
    max_age :
        Age in seconds beyond which the stored catalog is rescanned.
    cfg :
        Settings. Defaults to [](`labdata.config.Config.load`).

    Returns
    -------
    :
        Entries sorted by repository and path.

    Examples
    --------

    ```python
    for entry in catalog():
        print(entry.spec, entry.latest.size)
    ```
    """
    if not refresh:
        cached = load_cached(max_age)
        if cached is not None:
            return cached
    entries = build(cfg)
    save(entries)
    return entries


def match(entries: List[Entry], spec: Spec) -> List[Entry]:
    """
    Find the entries a spec refers to, ignoring its version.

    The path matches on the full repository-relative path, on the bare file
    name, or on a trailing portion of the path, so a file can be named as
    briefly as is unambiguous.

    Parameters
    ----------
    entries :
        Catalog to search.
    spec :
        Reference to match.

    Returns
    -------
    :
        Matching entries, possibly none or several.
    """
    hits = []
    for e in entries:
        if e.repo.lower() != spec.repo.lower():
            continue
        if spec.owner and e.owner.lower() != spec.owner.lower():
            continue
        if e.path == spec.path or e.name == spec.path or e.path.endswith("/" + spec.path):
            hits.append(e)
    return hits


def resolve_one(entries: List[Entry], spec: Spec) -> Entry:
    """
    Find the single entry a spec refers to.

    Parameters
    ----------
    entries :
        Catalog to search.
    spec :
        Reference to resolve.

    Returns
    -------
    :
        The matching entry.

    Raises
    ------
    LookupError
        If nothing matches, or if several entries do. The message lists the
        candidates as full specs, so the ambiguity can be resolved by copying
        one of them.
    """
    hits = match(entries, spec)
    if not hits:
        near = sorted({e.repo_key for e in entries if spec.repo.lower() in e.repo.lower()})
        hint = f" Repos matching {spec.repo!r}: {', '.join(near)}." if near else ""
        raise LookupError(f"nothing in the catalog matches {spec}.{hint}")
    if len(hits) > 1:
        opts = "\n  ".join(h.spec for h in hits)
        raise LookupError(f"{spec} is ambiguous; be more specific:\n  {opts}")
    return hits[0]


def versions(entry: Entry) -> List[Version]:
    """
    List every version of one result file, newest first.

    Parameters
    ----------
    entry :
        Entry whose history to read.

    Returns
    -------
    :
        Versions newest first, each with the size the file had at that commit.

    Examples
    --------

    ```python
    for v in versions(entry):
        print(v.short, v.date[:10], v.size, v.subject)
    ```
    """
    tags = gitutil.tag_map(entry.root)
    out: List[Version] = []
    for sha, short, date, subject in gitutil.file_history(entry.root, entry.path):
        got = gitutil.blob_at(entry.root, sha, entry.path)
        if got is None:
            continue
        blob, size = got
        real_size, lfs_oid = _resolve_size(entry.root, blob, size)
        out.append(
            Version(sha=sha, short=short, date=date, subject=subject, blob=blob,
                    size=real_size, lfs_oid=lfs_oid, tags=tags.get(sha, ()))
        )
    return out


def find_version(entry: Entry, ref: str) -> Version:
    """
    Resolve a version reference against a file's history.

    Parameters
    ----------
    entry :
        Entry whose history to search.
    ref :
        A commit sha or unique prefix of one, a tag name, or ``latest`` for the
        most recent version.

    Returns
    -------
    :
        The matching version.

    Raises
    ------
    LookupError
        If no version matches. The message lists recent versions.
    """
    if ref in ("latest", "HEAD", ""):
        return entry.latest
    hist = versions(entry)
    for v in hist:
        if v.sha.startswith(ref) or v.short == ref or ref in v.tags:
            return v
    known = ", ".join(v.short for v in hist[:8])
    raise LookupError(f"no version {ref!r} of {entry.repo_key}:{entry.path}; known: {known}")


def fetch(
    spec_text: str, refresh: bool = False, cfg: Optional[Config] = None
) -> Path:
    """
    Get a local path to a result file, fetching it if it is not cached.

    Parameters
    ----------
    spec_text :
        A spec of the form ``[owner/]repo:path[@version]``. Without a version,
        the latest is used.
    refresh :
        Rescan the repositories before resolving the spec.
    cfg :
        Settings. Defaults to [](`labdata.config.Config.load`).

    Returns
    -------
    :
        Path of the file in the local cache, ready to pass to a reader.

    Raises
    ------
    ValueError
        If `spec_text` cannot be parsed.
    LookupError
        If the spec matches no entry, several entries, or no version.
    FileNotFoundError
        If the file is held in Git LFS and its content has not been fetched into
        the repository.

    Examples
    --------

    Read a result file from another project into a notebook:

    ```python
    import pandas as pd
    df = pd.read_csv(fetch("munch-group/xwas:hits.csv@e4f5a6b"))
    ```

    See Also
    --------
    [](`labdata.core.catalog`)
    """
    spec = Spec.parse(spec_text)
    entries = catalog(refresh=refresh, cfg=cfg)
    entry = resolve_one(entries, spec)
    version = find_version(entry, spec.version or "latest")
    return materialize(entry, version)


def materialize(entry: Entry, version: Version) -> Path:
    """
    Place one version of a result file in the cache and return its path.

    Content already cached is not fetched again, which is why an unchanged file
    costs nothing across versions and repositories. Git LFS content is taken
    from the repository's own object store.

    Parameters
    ----------
    entry :
        Entry the version belongs to.
    version :
        Version to materialize.

    Returns
    -------
    :
        Readable path of the cached content, under
        ``<cache>/files/<repo>/<version>/<name>``.

    Raises
    ------
    FileNotFoundError
        If the content is held in Git LFS and has not been fetched. The message
        names the ``git lfs fetch`` command to run.
    """
    key = version.lfs_oid or version.blob
    blob = cache.blob_path(key)
    if not blob.exists():
        if version.lfs_oid:
            src = gitutil.lfs_object_path(entry.root, version.lfs_oid)
            if src is None:
                raise FileNotFoundError(
                    f"{entry.repo_key}:{entry.path}@{version.short} is stored in Git LFS "
                    f"and its content is not present locally. Run:\n"
                    f"  git -C {entry.root} lfs fetch --all"
                )
            cache.store_from_file(key, src)
        else:
            tmp, final = cache.open_for_write(key)
            gitutil.write_blob_to(entry.root, version.blob, tmp)
            tmp.replace(final)
    dest = cache.readable_path(entry.repo_key, version.short, entry.name)
    return cache.link(blob, dest)
