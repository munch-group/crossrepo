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

Read one file into a meta-analysis notebook, pinned to a version:

```python
import pandas as pd
from labdata import fetch

df = pd.read_csv(fetch("munch-group/xwas:hits.csv@e4f5a6b"))
```

See Also
--------
[](`labdata.core.fetch`)
[](`labdata.core.catalog`)
"""

from .config import Config
from .core import build, catalog, fetch, match, resolve_one, versions
from .model import Entry, Spec, Version

__version__ = "0.1.16"

__all__ = [
    "build",
    "catalog",
    "fetch",
    "frame",
    "match",
    "resolve_one",
    "versions",
    "Config",
    "Entry",
    "Spec",
    "Version",
]


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
        ``repo``, ``path``, ``name``, ``version``, ``date``, ``bytes``,
        ``tags``, ``lfs`` and ``spec``.

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
    df.sort_values("bytes").groupby("repo").last()
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
                "repo": e.repo_key,
                "path": e.path,
                "name": e.name,
                "version": e.latest.short,
                "date": e.latest.date[:10],
                "bytes": e.latest.size,
                "tags": ",".join(e.latest.tags),
                "lfs": bool(e.latest.lfs_oid),
                "spec": e.spec,
            }
            for e in entries
        ]
    )
