"""
Building the catalog, resolving specs and fetching content.

A file is in the catalog if it is tracked by git, sits under the results
directory at the repository root, and is named by the ``crossrepo.yml`` there.
Publishing is therefore deliberate: a results directory with no manifest
publishes nothing. The include and exclude patterns then filter that on the
reading side, for someone who wants to see only part of what is published.

A file's version is the commit the repository points at. One lookup per
repository stamps everything it publishes, which is what keeps reading a
repository over the network cheap. A version therefore names a state of the
whole repository rather than of the one file, so it changes when anything in the
repository changes; what it addresses is still exactly the bytes that were
cataloged, because the content is read at that commit.

See Also
--------
[](`crossrepo.manifest.Manifest`)
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shutil
import sys
import time
import warnings
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from . import cache, gitutil, manifest, remote
from .config import Config, SourceWarning, active_config, cache_root
from .location import Location
from .model import Entry, Spec, Version

REMOTE_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$")
"""Pattern pulling ``owner`` and ``repo`` out of an ssh or https remote URL."""


def human(n: int) -> str:
    """
    Format a byte count for display.

    Parameters
    ----------
    n :
        Number of bytes. A negative number means the size is unknown.

    Returns
    -------
    :
        A short string such as ``948B`` or ``488.5M``, or ``?`` when unknown.

    Examples
    --------

    ```python
    human(512189753)
    # '488.5M'
    ```
    """
    if n < 0:
        return "?"
    size = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"



def progress_bar(items, description: str, enabled: bool):
    """
    Wrap an iterable in a progress bar when one is wanted.

    Scanning is one step per repository, so that is what the bar counts.
    ``tqdm.auto`` is used, which draws a widget in a notebook and a text bar in
    a terminal, so the same call suits both.

    Parameters
    ----------
    items :
        What is about to be iterated. Its length is the total.
    description :
        Label shown beside the bar.
    enabled :
        Whether to draw one at all. The caller decides: a command line hides it
        when its output is not a terminal, a notebook always shows it.

    Returns
    -------
    :
        `items`, wrapped when a bar is wanted and unchanged otherwise. A missing
        `tqdm` also leaves it unchanged rather than failing.
    """
    if not enabled or not items:
        return items
    try:
        from tqdm.auto import tqdm
    except ImportError:            # pragma: no cover - tqdm is a dependency
        return items
    return tqdm(items, desc=description, unit="repo", leave=False)


def repo_identity(root: Location):
    """
    Determine the owner and name of a repository.

    The origin remote is authoritative, so a repository cloned into a directory
    of a different name, or mirrored under a directory named for a different
    organisation, is still identified correctly. Directory names are the
    fallback for a repository without a remote.

    Parameters
    ----------
    root :
        Working tree of the repository, on this machine or another.

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


def selects(select: Optional[str], repo_key: str) -> bool:
    """
    Test whether a narrowed scan should look at a repository.

    The rule is the one [](`crossrepo.list`) filters with, so that narrowing a
    rescan and narrowing a listing pick the same repositories: the text is
    matched anywhere in ``owner/repo``, ignoring case. An owner alone is written
    ``owner/``, which cannot match a repository name.

    Parameters
    ----------
    select :
        Text to look for. `None` selects everything, which is what an ordinary
        scan does.
    repo_key :
        The repository, as ``owner/repo``, or as its name alone where no owner
        is known.

    Returns
    -------
    :
        Whether the repository is one to scan.

    See Also
    --------
    [](`crossrepo.core.build`)
    """
    return select is None or select.lower() in repo_key.lower()


def _repo_key(owner: str, repo: str) -> str:
    """
    Name a repository the way an entry does, before there is an entry.

    Parameters
    ----------
    owner :
        Owner, empty when the clone has no remote to say who owns it.
    repo :
        Repository name.

    Returns
    -------
    :
        ``owner/repo``, or the name alone when there is no owner, as
        [](`crossrepo.model.Entry.repo_key`) gives it.
    """
    return f"{owner}/{repo}" if owner else repo


def origin_slug(root: Location) -> str:
    """
    Read ``owner/repo`` from a local clone's GitHub origin.

    A local clone knows where it came from, so it can offer the same URLs as a
    repository read over the API, without any request being made.

    Parameters
    ----------
    root :
        Working tree of the repository.

    Returns
    -------
    :
        ``owner/repo`` when the origin remote is on GitHub, else an empty
        string.
    """
    url = gitutil.git(root, "config", "--get", "remote.origin.url", check=False).strip()
    if not url or "github.com" not in url:
        return ""
    m = REMOTE_RE.search(url)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def _has_object(root: Location, sha: str) -> bool:
    """
    Test whether a local clone already holds some content.

    Parameters
    ----------
    root :
        Working tree to look in.
    sha :
        Blob sha to look for.

    Returns
    -------
    :
        `True` when the object is present, so it can be read instead of
        downloaded. Any kind of object counts, since a dataset is keyed by the
        tree sha of its directory rather than by a blob sha.
    """
    if not str(root) or str(root) == ".":
        return False
    try:
        gitutil.git(root, "cat-file", "-e", sha)
    except gitutil.GitError:
        return False
    return True


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


def _resolve_size(root: Location, blob_sha: str, size: int):
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


def _read_manifest(root: Location, blobs, results_dir: str):
    """
    Read the one manifest governing a results directory.

    Only a ``crossrepo.yml`` sitting directly in the results directory is read, so
    there is one place to look to see what a repository publishes. A results
    directory is any path a repository is configured to publish from, however
    deep — ``results`` or ``analysis/step3/results`` alike — and the manifest
    must be in it, not below it. Manifests are read from git rather than from
    the working tree, so an uncommitted one publishes nothing and a repository
    read over the network behaves the same.

    Parameters
    ----------
    root :
        Working tree of the repository.
    blobs :
        Tracked files under the results directory, as
        [](`crossrepo.gitutil.tracked_blobs`) returns them.
    results_dir :
        Results directory being scanned, as configured, relative to the
        repository root. The repository may spell it with different
        capitalisation.

    Returns
    -------
    :
        ``(manifest, path)``, the manifest and the repository-relative path it
        was read from, which is carried on every entry it publishes because it
        is where a link's versions are read from. ``(None, "")`` when there is
        no manifest or it cannot be parsed; a manifest that will not parse costs
        its repository, with a warning, rather than the whole scan.

        A directory holding both accepted spellings is read in
        `crossrepo.manifest.MANIFEST_NAMES` order rather than alphabetical
        order, so ``crossrepo.yml`` wins over ``crossrepo.yaml``.
    """
    wanted = results_dir.strip("/").lower()
    here = []
    for path, (blob_sha, _size) in blobs.items():
        head, _, name = path.rpartition("/")
        if head.lower() == wanted and name in manifest.MANIFEST_NAMES:
            here.append((manifest.MANIFEST_NAMES.index(name), path, head, blob_sha))
    for _rank, path, head, blob_sha in sorted(here):
        try:
            text = gitutil.read_blob(root, blob_sha).decode("utf-8", "replace")
            return manifest.parse(text, head), path
        except (manifest.ManifestError, gitutil.GitError) as exc:
            warnings.warn(f"{root}: {exc}", stacklevel=2)
            return None, ""
    return None, ""


