"""
Catalog and fetch versioned result files across many git repositories.

`labdata` lets one project read result files produced by another without adding
a submodule and without downloading files by hand. A file is published simply by
committing it under a repository's ``results`` directory; no manifest is needed.
The version of a file is the commit in which it last changed, so versioning
works in repositories that are never tagged.

Examples
--------

List everything the configured repositories publish:

```python
from labdata import catalog

for entry in catalog():
    print(entry.spec, entry.latest.size)
```

Read one file into a meta-analysis notebook. Without a version the latest is
taken and its hash printed, so the pinned call can be copied back into the cell:

```python
import pandas as pd
from labdata import get

df = pd.read_csv(get("x-gwas", "hits.csv"))
# munch-group/x-gwas:results/hits.csv@e4f5a6b  (2026-04-11, 1.2M)
# pin this version:  labdata.get("x-gwas", "hits.csv", "e4f5a6b")

df = pd.read_csv(get("x-gwas", "hits.csv", "e4f5a6b"))   # pinned, and silent
```

See Also
--------
[](`labdata.core.get`)
[](`labdata.core.fetch`)
[](`labdata.core.catalog`)
"""

from .config import Config
from .core import (
    build, catalog, diagnose, fetch, get, match, outdated, resolve_one,
)
from .model import Entry, Spec, Version

__version__ = "0.1.16"

__all__ = [
    "build",
    "catalog",
    "diagnose",
    "fetch",
    "frame",
    "get",
    "list",
    "match",
    "outdated",
    "refresh",
    "repos",
    "resolve_one",
    "versions",
    "Config",
    "Entry",
    "Spec",
    "Version",
]


#: Columns [](`labdata.frame`) always has, so an empty catalog still tabulates.
#: ``owner`` and ``repo`` are the two halves of ``github``, and ``dir`` and
#: ``name`` the two halves of ``path``, so that either can be grouped or sorted
#: on without taking the other apart first.
FRAME_COLUMNS = (
    "owner", "repo", "name", "description", "date", "github", "path", "dir",
    "bytes", "tags", "lfs", "version", "parts", "spec", "url",
)

#: Columns [](`labdata.versions`) always has.
VERSION_COLUMNS = ("version", "date", "bytes", "parts", "tags", "subject")

#: Columns [](`labdata.repos`) always has.
REPO_COLUMNS = ("repo", "files", "bytes", "latest")


def frame(entries=None):
    """
    Represent the catalog as a data frame.

    Parameters
    ----------
    entries :
        Entries to tabulate. Defaults to the whole catalog.

    Returns
    -------
    :
        A [](`pandas.DataFrame`) with one row per result file and columns
        ``owner``, ``repo``, ``name``, ``description``, ``date``, ``github``,
        ``path``, ``dir``, ``bytes``, ``tags``, ``lfs``, ``version``, ``parts``,
        ``spec`` and ``url``. ``github`` is ``owner/repo`` and ``path`` is
        ``dir/name``, each carried whole as well as in halves. ``parts`` is zero
        for an ordinary file and the number of files for a dataset published as
        a directory.

    Raises
    ------
    ImportError
        If pandas is not installed. It is not a dependency of `labdata`.

    Examples
    --------

    Find the largest result file in each repository:

    ```python
    from labdata import frame

    df = frame()
    df.sort_values("bytes").groupby("github").last()
    ```

    See Also
    --------
    [](`labdata.core.catalog`)
    """
    import pandas as pd

    entries = catalog() if entries is None else entries
    return pd.DataFrame(
        [
            {
                "owner": e.owner,
                "repo": e.repo,
                "name": e.name,
                "description": e.description,
                "date": e.latest.date[:10],
                "github": e.repo_key,
                "path": e.path,
                "dir": e.path.rpartition("/")[0],
                "bytes": e.latest.size,
                "tags": ",".join(e.latest.tags),
                "lfs": bool(e.latest.lfs_oid),
                "version": e.latest.sha,
                "parts": e.latest.parts,
                "spec": e.spec,
                "url": e.url(),
            }
            for e in entries
        ],
        columns=[*FRAME_COLUMNS],
    )


#: Columns [](`labdata.list`) leaves out unless they are asked for. Each is wide
#: and each restates something the other columns already carry.
_OPTIONAL = ("version", "spec", "url")

