"""
Cataloguing repositories on GitHub without checking them out.

The rules are the ones the local scanner follows: a file is published if it is
committed under a results directory and named by the ``crossrepo.yml`` governing
it, and its version is the commit in which it last changed. Only the way the
repository is read differs.

Cost is kept low deliberately, and is flat in the number of files. One recursive
tree call says whether a repository publishes anything at all, so a repository
without a manifest costs a single request. One further call reads the commit the
repository points at, which stamps everything it publishes. A repository holding
three hundred result files costs no more to catalog than one holding a single
file.
"""

from __future__ import annotations

import fnmatch
import posixpath
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import gitutil, manifest
from .config import Config, SourceWarning
from .github import Client, GitHubError
from .location import Location
from .model import Entry, Version

LFS_PROBE_LIMIT = 1024
"""Largest blob worth reading to see whether it is a Git LFS pointer."""


def _lfs_patterns(client: Client, owner: str, repo: str, tree: Sequence[dict]) -> List[str]:
    """
    Find which paths a repository keeps in Git LFS.

    Reading every small blob to see whether it is a pointer would cost a request
    per file. ``.gitattributes`` says which patterns are LFS tracked, so one
    request usually replaces all of them, and a repository using no LFS costs
    nothing.

    Parameters
    ----------
    client :
        Client to read with.
    owner :
        Repository owner.
    repo :
        Repository name.
    tree :
        The repository tree.

    Returns
    -------
    :
        Glob patterns whose files are held in Git LFS.
    """
    patterns: List[str] = []
    for item in tree:
        if posixpath.basename(item.get("path", "")) != ".gitattributes":
            continue
        prefix = posixpath.dirname(item["path"])
        try:
            text = client.read_blob(owner, repo, item["sha"]).decode("utf-8", "replace")
        except GitHubError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "filter=lfs" not in line:
                continue
            pattern = line.split()[0]
            patterns.append(posixpath.join(prefix, pattern) if prefix else pattern)
    return patterns


def _is_lfs_candidate(path: str, size: int, patterns: Sequence[str]) -> bool:
    """
    Test whether a file is worth reading to look for a Git LFS pointer.

    Parameters
    ----------
    path :
        Repository-relative path.
    size :
        Size of the stored blob.
    patterns :
        Patterns from ``.gitattributes``.

    Returns
    -------
    :
        `True` when the file is small enough to be a pointer and is tracked by
        Git LFS.
    """
    if size < 0 or size > LFS_PROBE_LIMIT:
        return False
    name = posixpath.basename(path)
    return any(
        fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in patterns
    )


def _resolve_size(
    client: Client, owner: str, repo: str, path: str, sha: str, size: int,
    patterns: Sequence[str],
) -> Tuple[int, Optional[str]]:
    """
    Look through a Git LFS pointer to the size of the real content.

    Parameters
    ----------
    client :
        Client to read with.
    owner :
        Repository owner.
    repo :
        Repository name.
    path :
        Repository-relative path, matched against the LFS patterns.
    sha :
        Blob sha.
    size :
        Size of the stored blob.
    patterns :
        Patterns from ``.gitattributes``.

    Returns
    -------
    :
        ``(real_size, lfs_oid)``, with `lfs_oid` `None` for an ordinary file.
    """
    if not _is_lfs_candidate(path, size, patterns):
        return size, None
    try:
        data = client.read_blob(owner, repo, sha)
    except GitHubError:
        return size, None
    from .gitutil import parse_lfs_pointer

    pointer = parse_lfs_pointer(data)
    if pointer:
        oid, real = pointer
        return real, oid
    return size, None