def _dataset_roots(blobs, governing) -> dict:
    """
    Find the directories a repository publishes as one dataset.

    A dataset is a directory a manifest names as though it were a file, which is
    how a table too large for one file — a ``.parquet`` directory split into
    parts to stay under GitHub's file size limit — is published as the single
    thing it actually is. Nothing about the suffix is special: naming a
    directory is what makes it a dataset.

    Parameters
    ----------
    blobs :
        Tracked files under the results directory, as
        [](`crossrepo.gitutil.tracked_blobs`) returns them.
    governing :
        The manifest for the results directory.

    Returns
    -------
    :
        Each dataset directory mapped to its description. A directory inside
        another dataset is left out, so a dataset is never split up.
    """
    directories = set()
    for path in blobs:
        d = posixpath.dirname(path)
        while d:
            directories.add(d)
            d = posixpath.dirname(d)
    named = {}
    for d in sorted(directories):
        if d == governing.directory:
            continue
        description = governing.describe(d)
        if description is not None:
            named[d] = description
    return {
        d: text
        for d, text in named.items()
        if not any(d.startswith(other + "/") for other in named)
    }


def _covering_dataset(path: str, roots) -> Optional[str]:
    """
    Find the dataset a file belongs to, if any.

    Parameters
    ----------
    path :
        Repository-relative path of the file.
    roots :
        Dataset directories, as [](`crossrepo.core._dataset_roots`) returns them.

    Returns
    -------
    :
        The dataset directory holding `path`, or `None` when the file stands on
        its own.
    """
    for d in roots:
        if path.startswith(d + "/"):
            return d
    return None


def _anchored_entries(root: Location, governing) -> dict:
    """
    List everything a manifest names from the repository root.

    A manifest publishes what sits in its own directory, and, with a key written
    from the root, anything else the repository tracks. Only the directories
    such keys point at are listed, so a manifest that names nothing outside its
    directory costs no extra work.

    Parameters
    ----------
    root :
        Working tree of the repository.
    governing :
        The manifest for the results directory.

    Returns
    -------
    :
        The files under those paths, as
        [](`crossrepo.gitutil.tracked_entries`) returns them, empty when the
        manifest names nothing outside its directory.
    """
    prefixes = governing.anchored_prefixes()
    if not prefixes:
        return {}
    # `:/` is the whole repository; `:(literal)` keeps a path with glob
    # characters in it from being read as a pattern by git.
    pathspecs = [f":(literal){p}" if p else ":/" for p in prefixes]
    return gitutil.tracked_entries(root, pathspecs)


def _regular(tracked: dict) -> dict:
    """
    Keep the tracked paths git holds content for.

    Parameters
    ----------
    tracked :
        Paths as [](`crossrepo.gitutil.tracked_entries`) returns them.

    Returns
    -------
    :
        The same mapping without the symbolic links, and without the modes, so
        that it is what the rest of the scan expects to read.
    """
    return {
        path: (sha, size)
        for path, (sha, size, mode) in tracked.items()
        if mode != gitutil.LINK_MODE
    }


def _links(tracked: dict) -> dict:
    """
    Keep the tracked paths that are symbolic links.

    Parameters
    ----------
    tracked :
        Paths as [](`crossrepo.gitutil.tracked_entries`) returns them.

    Returns
    -------
    :
        A mapping of repository-relative path to the blob sha of the link, whose
        content is the target path.
    """
    return {
        path: sha
        for path, (sha, _size, mode) in tracked.items()
        if mode == gitutil.LINK_MODE
    }


def scan_repo(root: Location, cfg: Config) -> List[Entry]:
    """
    Catalog the result files of one repository.

    Only files named by a committed ``crossrepo.yml`` are cataloged. A results
    directory without one contributes nothing, and neither does a file the
    manifest does not mention. A manifest may name any tracked file in the
    repository, not only the ones beneath it, by writing the path from the
    repository root.

    A symbolic link is cataloged when its manifest entry carries a stamp, which
    is what says which content the link stands for; git holds only the link. A
    link with no stamp is passed over with a warning, since nothing would be
    checking what it resolved to. Links inside a dataset directory are not
    published: a stamp names one file, so a directory of them has nothing to
    say what it holds.

    Parameters
    ----------
    root :
        Working tree of the repository.
    cfg :
        Settings supplying the results directories and the reading side filters.

    Returns
    -------
    :
        One entry per published result file, at its most recent version.

    See Also
    --------
    [](`crossrepo.core.build`)
    [](`crossrepo.manifest.Manifest`)
    """
    owner, repo = repo_identity(root)
    slug = origin_slug(root)
    source = "ssh" if root.is_remote else "local"
    head = gitutil.head_commit(root)
    if head is None:
        return []
    tags = gitutil.tag_map(root)
    entries: List[Entry] = []
    seen = set()

    for results_dir in cfg.crossrepo_dirs:
        wanted = results_dir.strip("/")
        if not wanted:
            continue
        pathspec = f":(icase){wanted}"
        tracked = gitutil.tracked_entries(root, pathspec)
        blobs = _regular(tracked)
        if not blobs:
            continue                          # nor, then, is there a manifest
        governing, where = _read_manifest(root, blobs, wanted)
        if governing is None:
            continue
        tracked.update(_anchored_entries(root, governing))
        blobs = _regular(tracked)
        datasets = _dataset_roots(blobs, governing)

        for directory, description in sorted(datasets.items()):
            if directory in seen:
                continue
            entry = _dataset_entry(
                root, owner, repo, directory, description, blobs, head, tags,
                cfg, slug, source, where,
            )
            if entry is not None:
                seen.add(directory)
                entries.append(entry)

        for path, (blob_sha, size) in sorted(blobs.items()):
            if path in seen or manifest.is_manifest(path):
                continue
            if _covering_dataset(path, datasets) is not None:
                continue                      # a part of a dataset, not a file
            description = governing.describe(path)
            if description is None:
                continue                      # committed, but not published
            if governing.stamp(path) is not None:
                warnings.warn(
                    f"{root}: {where} stamps {path}, which git holds the "
                    f"content of; the stamp is ignored, git's own version of "
                    f"the file being the better answer. A stamp belongs on a "
                    f"symbolic link.",
                    stacklevel=2,
                )
            name = path.split("/")[-1]
            real_size, lfs_oid = _resolve_size(root, blob_sha, size)
            if not _wanted(name, real_size, cfg):
                continue
            sha, date, subject = head
            seen.add(path)
            entries.append(
                Entry(
                    owner=owner,
                    repo=repo,
                    path=path,
                    root=root,
                    description=description,
                    remote=slug,
                    source=source,
                    manifest=where,
                    latest=Version(
                        sha=sha, date=date, subject=subject,
                        blob=blob_sha, size=real_size, lfs_oid=lfs_oid,
                        tags=tags.get(sha, ()),
                    ),
                )
            )

        for path, blob_sha in sorted(_links(tracked).items()):
            if path in seen:
                continue
            if _covering_dataset(path, datasets) is not None:
                continue                      # inside a dataset, not published
            description = governing.describe(path)
            if description is None:
                continue                      # committed, but not published
            stamp = governing.stamp(path)
            if stamp is None:
                warnings.warn(
                    f"{root}: {path} is published as a symbolic link but {where} "
                    f"gives it no stamp, so nothing says which content it "
                    f"stands for. Run `crossrepo stamp` in the repository and "
                    f"commit the result.",
                    stacklevel=2,
                )
                continue
            name = path.split("/")[-1]
            if not _wanted(name, stamp.size, cfg):
                continue
            sha, date, subject = head
            seen.add(path)
            # One read per link, after the filters, so a link that is not
            # published costs nothing. Reading them together would be one round
            # trip rather than several, which would matter if a repository
            # published many; a link is for a file too large to commit, and
            # there are only ever a few of those.
            entries.append(
                Entry(
                    owner=owner,
                    repo=repo,
                    path=path,
                    root=root,
                    description=description,
                    remote=slug,
                    source=source,
                    manifest=where,
                    latest=Version(
                        sha=sha, date=date, subject=subject,
                        blob=stamp.sha256, size=stamp.size, lfs_oid=None,
                        tags=tags.get(sha, ()),
                        link=gitutil.link_target(root, blob_sha),
                    ),
                )
            )
    return entries


