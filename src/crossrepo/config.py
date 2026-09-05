"""
Configuration and standard locations.

Everything has a default except the one thing only you can know: which
directories your repositories live in. `roots` starts empty rather than at your
home directory, because scanning a whole home directory reaches into synced
folders and network mounts that can take a very long time to answer, or never
answer at all.
"""

from __future__ import annotations

import os
import pprint
import warnings
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Dict, List, Optional, Set

DEFAULT_INCLUDE: List[str] = []
"""
File name patterns to catalog, empty by default.

Guessing at extensions was how the catalog was decided before there was a
manifest. What a repository publishes is now its own to state, so this is empty
and everything published is cataloged. It remains as a filter for a reader who
wants only part of what is published, such as ``["*.csv"]``.
"""

DEFAULT_EXCLUDE: List[str] = []
"""File name patterns to skip, applied before `DEFAULT_INCLUDE`. Empty by default."""

RETIRED: Set[str] = {"depth"}
"""
Settings that no longer exist, ignored with a warning rather than refused.

A configuration file written by an earlier version must still load, or the tool
stops working on the day it is updated. ``depth`` decided how far below each
root to look for repositories; a root is now either a repository itself or a
directory holding them.
"""

RENAMED: Dict[str, str] = {"results_dirs": "crossrepo_dirs"}
"""
Settings that changed name, read under the old one with a warning.

The old name still says what was meant, so it is honoured rather than refused,
for the same reason as `RETIRED`.
"""


_WIDTH = 80
"""
Column to wrap a [](`crossrepo.config.Config`) at when showing it.

The width `pprint.pprint` uses, since that is what its output is meant to look
like.
"""


class SourceWarning(UserWarning):
    """
    Raised when something the settings name cannot be read.

    A root that is not there, a server that does not answer, an organisation or
    repository GitHub will not show: each costs part of the catalog rather than
    all of it, so a scan carries on and says at the end what it could not reach.
    Silence would be worse than either: a catalog missing half a group's results
    looks exactly like a group that published half as much.
    """


def _toml_list(name: str, values: List[str]) -> str:
    """
    Render a list setting as TOML.

    Parameters
    ----------
    name :
        Setting name.
    values :
        Its values. An empty list is written on one line, since a blank block
        reads as though something were missing.

    Returns
    -------
    :
        The setting, ending in a newline.
    """
    if not values:
        return f"{name} = []\n"
    body = "\n".join(f'  "{v}",' for v in values)
    return f"{name} = [\n{body}\n]\n"


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
    return Path(base) / "crossrepo" / "config.toml"


def cache_root() -> Path:
    """
    Location of the local cache.

    Honours ``CROSSREPO_CACHE`` first and ``XDG_CACHE_HOME`` second.

    Returns
    -------
    :
        Root directory of the cache, whether or not it exists.

    See Also
    --------
    [](`crossrepo.cache.blob_path`)
    """
    if os.environ.get("CROSSREPO_CACHE"):
        return Path(os.environ["CROSSREPO_CACHE"]).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "crossrepo"