def _commit_info(commit: dict) -> Tuple[str, str, str]:
    """
    Reduce a commit document to what a version needs.

    Parameters
    ----------
    commit :
        Commit as GitHub returns it.

    Returns
    -------
    :
        ``(sha, iso_date, subject)``. The sha is full: it is the version key.
    """
    sha = commit["sha"]
    detail = commit.get("commit", {})
    date = (detail.get("committer") or detail.get("author") or {}).get("date", "")
    subject = (detail.get("message") or "").split("\n", 1)[0]
    return sha, date, subject


def scan_repo(
    client: Client, owner: str, repo: str, cfg: Config, ref: Optional[str] = None
) -> List[Entry]:
    """
    Catalog the result files one GitHub repository publishes.

    Parameters
    ----------
    client :
        Client to read with.
    owner :
        Repository owner.
    repo :
        Repository name.
    cfg :
        Settings supplying the results directories and the reading side filters.
    ref :
        Branch, tag or commit to read. Defaults to the repository's default
        branch.

    Returns
    -------
    :
        One entry per published result file or dataset, each stamped with the
        commit the repository points at. Empty, at the cost of a single request,
        when the repository publishes nothing.

    See Also
    --------
    [](`crossrepo.core.scan_repo`)
    """
    # "HEAD" is a ref GitHub resolves itself, so the default branch costs no
    # request of its own.
    branch = ref or "HEAD"
    tree = client.tree(owner, repo, branch)
    if not tree:
        return []

    # Results directories are paths relative to the repository root, at any
    # depth, so a blob belongs to one when that path is a leading part of it.
    wanted_dirs = tuple(d.strip("/").lower() for d in cfg.asset_dirs if d.strip("/"))

    def under_results(path: str) -> bool:
        lowered = path.lower()
        return any(lowered.startswith(d + "/") for d in wanted_dirs)

    # A symbolic link is a blob whose content is the target path, so taking it
    # for a result file would publish the path as though it were the data. Its
    # content is not in the repository at all, and the API cannot reach the
    # machine that has it, so a link is left to the clones that can: reading one
    # is [](`crossrepo.core.scan_repo`)'s to do.
    blobs = {
        item["path"]: (item["sha"], item.get("size", -1))
        for item in tree
        if item.get("type") == "blob"
        and item.get("mode") != gitutil.LINK_MODE
        and under_results(item.get("path", ""))
    }
    if not blobs:
        return []

    governing = None
    where = ""
    for path, (sha, _size) in sorted(blobs.items()):
        head, _, name = path.rpartition("/")
        if head.lower() not in wanted_dirs or name not in manifest.MANIFEST_NAMES:
            continue                       # only a results directory's own
        try:
            text = client.read_blob(owner, repo, sha).decode("utf-8", "replace")
            governing = manifest.parse(text, head)
            where = path
        except (manifest.ManifestError, GitHubError) as exc:
            warnings.warn(f"{owner}/{repo}: {exc}", stacklevel=2)
        break
    if governing is None:
        return []

    # A manifest may also name files from the root of the repository, which lie
    # outside the results directory the rest of this reads.
    prefixes = governing.anchored_prefixes()
    if prefixes:
        for item in tree:
            path = item.get("path", "")
            if item.get("type") != "blob" or path in blobs:
                continue
            if any(not p or path == p or path.startswith(p + "/") for p in prefixes):
                blobs[path] = (item["sha"], item.get("size", -1))

    from .core import _covering_dataset, _dataset_roots, _wanted

    datasets = _dataset_roots(blobs, governing)
    published: Dict[str, str] = {}
    for path in sorted(blobs):
        if manifest.is_manifest(path) or _covering_dataset(path, datasets) is not None:
            continue
        description = governing.describe(path)
        if description is not None:
            published[path] = description
    if not published and not datasets:
        return []

    candidates = list(published) + [
        p for p in blobs if _covering_dataset(p, datasets) is not None
    ]
    # Reading .gitattributes only pays off if something could be a pointer at
    # all; a repository whose published files are all large needs neither it nor
    # any probe.
    patterns: List[str] = []
    if any(0 <= blobs[p][1] <= LFS_PROBE_LIMIT for p in candidates):
        patterns = _lfs_patterns(client, owner, repo, tree)
    sizes: Dict[str, Tuple[int, Optional[str]]] = {}
    for path in candidates:
        sha, size = blobs[path]
        sizes[path] = _resolve_size(client, owner, repo, path, sha, size, patterns)

    head = client.commit(owner, repo, branch)
    if head is None:
        return []
    sha, date, subject = _commit_info(head)

    slug = f"{owner}/{repo}"
    entries: List[Entry] = []
    for path, description in sorted(published.items()):
        real_size, lfs_oid = sizes[path]
        if not _wanted(posixpath.basename(path), real_size, cfg):
            continue
        entries.append(
            Entry(
                owner=owner, repo=repo, path=path, root=Location(""),
                remote=slug, source="github",
                description=description, manifest=where,
                latest=Version(
                    sha=sha, date=date, subject=subject,
                    blob=blobs[path][0], size=real_size, lfs_oid=lfs_oid,
                    tags=(),
                ),
            )
        )
    for directory, description in sorted(datasets.items()):
        parts = [p for p in blobs if p.startswith(directory + "/")
                 and not manifest.is_manifest(p)]
        if not parts:
            continue
        total = sum(max(sizes[p][0], 0) for p in parts)
        if not _wanted(posixpath.basename(directory), total, cfg):
            continue
        entries.append(
            Entry(
                owner=owner, repo=repo, path=directory, root=Location(""),
                remote=slug, source="github",
                description=description, manifest=where,
                latest=Version(
                    sha=sha, date=date, subject=subject,
                    blob=_tree_sha(tree, directory), size=total, lfs_oid=None,
                    parts=len(parts), tags=(),
                ),
            )
        )
    return entries