def _dataset_entry(
    root: Location, owner: str, repo: str, directory: str, description: str,
    blobs, head, tags, cfg: Config, slug: str = "", source: str = "local",
    where: str = "",
) -> Optional[Entry]:
    """
    Build the single catalog entry standing for a directory of files.

    The version is the repository's own, as it is for a single file. The size is
    the total over the parts, and the content key is the directory's git tree
    sha.

    Parameters
    ----------
    root :
        Working tree of the repository.
    owner :
        Repository owner.
    repo :
        Repository name.
    directory :
        Repository-relative path of the dataset.
    description :
        What the manifest says the dataset holds.
    blobs :
        Tracked files under the results directory.
    head :
        The repository's current commit, as
        [](`crossrepo.gitutil.head_commit`) reads it.
    tags :
        Tags by commit sha.
    cfg :
        Settings supplying the reading side filters.
    slug :
        ``owner/repo`` when the repository is on GitHub, else empty.
    source :
        Where the entry is being cataloged from, ``local`` or ``ssh``.
    where :
        Repository-relative path of the manifest publishing the dataset.

    Returns
    -------
    :
        The entry, or `None` when the dataset holds nothing, is filtered out, or
        has no history git will report.
    """
    prefix = directory + "/"
    total = 0
    parts = 0
    for path, (blob_sha, size) in blobs.items():
        if not path.startswith(prefix) or manifest.is_manifest(path):
            continue
        real_size, _lfs_oid = _resolve_size(root, blob_sha, size)
        if real_size > 0:
            total += real_size
        parts += 1
    if not parts:
        return None
    if not _wanted(directory.split("/")[-1], total, cfg):
        return None
    tree = gitutil.tree_at(root, "HEAD", directory)
    if tree is None:
        return None
    sha, date, subject = head
    return Entry(
        owner=owner,
        repo=repo,
        path=directory,
        root=root,
        description=description,
        remote=slug,
        source=source,
        manifest=where,
        latest=Version(
            sha=sha, date=date, subject=subject,
            blob=tree, size=total, lfs_oid=None, parts=parts,
            tags=tags.get(sha, ()),
        ),
    )


def build(
    cfg: Optional[Config] = None, progress: bool = False,
    select: Optional[str] = None,
) -> List[Entry]:
    """
    Catalog every repository under the configured roots.

    Repositories that cannot be read are skipped rather than failing the scan,
    so one broken working tree does not hide the rest.

    Parameters
    ----------
    cfg :
        Settings. Defaults to [](`crossrepo.config.active_config`): what
        [](`crossrepo.config.use_config`) registered, or the configuration file.
    progress :
        Show a progress bar, one step per repository. Scanning an organisation
        is otherwise silent for as long as it takes.
    select :
        Scan only the repositories this names, by [](`crossrepo.core.selects`).
        A clone is identified before it is read and an organisation is listed
        but not read into, so what is skipped costs nothing beyond finding out
        that it is there. The result is then part of a catalog rather than a
        whole one, which is [](`crossrepo.core.catalog`)'s business to put back
        together.

    Returns
    -------
    :
        Entries sorted by repository and path.

    Examples
    --------

    ```python
    entries = build(Config(roots=["~/github-backup/munch-group"]))
    len(entries)
    ```

    See Also
    --------
    [](`crossrepo.core.catalog`)
    [](`crossrepo.core.selects`)
    """
    cfg = active_config() if cfg is None else cfg
    clones: List[Entry] = []
    roots = gitutil.discover_repos(cfg.roots)
    if select is not None:
        roots = [r for r in roots if selects(select, _repo_key(*repo_identity(r)))]
    for root in progress_bar(roots, "scanning clones", progress):
        try:
            clones.extend(scan_repo(root, cfg))
        except gitutil.GitError as exc:
            warnings.warn(f"{root}: {exc}", SourceWarning, stacklevel=2)
    fetched = remote.build(cfg, progress=progress, select=select)
    entries = _merge(_nearest(clones), fetched)
    entries.sort(key=lambda e: (e.repo_key.lower(), e.path))
    return entries