#: Columns [](`labdata.list`) shows, in the order it shows them: what the file
#: is, then where it came from, then what it is made of.
LIST_COLUMNS = (
    "owner", "repo", "name", "description", "date", "github", "path", "dir",
    "bytes", "tags", "lfs",
)

#: Columns ``labdata.list(brief=True)`` shows: what a file is and how big, and
#: nothing about where it is kept. ``size`` is ``bytes`` written for reading.
BRIEF_COLUMNS = ("owner", "repo", "name", "size", "description", "date")


def _size(n: int) -> str:
    """
    Write a byte count the way a result file is talked about.

    Megabytes and gigabytes of a million and a billion bytes, rather than the
    powers of two [](`labdata.core.human`) uses for the command line: these are
    the numbers a table of results is read against, and the difference of a few
    percent is not what the column is for.

    Parameters
    ----------
    n :
        Number of bytes. A negative number means the size is unknown.

    Returns
    -------
    :
        A string such as ``512.2 MB`` or ``1.4 GB``, or ``?`` when unknown.

    Examples
    --------

    ```python
    _size(512189753)
    # '512.2 MB'
    ```
    """
    if n < 0:
        return "?"
    return f"{n / 1e6:.1f} MB" if n < 1e9 else f"{n / 1e9:.1f} GB"

#: Columns [](`labdata.list`) never shows, being a detail of how a dataset is
#: stored rather than of what it holds. [](`labdata.frame`) still carries it.
_HIDDEN = ("parts",)


def list(
    repo=None,
    pattern=None,
    *,
    brief: bool = False,
    version: bool = False,
    spec: bool = False,
    url: bool = False,
    refresh: bool = False,
    progress: bool = False,
    cfg=None,
):
    """
    Tabulate what the configured repositories publish.

    The python side of ``labdata list``, and like it the version is left out
    unless asked for: it is a full commit sha, and every file a repository
    publishes carries the same one.

    Parameters
    ----------
    repo :
        Keep only repositories whose ``owner/repo`` contains this, as
        ``labdata list <repo>`` does.
    pattern :
        Keep only files whose name matches this glob, such as ``"*.csv"``.
    brief :
        Show only what a file is: ``owner``, ``repo``, ``name``, ``size``,
        ``description`` and ``date``. ``size`` is the byte count written for
        reading, as ``512.2 MB``, in place of the ``bytes`` it is counted in.
    version :
        Include the ``version`` column, the full commit sha.
    spec :
        Include the ``spec`` column, the one string naming a file at a version,
        which [](`labdata.fetch`) and the command line take. It restates the
        repository, the path and the version, so it is the widest column there
        is and is left out unless wanted.
    url :
        Include the ``url`` column, addressing that version on GitHub.
    refresh :
        Rescan before listing, rather than using the stored catalog.
    progress :
        Show a progress bar while rescanning. Only meaningful with `refresh`,
        since a stored catalog is read at once.
    cfg :
        Settings. Defaults to [](`labdata.config.Config.load`).

    Returns
    -------
    :
        A [](`pandas.DataFrame`) with one row per published file or dataset:
        ``owner``, ``repo``, ``name``, ``description``, ``date``, ``github``,
        ``path``, ``dir``, ``bytes``, ``tags`` and ``lfs``, or the six columns
        of `brief`, plus whichever of ``version``, ``spec`` and ``url`` were
        asked for. ``github`` is the repository as ``owner/repo``, and ``path``
        the file as ``dir/name``. [](`labdata.frame`) has everything, including
        the number of files a dataset holds.

    Raises
    ------
    ImportError
        If pandas is not installed. It is not a dependency of `labdata`.

    Examples
    --------

    ```python
    import labdata

    labdata.list()                        # everything published
    labdata.list(brief=True)              # just what each file is, and how big
    labdata.list("x-gwas")                # one repository
    labdata.list(pattern="*.parquet")     # by file name
    labdata.list(version=True)            # with the sha that pins each file
    labdata.list(spec=True)               # with the string the shell takes
    ```

    See Also
    --------
    [](`labdata.get`)
    [](`labdata.repos`)
    """
    import fnmatch

    entries = catalog(refresh=refresh, cfg=cfg, progress=progress)
    if repo:
        needle = repo.lower()
        entries = [e for e in entries if needle in e.repo_key.lower()]
    if pattern:
        entries = [e for e in entries if fnmatch.fnmatch(e.name, pattern)]
    table = frame(entries)
    wanted = {"version": version, "spec": spec, "url": url}
    asked = [c for c in _OPTIONAL if wanted[c]]    # asked-for columns go last
    if not brief:
        return table[[*LIST_COLUMNS, *asked]]
    table = table.assign(size=[_size(n) for n in table["bytes"]])
    return table[[*BRIEF_COLUMNS, *asked]]