def _tree_sha(tree: Sequence[dict], directory: str) -> str:
    """
    Find the tree sha of a directory in a repository tree.

    Parameters
    ----------
    tree :
        The repository tree.
    directory :
        Repository-relative path of the directory.

    Returns
    -------
    :
        Its tree sha, or an empty string when the tree does not list it.
    """
    for item in tree:
        if item.get("type") == "tree" and item.get("path") == directory:
            return item.get("sha", "")
    return ""


def _may_hold(select: Optional[str], owner: str) -> bool:
    """
    Test whether an owner can hold a repository a narrowed scan wants.

    Listing an organisation is one request; reading its repositories is one or
    more each. Skipping the listing of an owner that cannot match saves the
    first of those, and is safe: a `select` naming an owner is written with a
    slash, and both it and ``owner/repo`` hold exactly one, so a match must line
    the two slashes up, which puts everything before the slash in `select`
    inside the owner.

    Parameters
    ----------
    select :
        Text a repository is selected by, as [](`crossrepo.core.selects`) matches
        it. `None` selects everything.
    owner :
        The organisation or user about to be listed.

    Returns
    -------
    :
        Whether any of that owner's repositories could match.
    """
    if select is None or "/" not in select:
        return True
    return select.split("/", 1)[0].lower() in owner.lower()