def _nearest(entries: List[Entry]) -> List[Entry]:
    """
    Keep one clone per file when a repository is checked out more than once.

    A repository can be both on this machine and on a server named in `roots`.
    Either can supply the file, and the version each shows is only as current as
    its last pull, so the clone on this machine is preferred: reading from it
    costs nothing, where reading over ssh costs a round trip per call.

    Parameters
    ----------
    entries :
        Entries from every clone that was scanned.

    Returns
    -------
    :
        One entry per file, unsorted.
    """
    best: Dict[Tuple[str, str], Entry] = {}
    for entry in entries:
        key = (entry.repo_key.lower(), entry.path)
        already = best.get(key)
        if already is None or (already.source == "ssh" and entry.source == "local"):
            best[key] = entry
    return list(best.values())


def _merge(local: List[Entry], fetched: List[Entry]) -> List[Entry]:
    """
    Combine what local clones publish with what GitHub publishes.

    Where both know a file, GitHub decides the version, because a clone is only
    as current as its last pull, while the local working tree is kept as a place
    to read content from: content already in a clone is read rather than
    downloaded, and the cache being keyed by content means either source lands
    on the same object.

    Parameters
    ----------
    local :
        Entries from local clones.
    fetched :
        Entries read from GitHub.

    Returns
    -------
    :
        One entry per file, unsorted.
    """
    if not fetched:
        return list(local)
    by_key = {(e.repo_key.lower(), e.path): e for e in local}
    out = []
    for entry in fetched:
        known = by_key.pop((entry.repo_key.lower(), entry.path), None)
        if known is not None:
            entry = replace(entry, root=known.root)
        out.append(entry)
    out.extend(by_key.values())
    return out


def diagnose(cfg: Optional[Config] = None) -> List[str]:
    """
    Explain why a scan found nothing.

    An empty catalog has several ordinary causes, and they look identical from
    the outside: a root that does not exist, repositories that have no results
    directory, results directories with no manifest, or simply no GitHub owner
    configured. This reports which it is, reading only local git and making no
    request.

    Parameters
    ----------
    cfg :
        Settings to account for. Defaults to
        [](`crossrepo.config.active_config`).

    Returns
    -------
    :
        Lines describing what was looked at and what was missing.

    Examples
    --------

    ```python
    print("\n".join(diagnose()))
    ```
    """
    cfg = active_config() if cfg is None else cfg
    lines: List[str] = []
    if cfg.roots:
        for spelled in cfg.roots:
            loc = Location.parse(spelled)
            here = loc.local_path()
            if here is not None and not here.is_dir():
                lines.append(f"  {spelled}: no such directory")
                continue
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                found = gitutil.discover_repos([loc])
            if caught:
                # The warning names the source itself, and may go on to say
                # what to do about it, which is set apart as it is elsewhere.
                said = str(caught[0].message).replace("\n", "\n    ")
                lines.append(f"  {said}")
                continue
            with_results = 0
            with_manifest = 0
            for repo in found:
                names = _results_listing(repo, cfg)
                if not names:
                    continue
                with_results += 1
                if any(manifest.is_manifest(n) and n.count("/") == 1 for n in names):
                    with_manifest += 1
            lines.append(
                f"  {spelled}: {len(found)} repos, {with_results} with a results "
                f"directory, {with_manifest} with results/crossrepo.yml"
            )
    else:
        lines.append("  roots: none configured, so nothing local is scanned")
    if cfg.owners or cfg.repos:
        named = ", ".join(list(cfg.owners) + list(cfg.repos))
        lines.append(f"  github: {named}")
    else:
        lines.append(
            "  github: no owners or repos configured, so nothing is read from GitHub"
        )
    return lines


def _results_listing(root: Location, cfg: Config) -> List[str]:
    """
    List the tracked paths under a repository's results directories.

    Cheaper than [](`crossrepo.gitutil.tracked_blobs`), which also sizes every
    blob, and enough to say whether a repository has anything to publish.

    Parameters
    ----------
    root :
        Working tree to inspect.
    cfg :
        Settings supplying the results directories.

    Returns
    -------
    :
        Repository-relative paths, empty when there is no results directory.
    """
    out: List[str] = []
    for results_dir in cfg.crossrepo_dirs:
        listed = gitutil.git(
            root, "ls-files", "-z", "--", f':(icase){results_dir.strip("/")}',
            check=False,
        )
        out.extend(p for p in listed.split("\0") if p)
    return out


def _cache_file() -> Path:
    """
    Location of the on-disk catalog.

    Returns
    -------
    :
        Path of ``catalog.json``, whether or not it exists.
    """
    return cache_root() / "catalog.json"


