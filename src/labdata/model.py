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
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


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
        Full sha of the commit in which the file last changed.
    short :
        Abbreviated commit sha. This is the user-facing version key.
    date :
        Committer date in ISO-8601 format.
    subject :
        Subject line of the commit.
    blob :
        Git blob sha of the file at this commit. This is a hash of the file
        content, and is used as the cache key.
    size :
        Size of the file in bytes, resolved through Git LFS pointers so that it
        is the size of the real content rather than of the pointer.
    lfs_oid :
        The sha256 object id when the file is stored in Git LFS, else `None`.
    tags :
        Any tags pointing at this commit.
    """

    sha: str
    short: str
    date: str
    subject: str
    blob: str
    size: int
    lfs_oid: Optional[str] = None
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
        Working tree of the repository on disk.
    latest :
        The most recent version of this file.

    See Also
    --------
    [](`labdata.model.Version`)
    """

    owner: str
    repo: str
    path: str
    root: Path
    latest: Version

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
        # 'munch-group/primate-ils:results/ils_data.h5@4e6472a'
        ```
        """
        return f"{self.repo_key}:{self.path}@{self.latest.short}"

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
