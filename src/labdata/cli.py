"""
Command line interface.

Every subcommand prints specs in the form the other subcommands accept, so a
line of output can be pasted straight into the next command.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import cache
from . import core as cat
from .config import Config, config_path
from .model import Entry, Spec


def human(n: int) -> str:
    """
    Format a byte count for a table column.

    Parameters
    ----------
    n :
        Number of bytes. A negative number means the size is unknown.

    Returns
    -------
    :
        A short string such as ``948B`` or ``488.5M``, or ``?`` when unknown.

    Examples
    --------

    ```python
    human(512189753)
    # '488.5M'
    ```
    """
    if n < 0:
        return "?"
    size = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def table(rows: List[List[str]], headers: List[str]) -> str:
    """
    Render rows as a plain text table with aligned columns.

    Parameters
    ----------
    rows :
        Rows of cells, each row the same length as `headers`.
    headers :
        Column headings.

    Returns
    -------
    :
        The rendered table, or an empty string when there are no rows.
    """
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    head = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = "\n".join(
        "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows
    )
    return f"{head}\n{sep}\n{body}"


def _config(args: argparse.Namespace) -> Config:
    """
    Load the settings named on the command line.

    Parameters
    ----------
    args :
        Parsed arguments, whose `config` attribute may name a file.

    Returns
    -------
    :
        The settings.
    """
    return Config.load(Path(args.config).expanduser() if args.config else None)


def _entries(args: argparse.Namespace) -> List[Entry]:
    """
    Get the catalog for a command invocation.

    Parameters
    ----------
    args :
        Parsed arguments, whose `refresh` attribute forces a rescan.

    Returns
    -------
    :
        The catalog entries.
    """
    return cat.catalog(refresh=args.refresh, cfg=_config(args))


def cmd_list(args: argparse.Namespace) -> int:
    """
    Print the result files in the catalog.

    Parameters
    ----------
    args :
        Parsed arguments, using `repo` to filter by repository substring,
        `pattern` to filter by file name glob and `json` to print machine
        readable output.

    Returns
    -------
    :
        Process exit status; ``1`` when nothing matched.
    """
    entries = _entries(args)
    if args.repo:
        needle = args.repo.lower()
        entries = [e for e in entries if needle in e.repo_key.lower()]
    if args.pattern:
        entries = [e for e in entries if fnmatch.fnmatch(e.name, args.pattern)]
    if args.json:
        print(json.dumps([e.to_dict() for e in entries], indent=1))
        return 0
    if not entries:
        print(
            "nothing found. `labdata refresh` to rescan, or check `labdata config`.",
            file=sys.stderr,
        )
        return 1
    rows = [
        [
            e.repo_key, e.path, e.latest.short, e.latest.date[:10],
            human(e.latest.size),
            ",".join(e.latest.tags) or ("lfs" if e.latest.lfs_oid else ""),
        ]
        for e in entries
    ]
    print(table(rows, ["REPO", "PATH", "VERSION", "DATE", "SIZE", "NOTE"]))
    print(f"\n{len(entries)} files in {len({e.repo_key for e in entries})} repos")
    return 0


def cmd_repos(args: argparse.Namespace) -> int:
    """
    Print one line per repository holding result files.

    Parameters
    ----------
    args :
        Parsed arguments.

    Returns
    -------
    :
        Process exit status.
    """
    entries = _entries(args)
    by = {}
    for e in entries:
        by.setdefault(e.repo_key, []).append(e)
    rows = [
        [
            k, str(len(v)),
            human(sum(x.latest.size for x in v if x.latest.size > 0)),
            max(x.latest.date[:10] for x in v),
        ]
        for k, v in sorted(by.items())
    ]
    print(table(rows, ["REPO", "FILES", "SIZE", "LATEST"]))
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    """
    Print the history of one result file.

    Parameters
    ----------
    args :
        Parsed arguments, whose `spec` names the file.

    Returns
    -------
    :
        Process exit status.
    """
    entries = _entries(args)
    entry = cat.resolve_one(entries, Spec.parse(args.spec))
    rows = [
        [v.short, v.date[:10], human(v.size), ",".join(v.tags), v.subject[:60]]
        for v in cat.versions(entry)
    ]
    print(f"{entry.repo_key}:{entry.path}\n")
    print(table(rows, ["VERSION", "DATE", "SIZE", "TAGS", "COMMIT"]))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    """
    Fetch one version of a result file and print its path.

    The path is the only thing written to standard output, so the command
    composes with others in a shell pipeline.

    Parameters
    ----------
    args :
        Parsed arguments, whose `spec` names the file and version and whose
        `out` optionally names a copy to write.

    Returns
    -------
    :
        Process exit status.
    """
    entries = _entries(args)
    spec = Spec.parse(args.spec)
    entry = cat.resolve_one(entries, spec)
    version = cat.find_version(entry, spec.version or "latest")
    path = cat.materialize(entry, version)
    if args.out:
        dest = Path(args.out).expanduser()
        if dest.is_dir():
            dest = dest / entry.name
        with open(path, "rb") as src, open(dest, "wb") as fh:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        path = dest
    print(path)
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    """
    Rescan the repositories and store the catalog.

    Parameters
    ----------
    args :
        Parsed arguments.

    Returns
    -------
    :
        Process exit status.
    """
    entries = cat.build(_config(args))
    cat.save(entries)
    print(
        f"cataloged {len(entries)} files "
        f"in {len({e.repo_key for e in entries})} repos"
    )
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """
    Show the settings in force, or write a configuration file.

    Parameters
    ----------
    args :
        Parsed arguments, whose `init` writes a file and whose `force` allows
        overwriting one.

    Returns
    -------
    :
        Process exit status; ``1`` when a file exists and `force` was not given.
    """
    path = config_path()
    if args.init:
        if path.exists() and not args.force:
            print(f"{path} exists; --force to overwrite", file=sys.stderr)
            return 1
        Config().write_default(path)
        print(f"wrote {path}")
        return 0
    cfg = Config.load()
    suffix = "" if path.exists() else "  (not present -- using defaults)"
    print(f"config file: {path}{suffix}")
    for k, v in vars(cfg).items():
        print(f"  {k} = {v!r}")
    n, total = cache.usage()
    print(f"cache: {n} objects, {human(total)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """
    Construct the argument parser.

    Returns
    -------
    :
        A parser whose subcommands each set a ``func`` default.
    """
    p = argparse.ArgumentParser(
        prog="labdata",
        description="Catalog and fetch versioned result files across git repos.",
    )
    p.add_argument("--config", help="path to config.toml")
    p.add_argument(
        "--refresh", action="store_true",
        help="rescan repos instead of using the cached catalog",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("list", help="list result files")
    q.add_argument("repo", nargs="?", help="filter by repo (substring)")
    q.add_argument("-p", "--pattern", help="filter by filename glob, e.g. '*.csv'")
    q.add_argument("--json", action="store_true", help="print machine readable output")
    q.set_defaults(func=cmd_list)

    q = sub.add_parser("repos", help="one line per repo")
    q.set_defaults(func=cmd_repos)

    q = sub.add_parser("versions", help="history of one file")
    q.add_argument("spec", help="[owner/]repo:path")
    q.set_defaults(func=cmd_versions)

    q = sub.add_parser("get", help="fetch a file into the cache and print its path")
    q.add_argument("spec", help="[owner/]repo:path[@version]")
    q.add_argument("-o", "--out", help="also write a copy here")
    q.set_defaults(func=cmd_get)

    q = sub.add_parser("refresh", help="rebuild the catalog")
    q.set_defaults(func=cmd_refresh)

    q = sub.add_parser("config", help="show or create the config file")
    q.add_argument("--init", action="store_true", help="write a config file")
    q.add_argument("--force", action="store_true", help="overwrite an existing file")
    q.set_defaults(func=cmd_config)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """
    Run the command line interface.

    Expected failures are reported as a one line message on standard error
    rather than a traceback.

    Parameters
    ----------
    argv :
        Arguments to parse. Defaults to `sys.argv`.

    Returns
    -------
    :
        Process exit status.

    Examples
    --------

    ```python
    main(["list", "--pattern", "*.csv"])
    ```
    """
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (LookupError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
