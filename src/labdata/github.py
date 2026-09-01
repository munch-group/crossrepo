"""
Reading repositories over the GitHub API, without cloning them.

Nothing here writes to disk except the content it is asked to download, and
nothing clones: a repository is read through its tree and commit endpoints, so a
catalog can cover repositories that were never checked out. One recursive tree
call is enough to decide whether a repository publishes anything at all, which
keeps the cost of scanning an organisation close to one request per repository.

Blob shas are git's own, so content fetched here and content read from a local
clone land on the same key in the cache and are stored once.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

API = "https://api.github.com"
"""Base address of the GitHub REST API."""

WEB = "https://github.com"
"""Base address of the GitHub web site, used for LFS and for tree links."""

RAW = "https://raw.githubusercontent.com"
"""Base address serving file content, used to build the URL of a version."""

TIMEOUT = 30
"""Seconds to wait on a request before giving up."""


class GitHubError(RuntimeError):
    """Raised when GitHub cannot be read."""


def token() -> Optional[str]:
    """
    Find a GitHub token to authenticate with.

    ``GITHUB_TOKEN`` is used when set; otherwise the token the ``gh`` command
    line is logged in with, which is usually already there on a machine that
    uses GitHub at all. Without one only public repositories are readable, and
    the rate limit is sixty requests an hour rather than five thousand.

    Returns
    -------
    :
        The token, or `None` when there is none.
    """
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=False,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


class Client:
    """
    A thin, streaming client for the parts of the GitHub API labdata needs.

    Parameters
    ----------
    auth :
        Token to authenticate with. Defaults to [](`labdata.github.token`).
    api :
        Base address of the API, so a test can point this somewhere else.

    Examples
    --------

    ```python
    client = Client()
    client.tree("munch-group", "tree-stats", "main")
    ```
    """

    def __init__(self, auth: Optional[str] = None, api: str = API) -> None:
        self.auth = token() if auth is None else auth
        self.api = api.rstrip("/")

    # ------------------------------------------------------------ plumbing

    def _open(self, url: str, accept: str, extra: Optional[Dict[str, str]] = None):
        """
        Open a URL with the headers GitHub expects.

        Parameters
        ----------
        url :
            Full URL to open.
        accept :
            Value of the ``Accept`` header, which decides whether JSON or raw
            content comes back.
        extra :
            Further headers, as a download handed over by the LFS API needs.

        Returns
        -------
        :
            The open response, to be read or streamed by the caller.

        Raises
        ------
        GitHubError
            If the request fails. Missing, unauthorised and rate limited are
            each reported in the terms that suggest what to do about them.
        """
        headers = {"Accept": accept, "User-Agent": "labdata"}
        if self.auth and url.startswith((self.api, WEB)):
            headers["Authorization"] = f"Bearer {self.auth}"
        headers.update(extra or {})
        request = urllib.request.Request(url, headers=headers)
        try:
            return urllib.request.urlopen(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            raise GitHubError(_explain(exc, url, self.auth)) from None
        except urllib.error.URLError as exc:
            raise GitHubError(f"cannot reach {url}: {exc.reason}") from None

    def get(self, endpoint: str, **params: Any) -> Any:
        """
        Fetch one JSON document.

        Parameters
        ----------
        endpoint :
            API path, such as ``repos/owner/name/git/trees/main``. Named so
            rather than ``path`` because ``path`` is itself a query parameter
            GitHub takes, on the commits endpoint among others.
        **params :
            Query parameters.

        Returns
        -------
        :
            The decoded document.
        """
        url = f"{self.api}/{endpoint.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        with self._open(url, "application/vnd.github+json") as response:
            return json.loads(response.read().decode("utf-8"))

    def paginate(self, endpoint: str, **params: Any) -> Iterator[dict]:
        """
        Fetch every page of a listing.

        Parameters
        ----------
        endpoint :
            API path returning a JSON array.
        **params :
            Query parameters. ``per_page`` defaults to the maximum of 100.

        Returns
        -------
        :
            An iterator over every item, across all pages. Pages are fetched as
            it is consumed, so stopping early stops requesting.
        """
        params.setdefault("per_page", 100)
        page = 1
        while True:
            batch = self.get(endpoint, page=page, **params)
            if not isinstance(batch, list) or not batch:
                return
            for item in batch:
                yield item
            if len(batch) < params["per_page"]:
                return
            page += 1

    # --------------------------------------------------------------- reads

    def repositories(self, owner: str) -> List[Tuple[str, str]]:
        """
        List the repositories an organisation or user owns.

        Parameters
        ----------
        owner :
            Organisation or user name.

        Returns
        -------
        :
            Tuples of ``(name, default_branch)``, skipping empty repositories.
        """
        try:
            items = list(self.paginate(f"orgs/{owner}/repos", type="all"))
        except GitHubError:
            items = list(self.paginate(f"users/{owner}/repos", type="all"))
        return [
            (r["name"], r.get("default_branch") or "HEAD")
            for r in items
            if not r.get("disabled")
        ]

    def default_branch(self, owner: str, repo: str) -> str:
        """
        Find the branch a repository is read at by default.

        Parameters
        ----------
        owner :
            Repository owner.
        repo :
            Repository name.

        Returns
        -------
        :
            The default branch name.
        """
        return self.get(f"repos/{owner}/{repo}").get("default_branch") or "HEAD"

    def tree(self, owner: str, repo: str, ref: str) -> List[dict]:
        """
        List everything in a repository at a revision, in one request.

        Parameters
        ----------
        owner :
            Repository owner.
        repo :
            Repository name.
        ref :
            Branch, tag or commit sha.

        Returns
        -------
        :
            Entries with ``path``, ``type``, ``sha`` and, for blobs, ``size``.
            An empty list when the repository has no such revision.
        """
        try:
            doc = self.get(f"repos/{owner}/{repo}/git/trees/{ref}", recursive="1")
        except GitHubError:
            return []
        return doc.get("tree", [])

    def commits(self, owner: str, repo: str, path: str, limit: int = 100) -> List[dict]:
        """
        List the commits that touched a path, newest first.

        Parameters
        ----------
        owner :
            Repository owner.
        repo :
            Repository name.
        path :
            Repository-relative path of a file or directory.
        limit :
            Most commits to return.

        Returns
        -------
        :
            Raw commit documents, newest first.
        """
        got = self.get(
            f"repos/{owner}/{repo}/commits", path=path, per_page=min(limit, 100)
        )
        return got if isinstance(got, list) else []

    def commit(self, owner: str, repo: str, ref: str) -> Optional[dict]:
        """
        Read one commit.

        Parameters
        ----------
        owner :
            Repository owner.
        repo :
            Repository name.
        ref :
            Branch, tag or commit sha. A branch gives the commit it points at,
            which is what stamps everything the repository publishes.

        Returns
        -------
        :
            The commit document, or `None` when there is no such revision.
        """
        try:
            return self.get(f"repos/{owner}/{repo}/commits/{ref}")
        except GitHubError:
            return None

    def read_blob(self, owner: str, repo: str, sha: str) -> bytes:
        """
        Read a blob into memory.

        Only for content known to be small, such as a manifest or a Git LFS
        pointer. Use [](`labdata.github.Client.download_blob`) for result files.

        Parameters
        ----------
        owner :
            Repository owner.
        repo :
            Repository name.
        sha :
            Blob sha.

        Returns
        -------
        :
            The raw content.
        """
        url = f"{self.api}/repos/{owner}/{repo}/git/blobs/{sha}"
        with self._open(url, "application/vnd.github.raw") as response:
            return response.read()

    def download_blob(self, owner: str, repo: str, sha: str, dest: Path) -> None:
        """
        Stream a blob to a file.

        The content is copied straight from the socket to the file, so peak
        memory does not grow with the size of the result.

        Parameters
        ----------
        owner :
            Repository owner.
        repo :
            Repository name.
        sha :
            Blob sha.
        dest :
            File to write.
        """
        url = f"{self.api}/repos/{owner}/{repo}/git/blobs/{sha}"
        with self._open(url, "application/vnd.github.raw") as response:
            with open(dest, "wb") as fh:
                shutil.copyfileobj(response, fh, length=1 << 20)

    # ----------------------------------------------------------------- lfs

    def lfs_download(
        self, owner: str, repo: str, oid: str, size: int, dest: Path
    ) -> None:
        """
        Stream a Git LFS object to a file.

        LFS content is not in the git tree — the tree holds a pointer — so it is
        asked for through the batch endpoint, which hands back a URL to fetch it
        from.

        Parameters
        ----------
        owner :
            Repository owner.
        repo :
            Repository name.
        oid :
            The sha256 object id from the pointer file.
        size :
            Size of the real content, from the pointer file.
        dest :
            File to write.

        Raises
        ------
        GitHubError
            If the batch endpoint refuses, or names no download for the object.
        """
        url = f"{WEB}/{owner}/{repo}.git/info/lfs/objects/batch"
        body = json.dumps(
            {
                "operation": "download",
                "transfers": ["basic"],
                "objects": [{"oid": oid, "size": size}],
            }
        ).encode("utf-8")
        headers = {
            "Accept": "application/vnd.git-lfs+json",
            "Content-Type": "application/vnd.git-lfs+json",
            "User-Agent": "labdata",
        }
        if self.auth:
            headers["Authorization"] = f"Bearer {self.auth}"
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                doc = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise GitHubError(_explain(exc, url, self.auth)) from None
        except urllib.error.URLError as exc:
            raise GitHubError(f"cannot reach {url}: {exc.reason}") from None

        objects = doc.get("objects") or []
        if not objects:
            raise GitHubError(f"Git LFS returned nothing for {oid} in {owner}/{repo}")
        first = objects[0]
        if "error" in first:
            message = first["error"].get("message", "unknown error")
            raise GitHubError(f"Git LFS refused {oid} in {owner}/{repo}: {message}")
        action = (first.get("actions") or {}).get("download")
        if not action or not action.get("href"):
            raise GitHubError(
                f"Git LFS named no download for {oid} in {owner}/{repo}; "
                f"the object may not have been pushed"
            )
        with self._open(action["href"], "*/*", action.get("header") or {}) as response:
            with open(dest, "wb") as fh:
                shutil.copyfileobj(response, fh, length=1 << 20)


def _explain(exc: urllib.error.HTTPError, url: str, auth: Optional[str]) -> str:
    """
    Turn an HTTP failure into something worth reading.

    Parameters
    ----------
    exc :
        The failure.
    url :
        URL that was asked for.
    auth :
        Token used, if any, so that a missing one can be pointed at.

    Returns
    -------
    :
        A message naming what to do about it.
    """
    hint = ""
    if exc.code in (401, 403):
        remaining = exc.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            hint = " The rate limit is spent; it resets within the hour."
        elif not auth:
            hint = (
                " No token was found. Set GITHUB_TOKEN, or log in with "
                "`gh auth login`, to read private repositories and to raise the "
                "rate limit."
            )
        else:
            hint = " The token may not cover this repository."
    elif exc.code == 404:
        hint = " It may be private, renamed, or the branch may not exist."
    return f"GitHub returned {exc.code} for {url}.{hint}"


def blob_url(owner: str, repo: str, commit: str, path: str, directory: bool = False) -> str:
    """
    Build a stable URL for one version of a published file.

    The URL names the commit rather than a branch, so it keeps pointing at the
    bytes that were cataloged rather than at whatever the branch holds later.

    Parameters
    ----------
    owner :
        Repository owner.
    repo :
        Repository name.
    commit :
        Commit sha of the version.
    path :
        Repository-relative path.
    directory :
        Whether `path` is a dataset held in a directory, which has a page rather
        than a download.

    Returns
    -------
    :
        A URL to the content, or to the directory listing for a dataset.

    Examples
    --------

    ```python
    blob_url("munch-group", "tree-stats", "7701707", "results/dummy.csv")
    # 'https://raw.githubusercontent.com/munch-group/tree-stats/7701707/results/dummy.csv'
    ```
    """
    quoted = urllib.parse.quote(path)
    if directory:
        return f"{WEB}/{owner}/{repo}/tree/{commit}/{quoted}"
    return f"{RAW}/{owner}/{repo}/{commit}/{quoted}"