def repos(*, refresh: bool = False, cfg=None):
    """
    Tabulate one row per repository that publishes something.

    The python side of ``labdata repos``.

    Parameters
    ----------
    refresh :
        Rescan before listing, rather than using the stored catalog.
    cfg :
        Settings. Defaults to [](`labdata.config.Config.load`).

    Returns
    -------
    :
        A [](`pandas.DataFrame`) with columns ``repo``, ``files``, ``bytes`` and
        ``latest``.

    Raises
    ------
    ImportError
        If pandas is not installed.

    Examples
    --------

    ```python
    labdata.repos().sort_values("bytes", ascending=False)
    ```
    """
    import pandas as pd

    entries = catalog(refresh=refresh, cfg=cfg)
    by = {}
    for e in entries:
        by.setdefault(e.repo_key, []).append(e)
    return pd.DataFrame(
        [
            {
                "repo": key,
                "files": len(group),
                "bytes": sum(x.latest.size for x in group if x.latest.size > 0),
                "latest": max(x.latest.date[:10] for x in group),
            }
            for key, group in sorted(by.items())
        ],
        columns=[*REPO_COLUMNS],
    )


def versions(entry_or_repo, filename=None, *, refresh: bool = False, cfg=None):
    """
    Tabulate the history of one published file or dataset.

    The python side of ``labdata versions``. These are the commits in which the
    file itself changed, which is the useful set to pin; the catalog stamps a
    file with the repository's current commit instead.

    Parameters
    ----------
    entry_or_repo :
        Repository name, optionally ``owner/repo``, or an
        [](`labdata.model.Entry`) already in hand.
    filename :
        File name, or as much of the path as is unambiguous. Omitted when an
        entry was given.
    refresh :
        Rescan before resolving, rather than using the stored catalog.
    cfg :
        Settings. Defaults to [](`labdata.config.Config.load`).

    Returns
    -------
    :
        A [](`pandas.DataFrame`) with columns ``version``, ``date``, ``bytes``,
        ``parts``, ``tags`` and ``subject``, newest first. ``labdata.core``
        holds a ``versions`` taking an entry and returning
        [](`labdata.model.Version`) objects, which is what this tabulates.

    Raises
    ------
    ImportError
        If pandas is not installed.
    LookupError
        If nothing matches, or several files do.

    Examples
    --------

    ```python
    labdata.versions("x-gwas", "hits.csv")
    ```

    See Also
    --------
    [](`labdata.list`)
    """
    import pandas as pd

    from . import core

    if isinstance(entry_or_repo, Entry):
        entry = entry_or_repo
    else:
        owner = None
        repo = entry_or_repo
        if "/" in repo:
            owner, _, repo = repo.partition("/")
        entries = catalog(refresh=refresh, cfg=cfg)
        entry = resolve_one(entries, Spec(repo=repo, path=filename, owner=owner))
    return pd.DataFrame(
        [
            {
                "version": v.sha,
                "date": v.date[:10],
                "bytes": v.size,
                "parts": v.parts,
                "tags": ",".join(v.tags),
                "subject": v.subject,
            }
            for v in core.versions(entry)
        ],
        columns=[*VERSION_COLUMNS],
    )


def refresh(*, cfg=None, progress: bool = True, **kwargs):
    """
    Rescan the repositories and store the catalog.

    The python side of ``labdata refresh``. The rebuilt catalog is returned, so
    a notebook cell shows what is there now.

    Parameters
    ----------
    cfg :
        Settings. Defaults to [](`labdata.config.Config.load`).
    progress :
        Show a progress bar, one step per repository. On by default: this is the
        one call that can take a while, and it is otherwise silent throughout.
    **kwargs :
        Passed to [](`labdata.list`), so ``version=True`` and ``url=True`` work
        here too.

    Returns
    -------
    :
        A [](`pandas.DataFrame`), as [](`labdata.list`) returns.

    Raises
    ------
    ImportError
        If pandas is not installed.

    Examples
    --------

    ```python
    labdata.refresh()                  # with a progress bar
    labdata.refresh(progress=False)    # without
    ```
    """
    return list(refresh=True, cfg=cfg, progress=progress, **kwargs)
