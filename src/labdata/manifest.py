"""
The per-directory manifest that decides what a repository publishes.

A result file is published by committing it *and* naming it in a ``labdata.yml``
beside it. Committing alone is not enough: a results directory without a
manifest publishes nothing, which keeps working files, intermediates and
scratch output out of other people's catalogs without anyone having to tidy up.

The manifest also carries the one thing a file name cannot: what the file is.

```yaml
files:
  candidates.csv: Sweep candidates, one row per gene
  tmrca_stats.hdf: TMRCA per 100 kb window, autosomes only
  "SRR*/QC_table.txt": Per-sample Hi-C quality summary
```

Keys are file names, paths relative to the manifest, or glob patterns, so a
directory holding three hundred per-sample tables does not need three hundred
lines. Values are descriptions; the longer form ``{description: ...}`` is
accepted so that the format has somewhere to grow.

A key beginning with ``/`` is a path from the repository root rather than from
the manifest, which is how a result that does not live in the results directory
is published. Any tracked file can be named this way:

```yaml
files:
  hits.csv: In the results directory, as usual
  /data/reference/samples.csv: Somewhere else in the repository
  /data/raw/*.tsv: A pattern, matched against the path from the root
```

A key cannot climb out with ``..``. There is one spelling for a path that leaves
the results directory, so that what a manifest reaches is plain to read from the
key alone.

One manifest governs a whole results directory: the ``labdata.yml`` sitting
directly in it, covering everything beneath. A ``labdata.yml`` deeper in the
tree is not read, so there is exactly one place to look to see what a repository
publishes. Manifests are read from git, not from the working tree, so an
uncommitted one publishes nothing and the same rules hold for a repository read
over the network.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

MANIFEST_NAMES: Tuple[str, ...] = ("labdata.yml", "labdata.yaml")
"""File names recognised as a manifest."""


class ManifestError(ValueError):
    """Raised when a manifest cannot be understood."""


def is_manifest(path: str) -> bool:
    """
    Test whether a repository path is a manifest.

    Parameters
    ----------
    path :
        Repository-relative path.

    Returns
    -------
    :
        `True` for a manifest, which is never itself a published result file.
    """
    return path.rsplit("/", 1)[-1] in MANIFEST_NAMES


@dataclass(frozen=True)
class Manifest:
    """
    What one directory of a repository publishes.

    Attributes
    ----------
    directory :
        Repository-relative directory the manifest governs, ``""`` at the root
        of the repository.
    files :
        Keys in the order they were written, each mapped to its description. A
        key is a path relative to `directory`, a bare file name, a glob pattern
        over either, or -- written with a leading ``/`` -- a path or pattern
        from the root of the repository, naming a file outside `directory`.

    See Also
    --------
    [](`labdata.manifest.parse`)
    """

    directory: str
    files: Dict[str, str]

    def governs(self, path: str) -> bool:
        """
        Test whether a file lies in the part of the tree this manifest covers.

        Parameters
        ----------
        path :
            Repository-relative path of the file.

        Returns
        -------
        :
            `True` if `path` is inside `directory`.
        """
        if not self.directory:
            return True
        return path.startswith(self.directory + "/")

    def relative(self, path: str) -> str:
        """
        Express a repository path relative to the manifest.

        Parameters
        ----------
        path :
            Repository-relative path of a file this manifest governs.

        Returns
        -------
        :
            The path as the manifest would write it.
        """
        if not self.directory:
            return path
        return path[len(self.directory) + 1:]

    def describe(self, path: str) -> Optional[str]:
        """
        Look up what a file is, and thereby whether it is published at all.

        An exact key wins over a glob, and a full path wins over a bare file
        name, so a specific description can be given for one file among many
        covered by a pattern. A key written from the repository root is the most
        specific spelling of all and is tried first. Globs are tried in the
        order they were written.

        A key that does not start with ``/`` only ever reaches inside
        `directory`: a bare file name is matched against files this manifest
        governs, never against the same name elsewhere in the repository.

        Parameters
        ----------
        path :
            Repository-relative path of the file.

        Returns
        -------
        :
            The description, ``""`` when the file is published with none, or
            `None` when it is not published.

        Examples
        --------

        ```python
        m = parse('files:\\n  "*.csv": a table\\n  hits.csv: the good one\\n', "results")
        m.describe("results/hits.csv")
        # 'the good one'
        ```
        """
        anchored = self._anchored()
        if path in anchored:
            return anchored[path]
        inside = self.governs(path)
        rel = name = ""
        if inside:
            rel = self.relative(path)
            name = rel.rsplit("/", 1)[-1]
            for key in (rel, name):
                if key in self.files:
                    return self.files[key]
        for key, description in self.files.items():
            if not _is_glob(key):
                continue
            if key.startswith("/"):
                if fnmatch.fnmatch(path, key.strip("/")):
                    return description
            elif inside and (
                fnmatch.fnmatch(rel, key) or fnmatch.fnmatch(name, key)
            ):
                return description
        return None

    def anchored_prefixes(self) -> List[str]:
        """
        Where to look for the files this manifest names from the repository root.

        A key written from the root may name a file anywhere in the repository,
        so finding what it names means listing more than the results directory.
        Each such key contributes the literal part of its path, up to its first
        glob character, which is the least that has to be listed.

        Returns
        -------
        :
            Repository-relative paths to list, without duplicates and without
            any that lies inside another. ``""`` stands for the whole
            repository. Empty when the manifest names nothing outside
            `directory`.

        See Also
        --------
        [](`labdata.manifest.Manifest.describe`)
        """
        prefixes = set()
        for key in self._anchored():
            literal = []
            for part in key.split("/"):
                if _is_glob(part):
                    break
                literal.append(part)
            prefixes.add("/".join(literal))
        if "" in prefixes:
            return [""]
        return sorted(
            p for p in prefixes
            if not any(p.startswith(o + "/") for o in prefixes if o != p)
        )

    def _anchored(self) -> Dict[str, str]:
        """
        The keys written from the repository root, as repository paths.

        Returns
        -------
        :
            Each such key with its leading slash removed, so that it can be
            matched against a repository-relative path directly, mapped to its
            description.
        """
        return {
            key.strip("/"): description
            for key, description in self.files.items()
            if key.startswith("/")
        }


def _is_glob(key: str) -> bool:
    """
    Test whether a manifest key is a pattern rather than a literal name.

    Parameters
    ----------
    key :
        Key as written in the manifest.

    Returns
    -------
    :
        `True` if the key contains glob syntax.
    """
    return any(c in key for c in "*?[")


def parse(text: str, directory: str) -> Manifest:
    """
    Read a manifest.

    Parameters
    ----------
    text :
        Content of the ``labdata.yml`` file.
    directory :
        Repository-relative directory the manifest sits in, used to make its
        keys relative and to report where a problem is.

    Returns
    -------
    :
        The parsed manifest. An empty ``files`` mapping is allowed and publishes
        nothing.

    Raises
    ------
    ManifestError
        If the file is not valid YAML, has no ``files`` mapping, gives an entry
        a value that is neither a description nor a mapping holding one, or
        names a file with a key that climbs out of the directory with ``..``.

    Examples
    --------

    ```python
    parse("files:\\n  hits.csv: candidate hits\\n", "results")
    # Manifest(directory='results', files={'hits.csv': 'candidate hits'})
    ```
    """
    import yaml                      # a dependency only of this module

    where = f"{directory}/labdata.yml" if directory else "labdata.yml"
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"{where} is not valid YAML: {exc}") from None
    if doc is None:
        return Manifest(directory=directory, files={})
    if not isinstance(doc, dict):
        raise ManifestError(f"{where} should be a mapping with a `files` key")
    if "files" not in doc:
        raise ManifestError(
            f"{where} has no `files` key; it should look like\n"
            f"  files:\n"
            f"    hits.csv: what this file holds"
        )
    listed = doc["files"]
    if listed is None:
        return Manifest(directory=directory, files={})
    if not isinstance(listed, dict):
        raise ManifestError(
            f"{where}: `files` should map each file name to its description"
        )
    files: Dict[str, str] = {}
    for key, value in listed.items():
        if not isinstance(key, str):
            raise ManifestError(f"{where}: {key!r} is not a file name")
        if not key.strip("/"):
            raise ManifestError(f"{where}: {key!r} names no file")
        if ".." in key.split("/"):
            raise ManifestError(
                f"{where}: `{key}` climbs out of the directory with `..`; a file "
                f"elsewhere in the repository is named from the root instead, as "
                f"`/data/{key.split('/')[-1]}`"
            )
        if value is None:
            files[key] = ""
        elif isinstance(value, str):
            files[key] = value
        elif isinstance(value, dict) and isinstance(
            value.get("description", ""), str
        ):
            files[key] = value.get("description", "")
        else:
            raise ManifestError(
                f"{where}: `{key}` should be a description, or a mapping with a "
                f"`description` key"
            )
    return Manifest(directory=directory, files=files)


def manifest_path(results_dir: str) -> str:
    """
    Where a results directory's manifest must sit to be read.

    Parameters
    ----------
    results_dir :
        Repository-relative results directory, as the repository spells it.

    Returns
    -------
    :
        The path of the one manifest governing it.
    """
    return f"{results_dir}/{MANIFEST_NAMES[0]}"
