"""
Configuration and standard locations.

The defaults work with no configuration file present. A file is only needed to
point [](`labdata.core.build`) at the directories where repositories live.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

DEFAULT_INCLUDE = [
    "*.csv", "*.tsv", "*.txt", "*.parquet", "*.pq",
    "*.h5", "*.hdf", "*.hdf5", "*.store",
    "*.json", "*.jsonl", "*.xlsx", "*.bed", "*.gff", "*.vcf", "*.vcf.gz",
    "*.pkl", "*.pickle", "*.npy", "*.npz", "*.feather", "*.zarr",
]
"""File name patterns cataloged unless excluded."""

DEFAULT_EXCLUDE = ["*.png", "*.pdf", "*.svg", "*.html", "*.md", ".gitkeep", "*.log"]
"""File name patterns never cataloged, applied before `DEFAULT_INCLUDE`."""


def config_path() -> Path:
    """
    Location of the configuration file.

    Honours ``XDG_CONFIG_HOME``.

    Returns
    -------
    :
        Path of ``config.toml``, whether or not it exists.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "labdata" / "config.toml"


def cache_root() -> Path:
    """
    Location of the local cache.

    Honours ``LABDATA_CACHE`` first and ``XDG_CACHE_HOME`` second.

    Returns
    -------
    :
        Root directory of the cache, whether or not it exists.

    See Also
    --------
    [](`labdata.cache.blob_path`)
    """
    if os.environ.get("LABDATA_CACHE"):
        return Path(os.environ["LABDATA_CACHE"]).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "labdata"


@dataclass
class Config:
    """
    Settings controlling which files are cataloged.

    Attributes
    ----------
    roots :
        Directories searched for git working trees. ``~`` is expanded.
    depth :
        How many levels below each root to search.
    results_dirs :
        Directories within a repository holding published result files.
        Matching is case-insensitive, so a repository that committed
        ``Results/`` is found as well.
    include :
        File name patterns to catalog.
    exclude :
        File name patterns to skip, applied before `include`.
    min_bytes :
        Skip files smaller than this. Zero disables the check.
    max_bytes :
        Skip files larger than this. Zero disables the check.

    Examples
    --------

    ```python
    cfg = Config(roots=["~/github-backup/munch-group"], depth=1)
    entries = build(cfg)
    ```

    See Also
    --------
    [](`labdata.core.build`)
    """

    roots: List[str] = field(default_factory=lambda: ["~"])
    depth: int = 2
    results_dirs: List[str] = field(default_factory=lambda: ["results"])
    include: List[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE))
    exclude: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    min_bytes: int = 0
    max_bytes: int = 0

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        """
        Read settings from a TOML file.

        Parameters
        ----------
        path :
            File to read. Defaults to [](`labdata.config.config_path`). A
            missing file is not an error and yields the defaults.

        Returns
        -------
        :
            The settings.

        Raises
        ------
        ValueError
            If the file contains keys that are not settings.
        RuntimeError
            On Python older than 3.11 when `tomli` is not installed.
        """
        path = path or config_path()
        if not path.exists():
            return cls()
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - python < 3.11
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ModuleNotFoundError:
                raise RuntimeError(
                    f"reading {path} needs python >= 3.11 or the `tomli` package"
                ) from None
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown keys in {path}: {', '.join(sorted(unknown))}")
        return cls(**data)

    def write_default(self, path: Optional[Path] = None) -> Path:
        """
        Write a commented configuration file holding these settings.

        Parameters
        ----------
        path :
            File to write. Defaults to [](`labdata.config.config_path`). Parent
            directories are created. An existing file is overwritten.

        Returns
        -------
        :
            The path written.
        """
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        roots = "\n".join(f'  "{r}",' for r in self.roots)
        inc = "\n".join(f'  "{p}",' for p in self.include)
        exc = "\n".join(f'  "{p}",' for p in self.exclude)
        path.write_text(
            "# Where to look for repos. Each root is searched `depth` levels deep\n"
            "# for working trees; a directory containing .git is a repo.\n"
            f"roots = [\n{roots}\n]\n"
            f"depth = {self.depth}\n\n"
            "# Directory inside each repo that holds published result files.\n"
            "# Matched case-insensitively, so Results/ is found too.\n"
            f"results_dirs = {self.results_dirs!r}\n\n"
            "# Only tracked files matching `include` and not `exclude` are cataloged.\n"
            f"include = [\n{inc}\n]\n"
            f"exclude = [\n{exc}\n]\n\n"
            "# Size filters in bytes; 0 disables the limit.\n"
            f"min_bytes = {self.min_bytes}\n"
            f"max_bytes = {self.max_bytes}\n",
            encoding="utf-8",
        )
        return path
