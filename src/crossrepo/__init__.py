"""
Catalog and fetch versioned result files across many git repositories.

`crossrepo` lets one project read result files produced by another without adding
a submodule and without downloading files by hand. A file is published by
committing it under a repository's ``results`` directory and naming it in the
``crossrepo.yml`` there; a results directory without a manifest publishes
nothing. The version of a file is the commit in which it last changed, so
versioning works in repositories that are never tagged.

Examples
--------

List everything the configured repositories publish:

```python
from crossrepo import catalog

for entry in catalog():
    print(entry.spec, entry.latest.size)
```

Read one file into a meta-analysis notebook. Without a version the latest is
taken and the argument that pins it printed, ready to paste into the call:

```python
import pandas as pd
from crossrepo import get

df = pd.read_csv(get("x-gwas", "hits.csv"))
# Add version="4f2a9c1e8b7d6350a1c4e9f2b8d70a3c5e1f9b24" to pin this version.

df = pd.read_csv(get("x-gwas", "hits.csv", version="4f2a9c1e8b7d6350a1c4e9f2b8d70a3c5e1f9b24"))   # pinned, silent
```

Settings are read from the configuration file. To use different ones, register
them once rather than passing them to every call:

```python
import crossrepo

crossrepo.use_config(crossrepo.Config(repos=["munch-group/x-gwas"]))
crossrepo.refresh()
df = crossrepo.list()
```

See Also
--------
[](`crossrepo.core.get`)
[](`crossrepo.core.fetch`)
[](`crossrepo.core.catalog`)
[](`crossrepo.config.use_config`)
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

from .config import Config, active_config, use_config
from .core import (
    build, catalog, diagnose, fetch, find_version, get, info, match, outdated,
    resolve_one,
)
from .model import Entry, Spec, Version

try:
    __version__ = _installed_version("crossrepo")
except PackageNotFoundError:            # a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = [
    "active_config",
    "build",
    "catalog",
    "diagnose",
    "fetch",
    "frame",
    "get",
    "info",
    "list",
    "match",
    "outdated",
    "refresh",
    "repos",
    "resolve_one",
    "use_config",
    "versions",
    "Config",
    "Entry",
    "Spec",
    "Table",
    "Version",
]


#: Columns [](`crossrepo.frame`) always has, so an empty catalog still tabulates.
#: ``owner`` and ``repo`` are the two halves of ``github``, and ``dir`` and
#: ``name`` the two halves of ``path``, so that either can be grouped or sorted
#: on without taking the other apart first.
FRAME_COLUMNS = (
    "owner", "repo", "name", "description", "get", "date", "github", "path",
    "dir", "bytes", "tags", "lfs", "version", "parts", "spec", "url",
)

#: Columns [](`crossrepo.versions`) always has.
VERSION_COLUMNS = ("version", "date", "bytes", "parts", "tags", "subject")

#: Columns [](`crossrepo.repos`) always has.
REPO_COLUMNS = ("repo", "files", "bytes", "latest")


_TABLE = None
"""The `crossrepo.table.Table` class, once something has asked for it."""


def _table():
    """
    The data frame subclass every table here is handed back as.

    Reached through here rather than imported at the top of the module, because
    the module it lives in imports pandas, which `crossrepo` does not depend on
    and which is the slowest import in the package. Nothing pays for it until a
    table is asked for.

    Returns
    -------
    :
        The [](`crossrepo.table.Table`) class, the same one on every call.
    """
    global _TABLE
    if _TABLE is None:
        from .table import Table

        _TABLE = Table
    return _TABLE


def __getattr__(name):
    """
    Hand out `Table` without importing pandas to do it.

    Parameters
    ----------
    name :
        Attribute being looked for, once the module itself has none by that
        name.

    Returns
    -------
    :
        The [](`crossrepo.table.Table`) class.

    Raises
    ------
    AttributeError
        For anything else, as a module does.
    """
    if name == "Table":
        return _table()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
        A `Table` -- a [](`pandas.DataFrame`) that left aligns its text
        columns in a notebook -- with one row per result file and columns
        ``owner``, ``repo``, ``name``, ``description``, ``get``, ``date``,
        ``github``, ``path``, ``dir``, ``bytes``, ``tags``, ``lfs``,
        ``version``, ``parts``, ``spec`` and ``url``. ``github`` is
        ``owner/repo`` and ``path`` is ``dir/name``, each carried whole as well
        as in halves. ``get`` says where the content comes from: ``local``,
        ``github``, or the name of the server it is on. ``parts`` is zero
        for an ordinary file and the number of files for a dataset published as
        a directory.

    Raises
    ------
    ImportError
        If pandas is not installed. It is not a dependency of `crossrepo`.

    Examples
    --------

    Find the largest result file in each repository:

    ```python
    from crossrepo import frame

    df = frame()
    df.sort_values("bytes").groupby("github").last()
    ```

    See Also
    --------
    [](`crossrepo.core.catalog`)
    """
    entries = catalog() if entries is None else entries
    return _table()(
        [
            {
                "owner": e.owner,
                "repo": e.repo,
                "name": e.name,
                "description": e.description,
                "get": e.fetched_from,
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


#: Columns [](`crossrepo.list`) shows: which repository, which file, where
#: reading it goes, and what it holds. The same four the command line shows, so
#: that a listing read in a terminal and one read in a notebook are one thing.
#: ``repo`` is ``owner/repo``, which is what a spec is written with.
#: [](`crossrepo.frame`) carries the rest.
LIST_COLUMNS = ("repo", "path", "get", "description")


def list(
    repo=None,
    pattern=None,
    *,
    refresh: bool = False,
    progress: bool = True,
    cfg=None,
):
    """
    Tabulate what the configured repositories publish.

    The python side of ``crossrepo list``, showing the same four columns it
    shows: which repository, which file, where reading it goes, and what it
    holds. A listing is for finding the file you want among all of them, and
    these are what that is decided on. Everything else the catalog knows is in
    [](`crossrepo.frame`), and everything it knows about one file is in
    [](`crossrepo.info`).

    Parameters
    ----------
    repo :
        Keep only repositories whose ``owner/repo`` contains this, as
        ``crossrepo list <repo>`` does.
    pattern :
        Keep only files whose name matches this glob, such as ``"*.csv"``.
    refresh :
        Rescan before listing, rather than using the stored catalog.
    progress :
        Show a progress bar while rescanning, one step per repository. On by
        default, because a listing rescans on its own account -- when there is
        no stored catalog, or the stored one has aged out -- and a scan of
        repositories on a server is not something to do silently. Nothing is
        drawn when the stored catalog is read, there being nothing to wait for.
    cfg :
        Settings. Defaults to [](`crossrepo.config.active_config`): what
        [](`crossrepo.config.use_config`) registered, or the configuration file.

    Returns
    -------
    :
        A `Table`, which is a [](`pandas.DataFrame`), with one row per published
        file or dataset and the columns `LIST_COLUMNS` names: ``repo`` as
        ``owner/repo``, ``path`` from the repository root, ``get``, and
        ``description``. ``get`` says where reading the file goes: ``local`` for
        a clone on this machine, ``github`` for a repository read over the API,
        the server's name or alias for one reached over ssh, and ``missing``
        for a file published as a link whose target was not there when the
        catalog was built.

    Raises
    ------
    ImportError
        If pandas is not installed. It is not a dependency of `crossrepo`.

    Examples
    --------

    ```python
    import crossrepo

    crossrepo.list()                        # everything published
    crossrepo.list("x-gwas")                # one repository
    crossrepo.list(pattern="*.parquet")     # by file name
    ```

    See Also
    --------
    [](`crossrepo.info`)
    [](`crossrepo.get`)
    [](`crossrepo.frame`)
    """
    entries = catalog(refresh=refresh, cfg=cfg, progress=progress)
    return _tabulate(entries, repo, pattern)


def _tabulate(entries, repo=None, pattern=None):
    """
    Filter and tabulate entries, as [](`crossrepo.list`) shows them.

    The half of [](`crossrepo.list`) that is not about getting the catalog.

    Parameters
    ----------
    entries :
        Entries to show.
    repo :
        Keep only repositories whose ``owner/repo`` contains this.
    pattern :
        Keep only files whose name matches this glob.

    Returns
    -------
    :
        A [](`pandas.DataFrame`), as [](`crossrepo.list`) returns. The
        ``github`` column is what is shown as ``repo``: a listing is what a spec
        is copied out of, and a spec names the owner.
    """
    import fnmatch

    if repo:
        needle = repo.lower()
        entries = [e for e in entries if needle in e.repo_key.lower()]
    if pattern:
        entries = [e for e in entries if fnmatch.fnmatch(e.name, pattern)]
    table = frame(entries)[["github", "path", "get", "description"]]
    return table.rename(columns={"github": "repo"})


def repos(*, refresh: bool = False, progress: bool = True, cfg=None):
    """
    Tabulate one row per repository that publishes something.

    The python side of ``crossrepo repos``.

    Parameters
    ----------
    refresh :
        Rescan before listing, rather than using the stored catalog.
    progress :
        Show a progress bar while rescanning, one step per repository, as
        [](`crossrepo.list`) and [](`crossrepo.refresh`) do. Nothing is drawn
        when the stored catalog is read.
    cfg :
        Settings. Defaults to [](`crossrepo.config.active_config`): what
        [](`crossrepo.config.use_config`) registered, or the configuration file.

    Returns
    -------
    :
        A `Table`, which is a [](`pandas.DataFrame`), with columns ``repo``,
        ``files``, ``bytes`` and
        ``latest``.

    Raises
    ------
    ImportError
        If pandas is not installed.

    Examples
    --------

    ```python
    crossrepo.repos().sort_values("bytes", ascending=False)
    ```
    """
    entries = catalog(refresh=refresh, cfg=cfg, progress=progress)
    by = {}
    for e in entries:
        by.setdefault(e.repo_key, []).append(e)
    return _table()(
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


def versions(
    entry_or_repo, filename=None, *, refresh: bool = False,
    progress: bool = True, cfg=None,
):
    """
    Tabulate the history of one published file or dataset.

    The python side of ``crossrepo versions``. These are the commits in which the
    file itself changed, which is the useful set to pin; the catalog stamps a
    file with the repository's current commit instead.

    Parameters
    ----------
    entry_or_repo :
        Repository name, optionally ``owner/repo``, or an
        [](`crossrepo.model.Entry`) already in hand.
    filename :
        File name, or as much of the path as is unambiguous. Omitted when an
        entry was given.
    refresh :
        Rescan before resolving, rather than using the stored catalog.
    progress :
        Show a progress bar while rescanning, one step per repository, as
        [](`crossrepo.list`) and [](`crossrepo.refresh`) do. Nothing is drawn
        when the stored catalog is read, and nothing when an
        [](`crossrepo.model.Entry`) was given, there being no catalog to read.
    cfg :
        Settings. Defaults to [](`crossrepo.config.active_config`): what
        [](`crossrepo.config.use_config`) registered, or the configuration file.

    Returns
    -------
    :
        A `Table`, which is a [](`pandas.DataFrame`), with columns
        ``version``, ``date``, ``bytes``,
        ``parts``, ``tags`` and ``subject``, newest first. ``crossrepo.core``
        holds a ``versions`` taking an entry and returning
        [](`crossrepo.model.Version`) objects, which is what this tabulates.

    Raises
    ------
    ImportError
        If pandas is not installed.
    LookupError
        If nothing matches, or several files do.

    Examples
    --------

    ```python
    crossrepo.versions("x-gwas", "hits.csv")
    ```

    See Also
    --------
    [](`crossrepo.list`)
    """
    from . import core

    if isinstance(entry_or_repo, Entry):
        entry = entry_or_repo
    else:
        owner = None
        repo = entry_or_repo
        if "/" in repo:
            owner, _, repo = repo.partition("/")
        entries = catalog(refresh=refresh, cfg=cfg, progress=progress)
        entry = resolve_one(entries, Spec(repo=repo, path=filename, owner=owner))
    return _table()(
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


def _select(owner=None, repo=None):
    """
    Turn an owner and a repository into the one text both halves match on.

    Parameters
    ----------
    owner :
        Owner, or `None` for any.
    repo :
        Repository, written ``repo`` or ``owner/repo``, or `None` for any.

    Returns
    -------
    :
        Text to match against ``owner/repo``, as [](`crossrepo.core.selects`) and
        [](`crossrepo.list`) both match it, or `None` when neither was given. An
        owner alone becomes ``owner/``, which no repository name can match.
    """
    if owner and repo:
        return f"{owner}/{repo}"
    if owner:
        return f"{owner}/"
    return repo or None


def refresh(*, cfg=None, progress: bool = True, owner=None, repo=None):
    """
    Rescan the repositories and store the catalog.

    The python side of ``crossrepo refresh``. Nothing is returned: rescanning
    and looking are two things, and a cell that does the first should not answer
    with three hundred rows of the second. Call [](`crossrepo.list`) to look,
    which is also what someone reading the notebook later will see was meant.

    Naming an `owner` or a `repo` rescans that much and no more, which is the
    difference between a request or two and a request for every repository an
    organisation holds. The rest of the catalog is neither rescanned nor
    forgotten: what is stored is still the whole of it, with the named
    repositories as they are now.

    Parameters
    ----------
    cfg :
        Settings. Defaults to [](`crossrepo.config.active_config`): what
        [](`crossrepo.config.use_config`) registered, or the configuration file.
    progress :
        Show a progress bar, one step per repository. On by default: this is the
        one call that can take a while, and it is otherwise silent throughout.
    owner :
        Rescan only repositories of this owner. Repositories of any other owner
        the settings name are left as the stored catalog has them, and an
        organisation that cannot hold a match is not even listed.
    repo :
        Rescan only repositories matching this, as [](`crossrepo.list`) filters on
        it: the text is matched anywhere in ``owner/repo``, ignoring case, so a
        name, a fragment of one, or a whole ``owner/repo`` all work.

    Returns
    -------
    :
        Nothing. Where `owner` or `repo` was given and nothing configured
        matches it, a [](`crossrepo.config.SourceWarning`) says so: a rescan
        that looked at nothing is otherwise indistinguishable from one that
        worked.

    Examples
    --------

    ```python
    crossrepo.refresh()                       # everything, with a progress bar
    crossrepo.refresh(progress=False)         # without the bar

    crossrepo.refresh(repo="x-gwas")          # one repository
    crossrepo.refresh(owner="munch-group")    # one organisation
    crossrepo.refresh(owner="munch-group", repo="x-gwas")

    crossrepo.refresh()                       # rescan,
    crossrepo.list()                          # then look
    ```

    See Also
    --------
    [](`crossrepo.list`)
    [](`crossrepo.core.catalog`)
    """
    import warnings

    from .config import SourceWarning

    select = _select(owner, repo)
    entries = catalog(refresh=True, cfg=cfg, progress=progress, select=select)
    if select is not None and not any(
        select.lower() in e.repo_key.lower() for e in entries
    ):
        warnings.warn(
            f"nothing matching {select!r} publishes anything; "
            "the rest of the catalog was left as it was",
            SourceWarning, stacklevel=2,
        )
