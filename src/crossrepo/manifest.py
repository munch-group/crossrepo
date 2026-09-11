"""
The per-directory manifest that decides what a repository publishes.

A result file is published by committing it *and* naming it in a ``crossrepo.yml``
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

A result file too large to commit can be published as a *link*: a symbolic
link in the results directory, made with ``ln -s``, pointing at the real file
wherever the pipeline wrote it.

```yaml
files:
  very_large_file.csv:
    description: Merged per-sample table
    sha256: "3f9a...c1"
    size: 41231234
```

Git versions the link, not the bytes behind it, so a link on its own would let
the content change without the version changing. The ``sha256`` and ``size``
close that gap: they are the *stamp*, they say which content the link stands
for, and because they are committed it is the commit that changes a stamp which
makes a new version. ``crossrepo stamp`` writes them, so regenerating the file
means stamping it again and committing that.

A stamp needs both keys, and cannot go on a glob pattern, one hash describing
one file. Content reached through a link is never copied into the cache when it
is on the same filesystem: the cache holds a hard link to it.

A link may point at a directory rather than a file, which is how a dataset too
large to commit -- a partitioned parquet directory, most often -- is published.
The stamp then carries a third key, ``parts``, saying how many files the
directory holds:

```yaml
files:
  big.parquet:
    description: Per-chromosome effect sizes
    sha256: "3f9a...c1"
    size: 41231234
    parts: 12
```

``sha256`` is taken over the whole directory rather than over one file, by
[](`crossrepo.cache.tree_hash`), and ``size`` is the total over the parts. It is
``parts`` that says a directory is meant, and it is there so that the scan knows
what it is looking at without reaching for the target: a catalog is read from
git and the manifest alone, and the content a link stands for may be on another
machine entirely.

One manifest governs a whole results directory: the ``crossrepo.yml`` sitting
directly in it, covering everything beneath. A ``crossrepo.yml`` deeper in the
tree is not read, so there is exactly one place to look to see what a repository
publishes. Manifests are read from git, not from the working tree, so an
uncommitted one publishes nothing and the same rules hold for a repository read
over the network.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MANIFEST_NAMES: Tuple[str, ...] = ("crossrepo.yml", "crossrepo.yaml")
"""
File names recognised as a manifest.