def build(
    cfg: Config, client: Optional[Client] = None, progress: bool = False,
    select: Optional[str] = None,
) -> List[Entry]:
    """
    Catalog every repository the settings name on GitHub.

    Parameters
    ----------
    cfg :
        Settings supplying `owners` and `repos`.
    client :
        Client to read with. Defaults to a new one, authenticated as
        [](`crossrepo.github.token`) finds.
    progress :
        Show a progress bar, one step per repository. Reading a whole
        organisation is otherwise silent for as long as it takes.
    select :
        Read only the repositories this names, by [](`crossrepo.core.selects`).
        An owner that cannot hold a match is not even listed, and one that can
        is listed but only read into where a repository matches, so narrowing a
        scan to one repository of an organisation costs one request for the
        listing instead of one for every repository it holds.

    Returns
    -------
    :
        Entries from every repository that publishes something. A repository
        that cannot be read is skipped with a [](`crossrepo.config.SourceWarning`)
        rather than failing the scan, as is an owner GitHub will not list and a
        repository named in the settings that is not there.

    See Also
    --------
    [](`crossrepo.remote.scan_repo`)
    [](`crossrepo.core.selects`)
    """
    if not cfg.owners and not cfg.repos:
        return []
    from .core import selects

    client = client or Client()
    targets: List[Tuple[str, str, Optional[str]]] = []
    for owner in cfg.owners:
        if not _may_hold(select, owner):
            continue
        try:
            for name, branch in client.repositories(owner):
                if selects(select, f"{owner}/{name}"):
                    targets.append((owner, name, branch))
        except GitHubError as exc:
            warnings.warn(f"{owner}: {exc}", SourceWarning, stacklevel=2)
    named: List[Tuple[str, str]] = []
    for slug in cfg.repos:
        if "/" not in slug:
            warnings.warn(
                f"{slug!r} should be written owner/repo", SourceWarning, stacklevel=2
            )
            continue
        owner, _, name = slug.partition("/")
        if not selects(select, f"{owner}/{name}"):
            continue
        named.append((owner, name))
        targets.append((owner, name, None))

    from .core import progress_bar

    seen = set()
    published = set()
    entries: List[Entry] = []
    for owner, name, branch in progress_bar(targets, "reading GitHub", progress):
        if (owner.lower(), name.lower()) in seen:
            continue
        seen.add((owner.lower(), name.lower()))
        try:
            found = scan_repo(client, owner, name, cfg, branch)
        except GitHubError as exc:
            warnings.warn(f"{owner}/{name}: {exc}", SourceWarning, stacklevel=2)
            continue
        if found:
            published.add((owner.lower(), name.lower()))
        entries.extend(found)
    _check_named(client, named, published)
    return entries


def _check_named(
    client: Client, named: Sequence[Tuple[str, str]], published: Set[Tuple[str, str]]
) -> None:
    """
    Say which of the repositories named in the settings are not really there.

    A repository that publishes nothing and a repository that does not exist
    look alike from the outside: both come back empty. Telling them apart takes
    one more request, so it is asked only about a repository the settings name
    outright — a handful, where an organisation may hold hundreds — and only
    when it published nothing.

    Parameters
    ----------
    client :
        Client to ask with.
    named :
        ``(owner, repo)`` for each repository named in `crossrepo.config.Config`.
    published :
        Those that published something, lowercased, which are known to exist.
    """
    for owner, name in named:
        if (owner.lower(), name.lower()) in published:
            continue
        try:
            client.get(f"repos/{owner}/{name}")
        except GitHubError as exc:
            warnings.warn(f"{owner}/{name}: {exc}", SourceWarning, stacklevel=3)


def versions(entry: Entry, client: Optional[Client] = None) -> List[Version]:
    """
    List every version of one published file or dataset on GitHub.

    Parameters
    ----------
    entry :
        Entry whose history to read. Its `remote` names the repository.
    client :
        Client to read with. Defaults to a new one.

    Returns
    -------
    :
        Versions newest first.

    See Also
    --------
    [](`crossrepo.versions`)
    """
    client = client or Client()
    owner, _, repo = entry.remote.partition("/")
    dataset = entry.latest.parts > 0
    out: List[Version] = []
    for commit in client.commits(owner, repo, entry.path, limit=100):
        sha, date, subject = _commit_info(commit)
        if dataset:
            files = dataset_files(entry, sha, client)
            if not files:
                continue
            out.append(
                Version(sha=sha, date=date, subject=subject,
                        blob=_tree_sha(client.tree(owner, repo, sha), entry.path),
                        size=sum(max(s, 0) for _p, _b, s in files),
                        lfs_oid=None, parts=len(files), tags=())
            )
            continue
        try:
            got = client.get(
                f"repos/{owner}/{repo}/contents/{entry.path}", ref=sha
            )
        except GitHubError:
            continue
        if not isinstance(got, dict) or "sha" not in got:
            continue
        out.append(
            Version(sha=sha, date=date, subject=subject,
                    blob=got["sha"], size=got.get("size", -1),
                    lfs_oid=entry.latest.lfs_oid, tags=())
        )
    return out