@dataclass
class Config:
    """
    Settings controlling which files are cataloged.

    Attributes
    ----------
    roots :
        Directories searched for git working trees. Each is either a repository
        itself or a directory whose immediate subdirectories are repositories;
        nothing deeper is looked at. ``~`` is expanded. A directory on another
        machine is written ``user@host:path`` and read over ssh, without being
        cloned or mounted. Empty by default: nothing is scanned until this is
        set. Naming the directories your repositories are in keeps the scan away
        from synced folders such as OneDrive, which can block for a long time on
        a directory that is not there.
    owners :
        GitHub organisations or users whose repositories are cataloged over the
        API, without being cloned. Every repository they own is looked at, at
        the cost of one request each for those that publish nothing.
    repos :
        Further GitHub repositories to catalog, written ``owner/repo``, for ones
        outside `owners`.
    crossrepo_dirs :
        Directories within a repository holding a ``crossrepo.yml``, and with it
        the result files it publishes, given as paths relative to the repository
        root. They may be at any depth, so
        ``analysis/step3/results`` works as well as ``results``. Matching is
        case-insensitive, so a repository that committed ``Results/`` is found
        too. Each is searched, and each needs its own ``crossrepo.yml``. A
        manifest may publish files from anywhere else in its repository, by
        naming them from the repository root.
    include :
        File name patterns to catalog, on top of what the manifests publish.
        Empty means everything published.
    exclude :
        File name patterns to skip, applied before `include`.
    min_bytes :
        Skip files smaller than this. Zero disables the check.
    max_bytes :
        Skip files larger than this. Zero disables the check.

    Examples
    --------

    ```python
    cfg = Config(roots=["~/github-backup/munch-group", "kmt@genome.au.dk:~/projects"])
    entries = build(cfg)
    ```

    See Also
    --------
    [](`crossrepo.core.build`)
    """

    roots: List[str] = field(default_factory=list)
    owners: List[str] = field(default_factory=list)
    repos: List[str] = field(default_factory=list)
    crossrepo_dirs: List[str] = field(default_factory=lambda: ["results"])
    include: List[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE))
    exclude: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    min_bytes: int = 0
    max_bytes: int = 0

    def __repr__(self) -> str:
        """
        Show the settings one to a line, as [](`pprint.pprint`) would.

        The settings are a page of lists, and the single line a dataclass gives
        by default runs off the side of a notebook cell with the include
        patterns halfway along it. This is what `pprint.pprint` makes of the
        same object: one setting to a line, long lists broken and aligned. A
        short enough object still comes back on one line, as it does there.

        Returns
        -------
        :
            The settings as they would be written to construct them, wrapped to
            eighty columns.

        Examples
        --------

        ```python
        crossrepo.active_config()
        # Config(roots=[],
        #        owners=['munch-group'],
        #        repos=[],
        #        crossrepo_dirs=['results'],
        #        include=[],
        #        exclude=[],
        #        min_bytes=0,
        #        max_bytes=0)
        ```
        """
        name = type(self).__name__
        shown = [(f.name, getattr(self, f.name)) for f in fields(self) if f.repr]
        flat = f"{name}({', '.join(f'{k}={v!r}' for k, v in shown)})"
        if len(flat) <= _WIDTH:
            return flat
        indent = len(name) + 1
        lines = []
        for i, (key, value) in enumerate(shown):
            offset = indent + len(key) + 1
            allowance = 0 if i == len(shown) - 1 else 1
            text = pprint.pformat(value, width=_WIDTH - offset - allowance)
            lines.append(f"{key}={text}".replace("\n", "\n" + " " * offset))
        return f"{name}(" + (",\n" + " " * indent).join(lines) + ")"

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        """
        Read settings from a TOML file.

        Parameters
        ----------
        path :
            File to read. Defaults to [](`crossrepo.config.config_path`). A
            missing file is not an error and yields the defaults.

        Returns
        -------
        :
            The settings.

        Raises
        ------
        ValueError
            If the file contains keys that are not settings. A key that used to
            be one is not refused, so that a file written by an earlier version
            still loads: one listed in `RETIRED` is ignored with a warning, and
            one listed in `RENAMED` is read under its new name, also with a
            warning.
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
        renamed = sorted(set(data) & set(RENAMED))
        for key in renamed:
            value = data.pop(key)
            data.setdefault(RENAMED[key], value)     # the new name wins
        if renamed:
            warnings.warn(
                f"{path}: "
                + "; ".join(f"`{k}` is now `{RENAMED[k]}`" for k in renamed),
                stacklevel=2,
            )
        retired = sorted(set(data) & RETIRED)
        for key in retired:
            del data[key]
        if retired:
            warnings.warn(
                f"{path}: `{'`, `'.join(retired)}` no longer does anything and can "
                f"be deleted; a root is now either a repository or a directory "
                f"holding them",
                stacklevel=2,
            )
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
            File to write. Defaults to [](`crossrepo.config.config_path`). Parent
            directories are created. An existing file is overwritten.

        Returns
        -------
        :
            The path written.
        """
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.roots:
            roots = (
                "roots = [\n"
                + "\n".join(f'  "{r}",' for r in self.roots)
                + "\n]\n"
            )
        else:
            roots = (
                "# Nothing is scanned until this is filled in. Naming the\n"
                "# directories your repos are in keeps the scan out of synced\n"
                "# folders and network mounts, which can be very slow.\n"
                "roots = []\n"
            )
        inc = _toml_list("include", self.include)
        exc = _toml_list("exclude", self.exclude)
        path.write_text(
            "# Where to look for repos. Each root is either a repo itself or a\n"
            "# directory holding repos; a directory containing .git is a repo.\n"
            "# A root on another machine is written user@host:path and read\n"
            "# over ssh, with whatever access ssh already grants. E.g.\n"
            '#   roots = ["~/projects", "kmt@genome.au.dk:~/projects"]\n'

            f"{roots}\n"
            "# GitHub organisations or users to catalog over the API. Nothing is\n"
            "# cloned; repositories need not be checked out at all. E.g.\n"
            '#   owners = ["munch-group"]\n'
            f"{_toml_list('owners', self.owners)}"
            f"{_toml_list('repos', self.repos)}\n"
            "# Directories inside each repo holding a crossrepo.yml, and with it\n"
            "# the result files it publishes. Paths from the repo root, at any\n"
            "# depth, matched case-insensitively, so Results/ is found too. A\n"
            "# crossrepo.yml may publish files elsewhere in the repo as well, by\n"
            "# naming them from the repo root, as /data/samples.csv.\n"
            f"crossrepo_dirs = {self.crossrepo_dirs!r}\n\n"
            "# What a repo publishes is decided by its crossrepo.yml. These\n"
            "# narrow that on the reading side; empty means everything\n"
            "# published, e.g. include = [\"*.csv\"].\n"
            f"{inc}"
            f"{exc}\n"
            "# Size filters in bytes; 0 disables the limit.\n"
            f"min_bytes = {self.min_bytes}\n"
            f"max_bytes = {self.max_bytes}\n",
            encoding="utf-8",
        )
        return path