The first is what `default_path` writes; a repository spelling it ``.yaml`` is
read too.
"""

_HEX = frozenset("0123456789abcdef")
"""Characters a stamp's digest is written with; a digest is lower case."""


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
class Stamp:
    """
    The content a published link stands for.

    A link is a symbolic link committed in place of a file too large to commit,
    and git versions the link rather than the bytes behind it. The stamp is what
    supplies the missing identity: it is written in the manifest, so it is
    committed, and a commit that changes it is a new version of the file. It is
    also the cache key, which is why it is a hash of the content and not of
    anything cheaper.

    Attributes
    ----------
    sha256 :
        Hash of the content, in lower case hexadecimal. For a link to a file
        this is the digest Git LFS keys an object by, so linked and LFS content
        of equal bytes share one cached object. For a link to a directory it is
        the digest [](`crossrepo.cache.tree_hash`) takes over the whole of it.
    size :
        Size of the content in bytes, the total over the parts for a directory.
        It is checked before the hash so that a stamp left behind by a
        regenerated file is caught without reading it.
    parts :
        Number of files in the directory, or ``0`` for a link to a single file.
        A count rather than a flag because it is worth showing: it is the same
        column a dataset committed to git fills in, and the two then read alike
        in a listing.

    See Also
    --------
    [](`crossrepo.manifest.Manifest.stamp`)
    """

    sha256: str
    size: int
    parts: int = 0

    @property
    def is_directory(self) -> bool:
        """Whether the stamp describes a directory rather than one file."""
        return self.parts > 0


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
    stamps :
        The content stamps, under the same keys as `files` and only for the keys
        that carry one. A key with a stamp names a link: a symbolic link whose
        target holds the real content. Empty for a manifest that publishes no
        links.

    See Also
    --------
    [](`crossrepo.manifest.parse`)
    [](`crossrepo.manifest.Stamp`)
    """

    directory: str
    files: Dict[str, str]
    stamps: Dict[str, Stamp] = field(default_factory=dict)

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

    def stamp(self, path: str) -> Optional[Stamp]:
        """
        Look up the content a file's link stands for.

        Keys are matched as [](`crossrepo.manifest.Manifest.describe`) matches
        them, except that no glob is tried: a stamp identifies one file's
        content, so [](`crossrepo.manifest.parse`) refuses to put one on a
        pattern.

        Parameters
        ----------
        path :
            Repository-relative path of the file.

        Returns
        -------
        :
            The stamp, or `None` when the file is not published as a link. A
            file with no stamp is an ordinary committed file, versioned by git
            itself.

        Examples
        --------

        ```python
        m = parse('files:\\n  big.csv:\\n    size: 12\\n    sha256: "%s"\\n' % ("a" * 64), "results")
        m.stamp("results/big.csv")
        # Stamp(sha256='aaaa...', size=12)
        ```
        """
        key = self._stamp_key(path)
        return self.stamps[key] if key is not None else None

    def _stamp_key(self, path: str) -> Optional[str]:
        """
        Which stamped key a file is published under.

        Parameters
        ----------
        path :
            Repository-relative path of the file.

        Returns
        -------
        :
            The key as the manifest wrote it, or `None` when no stamped key
            names this file.
        """
        if not self.stamps:
            return None
        for key in self.stamps:
            if key.startswith("/") and key.strip("/") == path:
                return key
        if not self.governs(path):
            return None
        rel = self.relative(path)
        for key in (rel, rel.rsplit("/", 1)[-1]):
            if key in self.stamps:
                return key
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
        [](`crossrepo.manifest.Manifest.describe`)
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
        Content of the ``crossrepo.yml`` file.
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

    where = f"{directory}/crossrepo.yml" if directory else "crossrepo.yml"
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
    stamps: Dict[str, Stamp] = {}
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
            got = _read_stamp(where, key, value)
            if got is not None:
                stamps[key] = got
        else:
            raise ManifestError(
                f"{where}: `{key}` should be a description, or a mapping with a "
                f"`description` key"
            )
    return Manifest(directory=directory, files=files, stamps=stamps)


def _read_stamp(where: str, key: str, value: Dict[str, object]) -> Optional[Stamp]:
    """
    Read one entry's content stamp, if it has one.

    Parameters
    ----------
    where :
        Manifest path, for messages.
    key :
        Key the stamp was written under.
    value :
        The entry's mapping, which may hold ``sha256``, ``size`` and ``parts``.

    Returns
    -------
    :
        The stamp, or `None` when the entry carries none of those keys and so
        names an ordinary committed file.

    Raises
    ------
    ManifestError
        If only one of the two required keys is given, if the key is a glob, or
        if any value is not what it should be. Half a stamp is refused rather
        than ignored: it is a file whose content nothing is checking.
    """
    sha = value.get("sha256")
    size = value.get("size")
    parts = value.get("parts")
    if sha is None and size is None and parts is None:
        return None
    if _is_glob(key):
        raise ManifestError(
            f"{where}: `{key}` is a pattern and cannot carry a stamp; one "
            f"digest names one file's content, so a link is named in full"
        )
    if sha is None or size is None:
        missing = "sha256" if sha is None else "size"
        raise ManifestError(
            f"{where}: `{key}` has a stamp with no `{missing}`; a stamp needs "
            f"both, and `crossrepo stamp` writes them together"
        )
    if not isinstance(sha, str) or len(sha) != 64 or not _HEX.issuperset(sha):
        raise ManifestError(
            f"{where}: `{key}` has `sha256: {sha}`, which is not a digest; it "
            f"should be 64 lower case hexadecimal digits, as `crossrepo stamp` "
            f"writes it"
        )
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ManifestError(
            f"{where}: `{key}` has `size: {size}`, which is not a number of bytes"
        )
    if parts is None:
        parts = 0
    elif isinstance(parts, bool) or not isinstance(parts, int) or parts < 1:
        raise ManifestError(
            f"{where}: `{key}` has `parts: {parts}`, which is not a number of "
            f"files; `parts` says the link points at a directory, so it counts "
            f"at least one, and a link to a single file leaves it out"
        )
    return Stamp(sha256=sha, size=size, parts=parts)


def _find_key(lines: List[str], key: str):
    """
    Locate the line a manifest entry is written on.

    Parameters
    ----------
    lines :
        The manifest, split into lines.
    key :
        Key to find, as the manifest writes it, quoted or not.

    Returns
    -------
    :
        ``(index, indent, rest)``: the line's position, the whitespace it starts
        with, and whatever followed the colon. ``(None, "", "")`` when the key
        is not there. Only indented keys are considered, an entry always sitting
        under ``files``.
    """
    esc = re.escape(key)
    pattern = re.compile(rf"""^(\s+)(?:{esc}|"{esc}"|'{esc}')\s*:(.*)$""")
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m:
            return i, m.group(1), m.group(2)
    return None, "", ""


def write_stamp(text: str, key: str, stamp: Stamp) -> str:
    """
    Write one entry's stamp into a manifest, leaving the rest of the file alone.

    Only the entry's own ``sha256``, ``size`` and ``parts`` lines are touched, so
    comments,
    key order, quoting and formatting survive: the manifest is a file people
    write by hand and read in diffs, and a stamping run should show up in one as
    two changed lines and nothing else. An entry written as a bare description
    grows into a mapping holding that description, which is the one case where a
    line has to be rewritten rather than added.

    Parameters
    ----------
    text :
        Content of the manifest.
    key :
        Key to stamp, which must already be in the file: naming a file is how it
        is published, and stamping does not publish anything.
    stamp :
        The stamp to write.

    Returns
    -------
    :
        The manifest with the stamp written. Unchanged, apart from those lines.

    Raises
    ------
    ManifestError
        If the key is not in the file.

    Examples
    --------

    ```python
    write_stamp("files:\\n  big.csv: a table\\n", "big.csv", Stamp("a" * 64, 12))
    # 'files:\\n  big.csv:\\n    description: a table\\n    sha256: "aaa..."\\n    size: 12\\n'
    ```
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    ends = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    i, indent, rest = _find_key(lines, key)
    if i is None:
        raise ManifestError(
            f"`{key}` is not in the manifest, so there is nothing to stamp; a "
            f"file is published by naming it there first"
        )

    # Whatever the entry's own keys are indented by, so an inserted line lines
    # up with the ones already written.
    child = None
    for line in lines[i + 1:]:
        if not line.strip():
            continue
        lead = line[: len(line) - len(line.lstrip())]
        if len(lead) > len(indent):
            child = lead
        break
    if child is None:
        child = indent + "  "
    written = [f'{child}sha256: "{stamp.sha256}"', f"{child}size: {stamp.size}"]
    if stamp.parts:
        written.append(f"{child}parts: {stamp.parts}")

    body = rest.strip()
    if body and not body.startswith("#"):
        head = lines[i][: len(lines[i]) - len(rest)]
        lines[i:i + 1] = [head, f"{child}description: {body}", *written]
        return newline.join(lines) + (newline if ends else "")

    end = i + 1
    while end < len(lines):
        line = lines[end]
        if line.strip():
            lead = line[: len(line) - len(line.lstrip())]
            if len(lead) <= len(indent):
                break
        end += 1
    block = lines[i + 1:end]
    seen = set()
    kept = []
    for line in block:
        if re.match(r"^\s*sha256\s*:", line):
            line, seen = written[0], seen | {"sha256"}
        elif re.match(r"^\s*size\s*:", line):
            line, seen = written[1], seen | {"size"}
        elif re.match(r"^\s*parts\s*:", line):
            if not stamp.parts:
                continue        # a directory that is now one file: the count goes
            line, seen = written[2], seen | {"parts"}
        kept.append(line)
    block = kept
    # A blank line after the entry separates it from the next one; an inserted
    # line belongs before it, not after.
    tail = []
    while block and not block[-1].strip():
        tail.insert(0, block.pop())
    if "sha256" not in seen:
        block.append(written[0])
    if "size" not in seen:
        block.append(written[1])
    if stamp.parts and "parts" not in seen:
        block.append(written[2])
    lines[i + 1:end] = block + tail
    return newline.join(lines) + (newline if ends else "")


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