def fingerprint(cfg: Config) -> str:
    """
    Summarise the settings that decide what a catalog contains.

    A stored catalog is only reusable for the settings that produced it, and
    nothing about a catalog file says which those were. This gives the stored
    catalog a key, so that pointing `roots` somewhere else takes effect at once
    instead of after the stored catalog has aged out.

    Parameters
    ----------
    cfg :
        Settings to summarise. Only the fields that change which files are
        cataloged are used, and ``~`` in `roots` is expanded first so that two
        spellings of one directory agree.

    Returns
    -------
    :
        A short hex digest.

    See Also
    --------
    [](`crossrepo.core.catalog`)
    """
    payload = json.dumps(
        {
            "roots": sorted(str(Location.parse(r).resolved()) for r in cfg.roots),
            "owners": sorted(cfg.owners),
            "repos": sorted(cfg.repos),
            "crossrepo_dirs": list(cfg.crossrepo_dirs),
            "include": list(cfg.include),
            "exclude": list(cfg.exclude),
            "min_bytes": cfg.min_bytes,
            "max_bytes": cfg.max_bytes,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def save(entries: List[Entry], cfg: Optional[Config] = None) -> Path:
    """
    Write a catalog to disk.

    The file is written beside its final name and then renamed, so an
    interrupted write leaves the previous catalog in place rather than a
    truncated one.

    Parameters
    ----------
    entries :
        Entries to store.
    cfg :
        Settings the entries were built with, recorded so that
        [](`crossrepo.core.load_cached`) can tell whether they still apply.
        Defaults to [](`crossrepo.config.active_config`).

    Returns
    -------
    :
        The path written.
    """
    cfg = active_config() if cfg is None else cfg
    p = _cache_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(
            {
                "built": time.time(),
                "config": fingerprint(cfg),
                "entries": [e.to_dict() for e in entries],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    tmp.replace(p)
    return p


def load_cached(
    max_age: Optional[float] = None, cfg: Optional[Config] = None
) -> Optional[List[Entry]]:
    """
    Read the catalog written by [](`crossrepo.core.save`).

    A catalog that cannot be read is treated as absent rather than as an error,
    so an interrupted write or a change of format costs a rescan instead of
    making every command fail until the file is deleted by hand.

    Parameters
    ----------
    max_age :
        Reject a catalog older than this many seconds. `None` accepts any age.
    cfg :
        Settings the caller intends to use. A catalog built with different
        settings is rejected. `None` accepts a catalog whatever it was built
        with.

    Returns
    -------
    :
        The stored entries, or `None` when there is no usable catalog.
    """
    p = _cache_file()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if max_age is not None and time.time() - data.get("built", 0) > max_age:
            return None
        if cfg is not None and data.get("config") != fingerprint(cfg):
            return None
        out = []
        for d in data["entries"]:
            v = d["latest"]
            out.append(
                Entry(
                    owner=d["owner"], repo=d["repo"], path=d["path"],
                    root=Location.parse(d["root"]),
                    description=d.get("description", ""),
                    remote=d.get("remote", ""), source=d.get("source", "local"),
                    manifest=d.get("manifest", ""),
                    latest=Version(
                        sha=v["sha"], date=v["date"],
                        subject=v["subject"], blob=v["blob"], size=v["size"],
                        lfs_oid=v.get("lfs_oid"), parts=v.get("parts", 0),
                        tags=tuple(v.get("tags", ())),
                        link=v.get("link"),
                    ),
                )
            )
    except (OSError, ValueError, TypeError, KeyError):
        return None                       # truncated, or written by another version
    return out


def catalog(
    refresh: bool = False, max_age: float = 3600.0, cfg: Optional[Config] = None,
    progress: bool = False, select: Optional[str] = None,
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
        Settings. Defaults to [](`crossrepo.config.active_config`): what
        [](`crossrepo.config.use_config`) registered, or the configuration file. A stored catalog
        built with different settings is rescanned rather than reused.
    progress :
        Show a progress bar while rescanning. Nothing is drawn when the stored
        catalog is used, since there is nothing to wait for.
    select :
        Rescan only the repositories this names, by
        [](`crossrepo.core.selects`), and keep the stored catalog for the rest.
        The whole catalog is returned and stored, with the named repositories
        as they are now: rescanning one repository must not lose the others,
        which are not being looked at rather than known to be gone. Ignored
        unless `refresh` is set, since a stored catalog is read whole.

    Returns
    -------
    :
        Entries sorted by repository and path. Where `select` is given and
        there is no stored catalog to update, what was scanned is returned with
        a [](`crossrepo.config.SourceWarning`) and nothing is stored: storing it
        would leave a catalog holding one repository and claiming to hold them
        all.

    Examples
    --------

    ```python
    for entry in catalog():
        print(entry.spec, entry.latest.size)

    catalog(refresh=True, select="munch-group/x-gwas")   # just that one
    ```

    See Also
    --------
    [](`crossrepo.core.selects`)
    """
    cfg = active_config() if cfg is None else cfg
    if not refresh:
        cached = load_cached(max_age, cfg)
        if cached is not None:
            return cached
    if refresh and select is not None:
        return _refresh_some(cfg, select, progress)
    entries = build(cfg, progress=progress)
    save(entries, cfg)
    return entries


def _refresh_some(cfg: Config, select: str, progress: bool = False) -> List[Entry]:
    """
    Rescan some repositories, leaving the stored catalog for the rest alone.

    Parameters
    ----------
    cfg :
        Settings. The whole of them: what is stored stays keyed to the settings
        that describe the whole catalog, so that a later listing still finds it.
    select :
        Which repositories to rescan, by [](`crossrepo.core.selects`).
    progress :
        Show a progress bar while rescanning.

    Returns
    -------
    :
        The whole catalog, with those repositories as they are now.
    """
    stored = load_cached(None, cfg)
    entries = build(cfg, progress=progress, select=select)
    if stored is None:
        warnings.warn(
            f"nothing was stored: there is no catalog to update {select!r} in. "
            "Refresh without naming an owner or a repo to catalog everything.",
            SourceWarning, stacklevel=2,
        )
        return entries
    kept = [e for e in stored if not selects(select, e.repo_key)]
    merged = kept + entries
    merged.sort(key=lambda e: (e.repo_key.lower(), e.path))
    save(merged, cfg)
    return merged


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


def _stamp_at(entry: Entry, rev: str):
    """
    Read the stamp a manifest gave one link at some commit.

    Parameters
    ----------
    entry :
        Entry for the link, carrying the manifest that publishes it.
    rev :
        Commit to read at.

    Returns
    -------
    :
        ``(stamp, target)``, or `None` when the manifest is not there at `rev`,
        will not parse, does not stamp this file, or the file is not a symbolic
        link at that commit. A manifest that will not parse is treated as
        saying nothing, so one bad commit costs its own version rather than the
        whole history.
    """
    if not entry.manifest:
        return None
    got = gitutil.entry_at(entry.root, rev, entry.manifest)
    if got is None:
        return None
    try:
        text = gitutil.read_blob(entry.root, got[0]).decode("utf-8", "replace")
        governing = manifest.parse(text, posixpath.dirname(entry.manifest))
    except (manifest.ManifestError, gitutil.GitError):
        return None
    stamp = governing.stamp(entry.path)
    if stamp is None:
        return None
    link = gitutil.entry_at(entry.root, rev, entry.path)
    if link is None or link[2] != gitutil.LINK_MODE:
        return None
    return stamp, gitutil.link_target(entry.root, link[0])


def _link_versions(entry: Entry, tags) -> List[Version]:
    """
    List the versions of a file published as a link, newest first.

    Parameters
    ----------
    entry :
        Entry for the link.
    tags :
        Tags by commit sha, as [](`crossrepo.gitutil.tag_map`) reads them.

    Returns
    -------
    :
        One version per commit in which the stamp changed. A commit that touched
        the manifest without changing this file's stamp is not a version of this
        file, and the commit kept for a run of equal stamps is the oldest, being
        the one that introduced the content.
    """
    if not entry.manifest:
        return []
    # The manifest is read at every commit that touched it. That is more work
    # than reading one blob per version, but it is the only record of the bytes,
    # and a manifest is small and rarely written.
    rows = []
    for sha, date, subject in gitutil.file_history(
        entry.root, entry.manifest, follow=False
    ):
        got = _stamp_at(entry, sha)
        if got is not None:
            rows.append((sha, date, subject, got[0], got[1]))
    out: List[Version] = []
    for i, (sha, date, subject, stamp, target) in enumerate(rows):
        older = rows[i + 1] if i + 1 < len(rows) else None
        if older is not None and older[3].sha256 == stamp.sha256:
            continue                        # this commit left the content alone
        out.append(
            Version(sha=sha, date=date, subject=subject, blob=stamp.sha256,
                    size=stamp.size, link=target, tags=tags.get(sha, ()))
        )
    return out


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
        For a dataset published as a directory, each version carries the tree
        sha, the total size and the number of parts at that commit.

    Notes
    -----
    These are the commits in which the file itself changed, which is the useful
    set to pin. The catalog stamps a file with the repository's current commit
    instead, so the newest entry here need not be the one the catalog shows.

    For a file published as a link, git has no history of the content, only of
    the link. The versions are therefore the commits in which the manifest's
    stamp for it changed, which is the same question asked of the one record
    that does track the bytes.

    Examples
    --------

    ```python
    for v in versions(entry):
        print(v.sha[:12], v.date[:10], v.size, v.subject)
    ```
    """
    if entry.source == "github":
        return remote.versions(entry)
    tags = gitutil.tag_map(entry.root)
    if entry.latest.link is not None:
        return _link_versions(entry, tags)
    dataset = entry.latest.parts > 0
    out: List[Version] = []
    history = gitutil.file_history(entry.root, entry.path, follow=not dataset)
    for sha, date, subject in history:
        if dataset:
            tree = gitutil.tree_at(entry.root, sha, entry.path)
            if tree is None:
                continue
            files = [
                f for f in gitutil.tree_files(entry.root, sha, entry.path)
                if not manifest.is_manifest(f[0])
            ]
            if not files:
                continue
            total = 0
            for _path, blob_sha, size in files:
                real, _oid = _resolve_size(entry.root, blob_sha, size)
                if real > 0:
                    total += real
            out.append(
                Version(sha=sha, date=date, subject=subject,
                        blob=tree, size=total, lfs_oid=None, parts=len(files),
                        tags=tags.get(sha, ()))
            )
            continue
        got = gitutil.blob_at(entry.root, sha, entry.path)
        if got is None:
            continue
        blob, size = got
        real_size, lfs_oid = _resolve_size(entry.root, blob, size)
        out.append(
            Version(sha=sha, date=date, subject=subject, blob=blob,
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
    latest = entry.latest
    if latest.sha.startswith(ref) or ref in latest.tags:
        return latest
    # Resolving the wanted commit directly costs two lookups; listing every
    # version to search it costs one per version, and the catalog holds only the
    # current one, so history is read only to explain a failure.
    got = _version_at(entry, ref)
    if got is not None:
        return got
    hist = versions(entry)
    known = ", ".join(v.sha for v in hist[:5])
    raise LookupError(f"no version {ref!r} of {entry.repo_key}:{entry.path}; known: {known}")


def _version_at(entry: Entry, ref: str) -> Optional[Version]:
    """
    Read one named version of a file without listing the others.

    Parameters
    ----------
    entry :
        Entry whose content to read.
    ref :
        Commit sha, a unique prefix of one, or a tag.

    Returns
    -------
    :
        The version, or `None` when `ref` names nothing that holds this file.
    """
    if entry.source == "github":
        return remote.version_at(entry, ref)
    dataset = entry.latest.parts > 0
    sha = gitutil.git(entry.root, "rev-parse", f"{ref}^{{commit}}", check=False).strip()
    if not sha:
        return None
    fmt = f"%H{gitutil.SEP}%cI{gitutil.SEP}%s"
    one = gitutil.git(entry.root, "log", "-1", f"--format={fmt}", sha, check=False).strip()
    if not one:
        return None
    info = tuple(one.split(gitutil.SEP, 2))
    if len(info) != 3:
        return None
    if entry.latest.link is not None:
        got = _stamp_at(entry, sha)
        if got is None:
            return None
        stamp, target = got
        return Version(sha=info[0], date=info[1], subject=info[2],
                       blob=stamp.sha256, size=stamp.size, link=target,
                       tags=gitutil.tag_map(entry.root).get(info[0], ()))
    if dataset:
        tree = gitutil.tree_at(entry.root, sha, entry.path)
        if tree is None:
            return None
        files = [
            f for f in gitutil.tree_files(entry.root, sha, entry.path)
            if not manifest.is_manifest(f[0])
        ]
        total = 0
        for _p, blob_sha, size in files:
            real, _oid = _resolve_size(entry.root, blob_sha, size)
            if real > 0:
                total += real
        return Version(sha=info[0], date=info[1], subject=info[2],
                       blob=tree, size=total, lfs_oid=None, parts=len(files),
                       tags=gitutil.tag_map(entry.root).get(info[0], ()))
    got = gitutil.blob_at(entry.root, sha, entry.path)
    if got is None:
        return None
    blob, size = got
    real_size, lfs_oid = _resolve_size(entry.root, blob, size)
    return Version(sha=info[0], date=info[1], subject=info[2],
                   blob=blob, size=real_size, lfs_oid=lfs_oid,
                   tags=gitutil.tag_map(entry.root).get(info[0], ()))


def outdated(entry: Entry, version: Version) -> Optional[str]:
    """
    Say whether a pinned version has been overtaken.

    Comparing content rather than commits is what makes this worth reading. A
    version names a state of the whole repository, so a pinned sha falls behind
    whenever anything in the repository changes; that is not news. The file
    having actually changed is.

    Parameters
    ----------
    entry :
        Entry holding the current version.
    version :
        Version that was asked for.

    Returns
    -------
    :
        A message naming the newer version, or `None` when the pin is still the
        current content. It is only as current as the catalog: a stored catalog
        that has not been refreshed cannot know about a change made since.

    Examples
    --------

    ```python
    outdated(entry, pinned)
    # 'a newer version of x-gwas:results/hits.csv exists: 9f3c... (2026-04-11);
    #  you asked for e4f5... (2025-11-02)'
    ```
    """
    if version.sha == entry.latest.sha or version.blob == entry.latest.blob:
        return None
    return (
        f"a newer version of {entry.repo_key}:{entry.path} exists: "
        f"{entry.latest.sha} ({entry.latest.date[:10]}); "
        f"you asked for {version.sha} ({version.date[:10]})"
    )


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
        Settings. Defaults to [](`crossrepo.config.active_config`): what
        [](`crossrepo.config.use_config`) registered, or the configuration file.

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
    [](`crossrepo.core.catalog`)
    """
    spec = Spec.parse(spec_text)
    entries = catalog(refresh=refresh, cfg=cfg)
    entry = resolve_one(entries, spec)
    version = find_version(entry, spec.version or "latest")
    if spec.version is not None:
        stale = outdated(entry, version)
        if stale:
            print(stale, file=sys.stderr)
    return materialize(entry, version)


def get(
    repo: str,
    filename: str,
    version: Optional[str] = None,
    *,
    owner: Optional[str] = None,
    out: Optional[Union[str, Path]] = None,
    refresh: bool = False,
    quiet: bool = False,
    cfg: Optional[Config] = None,
) -> Path:
    """
    Download one result file, addressed by repository and file name.

    This is the form meant for a notebook. Passing `version` pins the result, and
    the call is then silent and reproducible. Leaving it out takes the latest
    version and prints its hash together with the call that pins it, so the
    pinned form can be copied straight back into the cell.

    Parameters
    ----------
    repo :
        Repository name, optionally written ``owner/repo`` when the name alone
        is used by two organisations.
    filename :
        File name, or as much of the path as is needed to be unambiguous within
        the repository.
    version :
        Hash of the commit holding the wanted version, a unique prefix of one,
        or a tag name. `None` takes the latest version and prints its hash.
    owner :
        Repository owner, an alternative to writing ``owner/repo`` in `repo`.
    out :
        Also write a copy here. A directory, or a path written with a trailing
        separator, keeps the original file name, and missing parent directories
        are created.
    refresh :
        Rescan the repositories before resolving, instead of using the stored
        catalog.
    quiet :
        Do not print the hash even when `version` was not given. The warning
        that a pinned version has been overtaken is still printed, on standard
        error: it says something happened rather than merely reporting.
    cfg :
        Settings. Defaults to [](`crossrepo.config.active_config`): what
        [](`crossrepo.config.use_config`) registered, or the configuration file.

    Returns
    -------
    :
        Path of the file in the local cache, ready to hand to a reader.

    Raises
    ------
    LookupError
        If nothing matches, if several files match, or if there is no such
        version. The message lists the candidates as full specs.
    FileNotFoundError
        If the file is held in Git LFS and its content has not been fetched
        into the repository, or is published as a link and the link resolves to
        nothing on the machine holding the repository.
    ValueError
        If the file is published as a link and the content it points at is not
        what its stamp records, which means it was regenerated without being
        stamped again.

    Examples
    --------

    Read the latest version, and be told the hash that pins it:

    ```python
    import crossrepo
    import pandas as pd

    df = pd.read_csv(crossrepo.get("x-gwas", "hits.csv"))
    # munch-group/x-gwas:results/hits.csv@e4f5a6b  (2026-04-11, 1.2M)
    # pin this version:  crossrepo.get("x-gwas", "hits.csv", "e4f5a6b")
    ```

    Pin it, so the notebook reads the same bytes next year:

    ```python
    df = pd.read_csv(crossrepo.get("x-gwas", "hits.csv", "e4f5a6b"))
    ```

    See Also
    --------
    [](`crossrepo.core.fetch`)
    [](`crossrepo.versions`)
    [](`crossrepo.core.outdated`)
    """
    given = repo
    if owner is None and "/" in repo:
        owner, _, repo = repo.partition("/")
    entries = catalog(refresh=refresh, cfg=cfg)
    entry = resolve_one(entries, Spec(repo=repo, path=filename, owner=owner))
    found = find_version(entry, version or "latest")
    path = materialize(entry, found)
    if version is not None:
        stale = outdated(entry, found)
        if stale:
            print(stale, file=sys.stderr)
    if version is None and not quiet:
        note = f"  {entry.description}" if entry.description else ""
        print(f'Add version="{found.sha}" to pin this version.')
        # print(
        #     f"{entry.repo_key}:{entry.path}@{found.sha}  "
        #     f"({found.date[:10]}, {human(found.size)}){note}\n"
        #     f'pin this version:  crossrepo.get("{given}", "{filename}", "{found.sha}")'
        # )
    if out is not None:
        path = copy_out(path, out, entry.name)
    return path


def copy_out(src: Path, out: Union[str, Path], name: str) -> Path:
    """
    Write a copy of a cached file where the caller asked for it.

    The copy is streamed rather than buffered, so a large result file does not
    have to fit in memory.

    Parameters
    ----------
    src :
        File to copy, typically a path returned by
        [](`crossrepo.core.materialize`).
    out :
        Destination. An existing directory, or a path written with a trailing
        separator, keeps `name`; anything else is used as the file name itself.
        Missing parent directories are created.
    name :
        File name to use when `out` names a directory.

    Returns
    -------
    :
        The path written.

    See Also
    --------
    [](`crossrepo.core.get`)
    """
    dest = Path(out).expanduser()
    if dest.is_dir() or str(out).endswith(("/", "\\")):
        dest = dest / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return dest


def materialize(entry: Entry, version: Version) -> Path:
    """
    Place one version of a result file in the cache and return its path.

    Content already cached is not fetched again, which is why an unchanged file
    costs nothing across versions and repositories. Git LFS content is taken
    from the repository's own object store, and content published as a link from
    wherever the link points.

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
        ``<cache>/files/<repo>/<version>/<path>``. For a dataset published as a
        directory this is the directory, with every part inside it.

    Raises
    ------
    FileNotFoundError
        If the content is held in Git LFS and has not been fetched, or is
        published as a link and the link resolves to nothing. The message names
        the ``git lfs fetch`` command to run, or the file that is missing.
    ValueError
        If the content is published as a link and does not hash to the stamp
        that was asked for, which means it was regenerated without being
        stamped again.
    """
    dest = cache.readable_path(entry.repo_key, version.sha, entry.path)
    if version.parts:
        return _materialize_dataset(entry, version, dest)
    if version.link is not None:
        return cache.link(_cache_link(entry, version), dest)
    blob = _cache_blob(
        entry, version.sha, entry.path, version.blob, version.lfs_oid, version.size
    )
    return cache.link(blob, dest)


def _lfs_fetch(root: Location) -> str:
    """
    The command that fetches a repository's Git LFS content.

    Parameters
    ----------
    root :
        Working tree the content is missing from.

    Returns
    -------
    :
        A command to run, naming the machine the repository is on when that is
        not this one.
    """
    fetch = f"git -C {root.path} lfs fetch --all"
    return f"ssh {root.host} {fetch}" if root.is_remote else fetch


def _on(root: Location) -> str:
    """
    Name the machine a repository's content would have to be on.

    Parameters
    ----------
    root :
        Working tree the content was looked for in.

    Returns
    -------
    :
        A phrase for a message, naming the host when the repository is on
        another machine.
    """
    return f"on {root.host}" if root.is_remote else "on this machine"


def _cache_link(entry: Entry, version: Version) -> Path:
    """
    Put the content a link points at in the cache and return the object.

    The content is hard linked rather than copied when it and the cache are on
    one filesystem, which is the point of publishing a large file this way: the
    bytes are written once, by the pipeline, and the cache borrows them. A copy
    is the fallback across filesystems, and a stream the fallback over ssh.

    The stamp is checked before the object is published under it, so the cache
    cannot come to hold content that does not hash to its key. Size is checked
    first, which settles the common case -- a file regenerated since it was
    stamped -- without reading it.

    Parameters
    ----------
    entry :
        Entry the link belongs to.
    version :
        Version to fetch, whose `crossrepo.model.Version.blob` is the stamped
        sha256 and whose `crossrepo.model.Version.link` is the target.

    Returns
    -------
    :
        Path of the cached object. Content already cached is not read again, so
        a second call costs nothing, and content equal to something already
        cached -- another version of the same file, or a Git LFS object -- is
        stored once.

    Raises
    ------
    FileNotFoundError
        If the link resolves to nothing, which is what a clone without the
        pipeline's output looks like.
    ValueError
        If the content does not match the stamp.

    Notes
    -----
    A hard link shares an inode with the file the pipeline wrote. Rewriting that
    file in place therefore changes the cached object too, which
    ``crossrepo cache --verify`` is what catches; a pipeline that writes a new
    file and renames it over the old one, as most do, leaves the cache alone.
    """
    key = version.blob
    blob = cache.blob_path(key)
    if blob.exists():
        return blob
    root = entry.root
    src = None
    if str(root) and str(root) != ".":
        src = gitutil.resolve_link(root, entry.path, version.link or "")
    if src is None:
        raise FileNotFoundError(
            f"{entry.repo_key}:{entry.path}@{version.sha[:7]} is published as a "
            f"symbolic link to {version.link}, and there is no such file "
            f"{_on(root)}. Git holds the link and not the content, so the "
            f"content can only be read from a clone that the pipeline wrote "
            f"its output beside."
        )
    tmp, final = cache.open_for_write(key)
    try:
        here = src.local_path()
        if here is not None:
            try:
                os.link(here, tmp)              # same filesystem: free
            except OSError:
                shutil.copyfile(here, tmp)      # across filesystems: streamed
        else:
            gitutil.write_file_to(src, tmp)     # streamed over ssh
        size = tmp.stat().st_size
        if size != version.size:
            raise ValueError(
                _stale(entry, version, f"is {human(size)}, not {human(version.size)}")
            )
        digest = cache.content_hash(tmp)
        if digest != key:
            raise ValueError(
                _stale(entry, version, f"hashes to {digest[:12]}, not {key[:12]}")
            )
        tmp.replace(final)
    finally:
        tmp.unlink(missing_ok=True)
    return blob


def _stale(entry: Entry, version: Version, what: str) -> str:
    """
    Say that a link resolved to content its stamp does not describe.

    Parameters
    ----------
    entry :
        Entry the link belongs to.
    version :
        Version that was asked for.
    what :
        How the content differs, as a phrase completing the sentence.

    Returns
    -------
    :
        The message to raise.
    """
    return (
        f"{entry.repo_key}:{entry.path}@{version.sha[:7]} does not hold the "
        f"content it was stamped with: {version.link} {what}. The file has been "
        f"regenerated since it was stamped, so this version no longer exists "
        f"{_on(entry.root)}. Run `crossrepo stamp` in the repository and commit "
        f"the result to publish what is there now."
    )


def _cache_blob(
    entry: Entry, short: str, path: str, blob_sha: str, lfs_oid: Optional[str],
    size: int = 0,
) -> Path:
    """
    Put one file's content in the cache and return the object.

    Parameters
    ----------
    entry :
        Entry the content belongs to, used for the repository and for messages.
    short :
        Commit sha of the version, used only in messages.
    path :
        Repository-relative path of the file, used only in messages.
    blob_sha :
        Blob sha of the content.
    lfs_oid :
        Git LFS object id when the file is held in LFS, else `None`.
    size :
        Size of the real content, which the Git LFS batch endpoint asks for when
        downloading.

    Returns
    -------
    :
        Path of the cached object. Content already in a local clone is read from
        it; otherwise, for a repository read over the API, it is downloaded.

    Raises
    ------
    FileNotFoundError
        If the content is held in Git LFS and has not been fetched.
    """
    key = lfs_oid or blob_sha
    blob = cache.blob_path(key)
    if blob.exists():
        return blob
    if entry.source == "github" and not _has_object(entry.root, blob_sha):
        tmp, final = cache.open_for_write(key)
        try:
            remote.download(entry, blob_sha, lfs_oid, size, tmp)
            tmp.replace(final)
        finally:
            tmp.unlink(missing_ok=True)
        return blob
    if lfs_oid:
        src = gitutil.lfs_object_path(entry.root, lfs_oid)
        if src is None:
            raise FileNotFoundError(
                f"{entry.repo_key}:{path}@{short} is stored in Git LFS "
                f"and its content has not been fetched into the clone. Run:\n"
                f"  {_lfs_fetch(entry.root)}"
            )
        here = src.local_path()
        if here is not None:
            cache.store_from_file(key, here)
        else:
            tmp, final = cache.open_for_write(key)
            try:
                gitutil.write_file_to(src, tmp)     # streamed over ssh
                tmp.replace(final)
            finally:
                tmp.unlink(missing_ok=True)
    else:
        tmp, final = cache.open_for_write(key)
        try:
            gitutil.write_blob_to(entry.root, blob_sha, tmp)
            tmp.replace(final)
        finally:
            tmp.unlink(missing_ok=True)
    return blob


def _materialize_dataset(entry: Entry, version: Version, dest: Path) -> Path:
    """
    Place every part of a dataset in the cache and return the directory.

    Parts are cached one at a time, each under its own blob sha, so a version
    that repartitions only some of them costs only those. The parts are then
    hard linked into one directory, which is what a parquet reader wants handed
    to it.

    Parameters
    ----------
    entry :
        Entry the dataset belongs to.
    version :
        Version to materialize.
    dest :
        Directory to assemble.

    Returns
    -------
    :
        `dest`, holding every part of the dataset.
    """
    prefix = entry.path + "/"
    if entry.source == "github" and not _has_object(entry.root, version.blob):
        parts = remote.dataset_files(entry, version.sha)
        for path, blob_sha, size in parts:
            blob = _cache_blob(entry, version.sha, path, blob_sha, None, size)
            cache.link(blob, dest / path[len(prefix):])
        return dest
    for path, blob_sha, size in gitutil.tree_files(entry.root, version.sha, entry.path):
        if manifest.is_manifest(path):
            continue
        _real, lfs_oid = _resolve_size(entry.root, blob_sha, size)
        blob = _cache_blob(entry, version.sha, path, blob_sha, lfs_oid, _real)
        cache.link(blob, dest / path[len(prefix):])
    return dest