def dataset_files(
    entry: Entry, commit: str, client: Optional[Client] = None
) -> List[Tuple[str, str, int]]:
    """
    List the parts of a dataset at one commit.

    Parameters
    ----------
    entry :
        Entry naming the dataset.
    commit :
        Commit sha to read at.
    client :
        Client to read with. Defaults to a new one.

    Returns
    -------
    :
        Tuples of ``(path, blob_sha, size)``, sorted by path.

    See Also
    --------
    [](`crossrepo.gitutil.tree_files`)
    """
    client = client or Client()
    owner, _, repo = entry.remote.partition("/")
    prefix = entry.path + "/"
    return sorted(
        (item["path"], item["sha"], item.get("size", -1))
        for item in client.tree(owner, repo, commit)
        if item.get("type") == "blob"
        and item.get("path", "").startswith(prefix)
        and not manifest.is_manifest(item["path"])
    )


def download(
    entry: Entry, blob_sha: str, lfs_oid: Optional[str], size: int, dest: Path,
    client: Optional[Client] = None,
) -> None:
    """
    Stream one file's content from GitHub into a local path.

    Parameters
    ----------
    entry :
        Entry naming the repository.
    blob_sha :
        Blob sha of the content, used for an ordinary file.
    lfs_oid :
        Git LFS object id, used instead when the file is held in LFS.
    size :
        Size of the real content, which the LFS batch endpoint asks for.
    dest :
        File to write.
    client :
        Client to read with. Defaults to a new one.

    Raises
    ------
    GitHubError
        If the content cannot be read.
    """
    client = client or Client()
    owner, _, repo = entry.remote.partition("/")
    if lfs_oid:
        client.lfs_download(owner, repo, lfs_oid, size, dest)
    else:
        client.download_blob(owner, repo, blob_sha, dest)


def version_at(entry: Entry, ref: str, client: Optional[Client] = None) -> Optional[Version]:
    """
    Read one named version of a file on GitHub without listing the others.

    Two requests, whatever the length of the history: the commit, and the file
    as of that commit. Listing every version to search it would cost a request
    per version.

    Parameters
    ----------
    entry :
        Entry whose content to read.
    ref :
        Commit sha, a unique prefix of one, a tag or a branch.
    client :
        Client to read with. Defaults to a new one.

    Returns
    -------
    :
        The version, or `None` when `ref` names nothing that holds this file.
    """
    client = client or Client()
    owner, _, repo = entry.remote.partition("/")
    try:
        commit = client.get(f"repos/{owner}/{repo}/commits/{ref}")
    except GitHubError:
        return None
    sha, date, subject = _commit_info(commit)
    if entry.latest.parts:
        files = dataset_files(entry, sha, client)
        if not files:
            return None
        return Version(
            sha=sha, date=date, subject=subject,
            blob=_tree_sha(client.tree(owner, repo, sha), entry.path),
            size=sum(max(s, 0) for _p, _b, s in files),
            lfs_oid=None, parts=len(files), tags=(),
        )
    try:
        got = client.get(f"repos/{owner}/{repo}/contents/{entry.path}", ref=sha)
    except GitHubError:
        return None
    if not isinstance(got, dict) or "sha" not in got:
        return None
    return Version(
        sha=sha, date=date, subject=subject,
        blob=got["sha"], size=got.get("size", -1),
        lfs_oid=entry.latest.lfs_oid, tags=(),
    )
