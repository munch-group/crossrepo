"""
Core data types describing result files and their versions.

A result file is identified by a *spec* of the form ``[owner/]repo:path[@version]``,
where ``version`` is the abbreviated sha of the commit in which the file last
changed. Specs are what the command line prints and what
[](`labdata.core.fetch`) accepts.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple

from .location import Location


@dataclass(frozen=True)
class Version:
    """
    One commit in which a result file changed.

    A version is defined by a commit, not by a repository tag. Analysis repos
    are rarely tagged, whereas every change to a result file is a commit, so
    commits give a version key that exists without extra discipline. Tags, when
    present, are carried in `tags` and can be used to address a version, but
    they never define one.

    Attributes
    ----------
    sha :
        Full sha of the commit the repository pointed at. This is the
        user-facing version key, written out in full so that it also means
        something to git and to GitHub without labdata in hand.
    date :
        Committer date in ISO-8601 format.
    subject :
        Subject line of the commit.
    blob :
        Git blob sha of the file at this commit, a hash of its content, used as
        the cache key. For a dataset held in a directory this is the git tree
        sha instead, which hashes the whole directory and so plays the same part.
    size :
        Size in bytes, resolved through Git LFS pointers so that it is the size
        of the real content rather than of the pointer. For a dataset it is the
        total over its files.
    lfs_oid :
        The sha256 object id when the file is stored in Git LFS, else `None`.
    parts :
        Number of files the version is made of. Zero for an ordinary file; for a
        dataset published as a directory, the count of files beneath it.
    tags :
        Any tags pointing at this commit.
    """

    sha: str
    date: str
    subject: str
    blob: str
    size: int
    lfs_oid: Optional[str] = None
    parts: int = 0
    tags: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Entry:
    """
    A result file in a repository, at its most recent version.

    Attributes
    ----------
    owner :
        Owner of the repository, taken from the origin remote when there is one
        and from the parent directory name otherwise.
    repo :
        Repository name.
    path :
        Path of the file within the repository, for example
        ``results/candidates.csv``.
    root :
        Working tree of the repository, on this machine or on a server reached
        over ssh. Empty for a repository read over the GitHub API, which was
        never checked out at all.
    latest :
        The most recent version of this file.
    description :
        What the file holds, as given for it in the ``labdata.yml`` that
        publishes it. Empty when the manifest names the file but says nothing
        about it.
    remote :
        ``owner/repo`` when the repository is on GitHub, whether it was read
        over the API or found as a local clone with a GitHub origin. Empty
        otherwise. This is what gives a version a URL.
    source :
        Where the entry was cataloged: ``local`` from a clone on this machine,
        ``ssh`` from a clone on another machine, ``github`` over the API. It
        decides where history is read from, which is not the same question as
        where content is read from — a clone can supply content it happens to
        hold for a version cataloged over the API.

    See Also
    --------
    [](`labdata.model.Version`)
    [](`labdata.manifest.Manifest`)
    """

    owner: str
    repo: str
    path: str
    root: Location
    latest: Version
    description: str = ""
    remote: str = ""
    source: str = "local"

    def url(self, version: Optional[Version] = None) -> str:
        """
        Address of one version of this file on GitHub.

        The URL names the commit, not a branch, so it keeps pointing at the
        bytes that were cataloged. For a dataset held in a directory it is the
        page listing the directory, there being no single file to fetch.

        Parameters
        ----------
        version :
            Version to address. Defaults to `latest`.

        Returns
        -------
        :
            The URL, or an empty string when the repository is not on GitHub.

        Examples
        --------

        ```python
        entry.url()
        # 'https://raw.githubusercontent.com/munch-group/tree-stats/7701707/results/dummy.csv'
        ```
        """
        if not self.remote:
            return ""
        from .github import blob_url

        v = version or self.latest
        owner, _, repo = self.remote.partition("/")
        return blob_url(owner, repo, v.sha, self.path, directory=bool(v.parts))

    @property
    def name(self) -> str:
        """
        File name without its directory.

        Returns
        -------
        :
            The last path component, for example ``candidates.csv``.
        """
        return self.path.split("/")[-1]

    @property
    def repo_key(self) -> str:
        """
        Repository identifier used in specs and listings.

        Returns
        -------
        :
            ``owner/repo`` when an owner is known, else just ``repo``.
        """
        return f"{self.owner}/{self.repo}" if self.owner else self.repo

    @property
    def spec(self) -> str:
        """
        Copy-pasteable identifier for the latest version of this file.

        Returns
        -------
        :
            A spec of the form ``owner/repo:path@version``.

        Examples
        --------

        ```python
        entry.spec
        # 'munch-group/primate-ils:results/ils_data.h5@4e6472a1b...'
        ```
        """
        return f"{self.repo_key}:{self.path}@{self.latest.sha}"

    def to_dict(self) -> Dict[str, Any]:
        """
        Represent the entry as JSON-serialisable data.

        Used by the on-disk catalog cache and by ``labdata list --json``.

        Returns
        -------
        :
            A dictionary with the dataclass fields plus the derived `spec` and
            `name`, with `root` converted to a string.
        """
        d = asdict(self)
        d["root"] = str(self.root)
        d["spec"] = self.spec
        d["name"] = self.name
        d["url"] = self.url()
        return d


_SPEC = re.compile(
    r"""^
    (?:(?P<owner>[^/:@]+)/)?      # optional owner/
    (?P<repo>[^/:@]+)             # repo
    :(?P<path>[^@]+)              # :path (may be a bare filename)
    (?:@(?P<version>.+))?         # optional @version
    $""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class Spec:
    """
    A parsed reference to a result file.

    Attributes
    ----------
    repo :
        Repository name.
    path :
        Path within the repository, or a bare file name when that is
        unambiguous within the repository.
    owner :
        Repository owner, needed only to disambiguate repositories of the same
        name in different organisations.
    version :
        Commit sha prefix or tag name. `None` means the latest version.

    See Also
    --------
    [](`labdata.core.resolve_one`)
    """

    repo: str
    path: str
    owner: Optional[str] = None
    version: Optional[str] = None

    @classmethod
    def parse(cls, text: str) -> "Spec":
        """
        Parse a spec string.

        Parameters
        ----------
        text :
            A string of the form ``[owner/]repo:path[@version]``.

        Returns
        -------
        :
            The parsed spec.

        Raises
        ------
        ValueError
            If `text` does not have the expected form.

        Examples
        --------

        ```python
        Spec.parse("munch-group/primate-ils:results/ils_data.h5@4e6472a")
        Spec.parse("humanXsweeps:tmrca_stats.hdf")
        ```
        """
        m = _SPEC.match(text.strip())
        if not m:
            raise ValueError(
                f"cannot parse {text!r}; expected [owner/]repo:path[@version], "
                f"e.g. munch-group/primate-ils:ils_data.h5@4e6472a"
            )
        g = m.groupdict()
        return cls(repo=g["repo"], path=g["path"], owner=g["owner"], version=g["version"])

    def __str__(self) -> str:
        s = f"{self.owner + '/' if self.owner else ''}{self.repo}:{self.path}"
        return f"{s}@{self.version}" if self.version else s