_registered: Optional[Config] = None
"""
Settings registered with `use_config`, or `None` while the file is read.

Process-wide rather than per-thread: a config registered in a notebook cell
applies to every call that follows, including ones made from a worker thread,
which is what a reader who has just registered one expects.
"""


class Registration:
    """
    The undo for one call to [](`crossrepo.config.use_config`).

    Returned rather than used directly. Ignoring it leaves the registration in
    place for the rest of the session; entering it with ``with`` puts the
    previous settings back on the way out. It shows itself as the settings now
    in effect, so that a registration made as the last line of a notebook cell
    prints something worth reading.

    See Also
    --------
    [](`crossrepo.config.use_config`)
    [](`crossrepo.config.active_config`)
    """

    def __init__(self, previous: Optional[Config]) -> None:
        self._previous = previous
        self._undone = False

    def __repr__(self) -> str:
        """
        Show the settings now in effect, as [](`crossrepo.config.Config`) does.

        The handle itself is nothing to look at, and a notebook shows the last
        expression in a cell, which for a registration made as a statement is
        this. What the call put in effect is worth seeing there; where the
        handle lives in memory is not.

        Returns
        -------
        :
            What [](`crossrepo.config.active_config`) returns, shown as it shows
            itself. Asked after the registration has been undone, it says what
            is in effect then rather than what this call once registered: it
            reports the settings, not the history.
        """
        return repr(active_config())

    def __enter__(self) -> Config:
        return active_config()

    def __exit__(self, *exc) -> bool:
        self.undo()
        return False

    def undo(self) -> None:
        """
        Put back the settings that were registered before this call.

        Doing it twice is not an error: the second time does nothing, so a
        registration undone by hand inside a ``with`` block is not undone again
        on the way out.
        """
        global _registered
        if not self._undone:
            self._undone = True
            _registered = self._previous


def active_config() -> Config:
    """
    The settings in effect for calls that are not given any.

    Returns
    -------
    :
        The settings registered with [](`crossrepo.config.use_config`), or, while
        none is registered, the ones read from
        [](`crossrepo.config.config_path`). Every function taking a ``cfg``
        argument falls back to this when it is left out, so this says what such
        a call is about to use.

    Examples
    --------

    ```python
    crossrepo.active_config().owners
    ```

    See Also
    --------
    [](`crossrepo.config.use_config`)
    [](`crossrepo.config.Config.load`)
    """
    return _registered if _registered is not None else Config.load()


def use_config(cfg: Optional[Config] = None, **overrides) -> Registration:
    """
    Register settings for the rest of the session, or for a block.

    Saves passing ``cfg=cfg`` to every call: what is registered here is what
    [](`crossrepo.config.active_config`) returns, and with it what every function
    taking a ``cfg`` argument uses when it is not given one. An explicit ``cfg``
    still wins, so a single call can always step outside what is registered.

    Parameters
    ----------
    cfg :
        Settings to register, replacing whatever is in effect. `None` with no
        `overrides` unregisters, so the configuration file is read again.
    **overrides :
        Individual settings to change, named as the fields of
        [](`crossrepo.config.Config`). They are applied on top of `cfg`, or, when
        that is left out, on top of the configuration file, so one setting can
        be changed without restating the rest.

    Returns
    -------
    :
        A [](`crossrepo.config.Registration`), which is the undo. Ignore it to
        register for the rest of the session, or use it as a context manager to
        register for a block and put back the previous settings afterwards.

    Raises
    ------
    TypeError
        If `cfg` is not a [](`crossrepo.config.Config`), or if an override does
        not name one of its fields. The message lists the fields there are.

    Examples
    --------

    Register once, then call everything without `cfg`:

    ```python
    import crossrepo

    crossrepo.use_config(crossrepo.Config(repos=["munch-group/x-gwas"]))
    df = crossrepo.refresh()
    ```

    Change one setting and keep the rest of the configuration file:

    ```python
    crossrepo.use_config(repos=["munch-group/x-gwas"])
    ```

    Register for a block only:

    ```python
    with crossrepo.use_config(owners=["munch-group"]):
        df = crossrepo.list()
    ```

    Go back to the configuration file:

    ```python
    crossrepo.use_config(None)
    ```

    See Also
    --------
    [](`crossrepo.config.active_config`)
    [](`crossrepo.config.Config`)
    """
    global _registered
    if cfg is not None and not isinstance(cfg, Config):
        raise TypeError(f"cfg should be a Config, not {type(cfg).__name__}")
    fields = set(Config.__dataclass_fields__)
    unknown = sorted(set(overrides) - fields)
    if unknown:
        raise TypeError(
            f"{', '.join(unknown)} is not a setting; there is "
            f"{', '.join(sorted(fields))}"
        )
    previous = _registered
    if overrides:
        cfg = replace(cfg if cfg is not None else Config.load(), **overrides)
    _registered = cfg
    return Registration(previous)
