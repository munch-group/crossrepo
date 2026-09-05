"""
Command line interface.

Every subcommand prints specs in the form the other subcommands accept, so a
line of output can be pasted straight into the next command. The commands are
built with `click`; [](`labdata.cli.main`) wraps the group so that an expected
failure becomes a one line message rather than a traceback, and so that the
process exit status is returned rather than raised.

``--config`` and ``--refresh`` are accepted both before and after the
subcommand, so ``labdata --refresh list`` and ``labdata list --refresh`` mean
the same thing.
"""

from __future__ import annotations

import fnmatch
import json
import sys
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import click

from . import __version__, cache, gitutil, manifest
from . import core as cat
from .config import Config, SourceWarning, cache_root, config_path
from .core import human
from .gitutil import GitError
from .model import Entry, Spec

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}
"""Show usage for both ``-h`` and ``--help``."""


def note(version) -> str:
    """
    Summarise what is unusual about a version, for the table's NOTE column.

    Parameters
    ----------
    version :
        Version to describe.

    Returns
    -------
    :
        Its tags, whether it is held in Git LFS or published as a symbolic link,
        and how many parts it has if it is a dataset, comma separated; empty for
        an ordinary tagless file.

    Examples
    --------

    ```python
    note(entry.latest)
    # 'v1.0,12 parts'
    ```
    """
    bits = list(version.tags)
    if version.lfs_oid:
        bits.append("lfs")
    if version.link is not None:
        # Worth saying: it means the content is not in the repository, so a
        # clone without the pipeline's output cannot read it.
        bits.append("link")
    if version.parts:
        bits.append(f"{version.parts} parts")
    return ",".join(bits)


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


def catalog_options(f: Callable) -> Callable:
    """
    Add the options shared by every command that reads the catalog.

    The same two options sit on the group, so they may be given on either side
    of the subcommand.

    Parameters
    ----------
    f :
        Command callback to decorate.

    Returns
    -------
    :
        The decorated callback, taking `config_file` and `refresh`.
    """
    f = click.option(
        "--refresh",
        is_flag=True,
        default=False,
        help="rescan repos instead of using the cached catalog",
    )(f)
    f = click.option(
        "--config",
        "config_file",
        metavar="PATH",
        default=None,
        type=click.Path(dir_okay=False),
        help="path to config.toml",
    )(f)
    return f


def _shared(ctx: click.Context) -> Dict[str, Any]:
    """
    Read the options given before the subcommand.

    Parameters
    ----------
    ctx :
        Click context, whose `obj` the group fills in.

    Returns
    -------
    :
        A mapping with ``config`` and ``refresh`` keys.
    """
    return ctx.obj or {}


def _settings(
    ctx: click.Context, config_file: Optional[str] = None, refresh: bool = False
) -> Tuple[Config, bool]:
    """
    Combine the group level and subcommand level options.

    Parameters
    ----------
    ctx :
        Click context.
    config_file :
        Configuration file named after the subcommand, if any.
    refresh :
        Whether a rescan was asked for after the subcommand.

    Returns
    -------
    :
        ``(settings, refresh)``, where either level may supply either value.
    """
    shared = _shared(ctx)
    path = config_file or shared.get("config")
    cfg = Config.load(Path(path).expanduser() if path else None)
    return cfg, bool(refresh or shared.get("refresh"))


def _require_sources(cfg: Config) -> None:
    """
    Stop with an actionable message when there is nothing to read.

    Both settings are empty until configured, so the alternative is a command
    that quietly reports nothing and gives no clue why.

    Parameters
    ----------
    cfg :
        Settings to check.

    Raises
    ------
    click.ClickException
        If neither local roots nor GitHub owners or repos are configured.
    """
    if cfg.roots or cfg.owners or cfg.repos:
        return
    raise click.ClickException(
        "nothing is configured to read.\n"
        f"Run `labdata config --init` to write {config_path()}, then set either\n"
        "  owners = [\"munch-group\"]        # read GitHub directly, nothing cloned\n"
        "  roots  = [\"~/projects\"]         # or scan clones already on this machine\n"
        "  roots  = [\"me@server:~/projects\"] # or clones on a server, over ssh"
    )


@contextmanager
def _collecting() -> Iterator[List[warnings.WarningMessage]]:
    """
    Gather what a scan says about itself, to be reported when it is over.

    Warnings are how a scan says that a root, an organisation or a repository
    could not be read, since one unreachable source must not cost the others.
    Python would print each with the file and line it came from, which says
    nothing to the person who wrote the settings, so they are collected here and
    handed to [](`labdata.cli._report`) instead.

    Yields
    ------
    :
        The list the warnings are collected into, filled by the end of the
        block.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield caught


def _report(caught: List[warnings.WarningMessage]) -> None:
    """
    Print what a scan could not read, as a list of sources and their reasons.

    Parameters
    ----------
    caught :
        Warnings raised during the scan. Anything that is not about a source is
        printed on its own line; the sources are counted and listed together,
        each once however many repositories it cost, and any further line of a
        message is indented under it as the advice it is.
    """
    sources: Dict[str, None] = {}
    for entry in caught:
        said = str(entry.message)
        if issubclass(entry.category, SourceWarning):
            sources.setdefault(said, None)
        else:
            click.echo(f"warning: {said}", err=True)
    if not sources:
        return
    count = len(sources)
    click.echo(
        f"{count} configured source{'' if count == 1 else 's'} could not be read:",
        err=True,
    )
    for said in sources:
        first, *rest = said.splitlines()
        click.echo(f"  {first}", err=True)
        for line in rest:                 # what to do about it, set apart
            click.echo(f"    {line}", err=True)


def _entries(
    ctx: click.Context, config_file: Optional[str], refresh: bool
) -> List[Entry]:
    """
    Get the catalog for a command invocation.

    Parameters
    ----------
    ctx :
        Click context.
    config_file :
        Configuration file named after the subcommand, if any.
    refresh :
        Whether a rescan was asked for after the subcommand.

    Returns
    -------
    :
        The catalog entries.
    """
    cfg, do_refresh = _settings(ctx, config_file, refresh)
    _require_sources(cfg)
    with _collecting() as caught:         # silent unless something was scanned
        entries = cat.catalog(refresh=do_refresh, cfg=cfg)
    _report(caught)
    return entries


@click.group(context_settings=CONTEXT_SETTINGS)
@click.option(
    "--config",
    "config_file",
    metavar="PATH",
    type=click.Path(dir_okay=False),
    help="path to config.toml",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="rescan repos instead of using the cached catalog",
)
@click.version_option(__version__, "-V", "--version", prog_name="labdata")
@click.pass_context
def cli(ctx: click.Context, config_file: Optional[str], refresh: bool) -> None:
    """Catalog and fetch versioned result files across git repos."""
    ctx.obj = {"config": config_file, "refresh": refresh}


def _explain_empty(
    ctx: click.Context, config_file: Optional[str], refresh: bool
) -> None:
    """
    Say why the catalog is empty, in terms of what was actually looked at.

    Parameters
    ----------
    ctx :
        Click context.
    config_file :
        Configuration file named after the subcommand, if any.
    refresh :
        Whether a rescan was asked for.
    """
    cfg, _ = _settings(ctx, config_file, refresh)
    click.echo("nothing is published by anything configured. What was looked at:", err=True)
    for line in cat.diagnose(cfg):
        click.echo(line, err=True)
    click.echo(
        "\nA repo publishes by committing a labdata.yml in the results directory\n"
        "at its root, naming the files:\n"
        "  files:\n"
        "    hits.csv: what this file holds\n"
        "To read repos on GitHub without cloning them, set in your config:\n"
        '  owners = ["munch-group"]',
        err=True,
    )


@cli.command("list")
@click.argument("repo", required=False)
@click.option("-p", "--pattern", metavar="GLOB", help="filter by filename glob, e.g. '*.csv'")
@click.option("--json", "as_json", is_flag=True, help="print machine readable output")
@click.option("--version", "--sha", "show_version", is_flag=True,
              help="add the version, the full commit sha, as a last column")
@click.option("-u", "--url", "show_url", is_flag=True,
              help="add the URL of each file's version on GitHub")
@catalog_options
@click.pass_context
def cmd_list(
    ctx: click.Context,
    repo: Optional[str],
    pattern: Optional[str],
    as_json: bool,
    show_version: bool,
    show_url: bool,
    config_file: Optional[str],
    refresh: bool,
) -> int:
    """
    List result files, optionally limited to one repo.

    The version is left out unless asked for: it is a full commit sha, which is
    wide, and it is the same for every file a repository publishes.
    """
    entries = _entries(ctx, config_file, refresh)
    if repo:
        needle = repo.lower()
        entries = [e for e in entries if needle in e.repo_key.lower()]
    if pattern:
        entries = [e for e in entries if fnmatch.fnmatch(e.name, pattern)]
    if as_json:
        click.echo(json.dumps([e.to_dict() for e in entries], indent=1))
        return 0
    if not entries:
        _explain_empty(ctx, config_file, refresh)
        return 1
    headers = ["REPO", "PATH", "DATE", "SIZE", "NOTE", "DESCRIPTION"]
    rows = [
        [
            e.repo_key, e.path, e.latest.date[:10],
            human(e.latest.size),
            note(e.latest),
            e.description,
        ]
        for e in entries
    ]
    if show_version:
        headers.append("VERSION")
        for row, entry in zip(rows, entries):
            row.append(entry.latest.sha)
    if show_url:
        headers.append("URL")
        for row, entry in zip(rows, entries):
            row.append(entry.url())
    click.echo(table(rows, headers))
    click.echo(f"\n{len(entries)} files in {len({e.repo_key for e in entries})} repos")
    return 0


@cli.command("repos")
@catalog_options
@click.pass_context
def cmd_repos(ctx: click.Context, config_file: Optional[str], refresh: bool) -> int:
    """Print one line per repo holding result files."""
    entries = _entries(ctx, config_file, refresh)
    by: Dict[str, List[Entry]] = {}
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
    click.echo(table(rows, ["REPO", "FILES", "SIZE", "LATEST"]))
    return 0


@cli.command("versions")
@click.argument("spec")
@catalog_options
@click.pass_context
def cmd_versions(
    ctx: click.Context, spec: str, config_file: Optional[str], refresh: bool
) -> int:
    """Print the history of one result file, given as [owner/]repo:path."""
    entries = _entries(ctx, config_file, refresh)
    entry = cat.resolve_one(entries, Spec.parse(spec))
    rows = [
        [v.sha, v.date[:10], human(v.size), note(v), v.subject[:60]]
        for v in cat.versions(entry)
    ]
    click.echo(f"{entry.repo_key}:{entry.path}\n")
    click.echo(table(rows, ["VERSION", "DATE", "SIZE", "NOTE", "COMMIT"]))
    return 0


@cli.command("get")
@click.argument("spec")
@click.option("-o", "--out", metavar="PATH", help="also write a copy here")
@click.option("-u", "--url", "show_url", is_flag=True,
              help="print the URL of the version instead of downloading it")
@catalog_options
@click.pass_context
def cmd_get(
    ctx: click.Context,
    spec: str,
    out: Optional[str],
    show_url: bool,
    config_file: Optional[str],
    refresh: bool,
) -> int:
    """
    Fetch [owner/]repo:path[@version] into the cache and print its path.

    The path is the only thing written to standard output, so the command
    composes in a shell pipeline. When no version is given, the one chosen is
    named on standard error; when one is given and the file has changed since,
    that is said there instead.
    """
    entries = _entries(ctx, config_file, refresh)
    parsed = Spec.parse(spec)
    entry = cat.resolve_one(entries, parsed)
    version = cat.find_version(entry, parsed.version or "latest")
    if show_url:
        address = entry.url(version)
        if not address:
            raise click.ClickException(
                f"{entry.repo_key} has no GitHub origin, so its files have no URL"
            )
        click.echo(address)
        return 0
    path = cat.materialize(entry, version)
    if parsed.version is None:
        click.echo(
            f"{entry.repo_key}:{entry.path}@{version.sha}  "
            f"({version.date[:10]}, {human(version.size)})",
            err=True,
        )
    else:
        stale = cat.outdated(entry, version)
        if stale:
            click.echo(stale, err=True)
    if out:
        path = cat.copy_out(path, out, entry.name)
    click.echo(str(path))
    return 0


@cli.command("refresh")
@catalog_options
@click.pass_context
def cmd_refresh(ctx: click.Context, config_file: Optional[str], refresh: bool) -> int:
    """
    Rescan the repos and store the catalog.

    A progress bar is drawn, one step per repository, when standard error is a
    terminal. Any root, organisation or repository that could not be read is
    named on standard error afterwards; the catalog is still written from what
    could be, and the status stays zero, since a scan that reached most of its
    sources has done its job.
    """
    cfg, _ = _settings(ctx, config_file, refresh)
    _require_sources(cfg)
    # A bar belongs on a terminal, not in a log or a pipe.
    with _collecting() as caught:
        entries = cat.build(cfg, progress=sys.stderr.isatty())
    cat.save(entries, cfg)
    click.echo(
        f"cataloged {len(entries)} files "
        f"in {len({e.repo_key for e in entries})} repos"
    )
    _report(caught)                       # what was read, then what was not
    if not entries:
        _explain_empty(ctx, config_file, refresh)
        return 1
    return 0


@cli.command("config")
@click.option("--init", "init", is_flag=True, help="write a config file")
@click.option("--force", is_flag=True, help="overwrite an existing file")
@click.pass_context
def cmd_config(ctx: click.Context, init: bool, force: bool) -> int:
    """
    Show the settings in force, or write a config file.

    A ``--config PATH`` given before the subcommand names the file to show or
    write, instead of the default location.
    """
    named = _shared(ctx).get("config")
    path = Path(named).expanduser() if named else config_path()
    if init:
        if path.exists() and not force:
            click.echo(f"{path} exists; --force to overwrite", err=True)
            return 1
        Config().write_default(path)
        click.echo(f"wrote {path}")
        return 0
    cfg = Config.load(path)
    suffix = "" if path.exists() else "  (not present -- using defaults)"
    click.echo(f"config file: {path}{suffix}")
    for k, v in vars(cfg).items():
        click.echo(f"  {k} = {v!r}")
    n, total = cache.usage()
    click.echo(f"cache: {n} objects, {human(total)}")
    return 0


def _manifest_file(root: Path, wanted: str) -> Optional[Path]:
    """
    Find the manifest of one results directory in a working tree.

    The directory is matched without regard to case, as the scan matches it, so
    a repository spelling it ``Results`` is stamped like any other.

    Parameters
    ----------
    root :
        Top of the working tree.
    wanted :
        Results directory as the settings spell it, relative to `root`.

    Returns
    -------
    :
        Path of the ``labdata.yml``, or `None` when the directory or the
        manifest is not there.
    """
    here = root
    for part in wanted.strip("/").split("/"):
        if not part:
            return None
        if (here / part).is_dir():
            here = here / part
            continue
        got = [c for c in sorted(here.iterdir()) if c.is_dir() and c.name.lower() == part.lower()]
        if not got:
            return None
        here = got[0]
    for name in manifest.MANIFEST_NAMES:
        if (here / name).is_file():
            return here / name
    return None


def _key_path(governing: manifest.Manifest, key: str) -> str:
    """
    The file one literal manifest key names, as a repository path.

    Parameters
    ----------
    governing :
        Manifest the key was written in.
    key :
        Key as written, either relative to the manifest or, with a leading
        ``/``, from the root of the repository.

    Returns
    -------
    :
        The repository-relative path.
    """
    if key.startswith("/"):
        return key.strip("/")
    return f"{governing.directory}/{key}" if governing.directory else key


def _stamp_one(root: Path, path: str) -> Tuple[Optional[manifest.Stamp], str]:
    """
    Hash what a published symbolic link points at.

    Parameters
    ----------
    root :
        Top of the working tree.
    path :
        Repository-relative path of the link.

    Returns
    -------
    :
        ``(stamp, "")`` for a link that resolves, or ``(None, reason)`` when it
        does not. Reading the target is the expensive part of stamping, and it
        is the only way to know what the link stands for.
    """
    here = root / path
    target = here.readlink()
    resolved = target if target.is_absolute() else here.parent / target
    if not resolved.is_file():
        return None, f"points at {target}, which is not a file here"
    return manifest.Stamp(
        sha256=cache.content_hash(resolved), size=resolved.stat().st_size
    ), ""


def _links_under(directory: Path) -> Iterator[Path]:
    """
    Every symbolic link in a directory tree.

    Parameters
    ----------
    directory :
        Directory to walk.

    Yields
    ------
    :
        Each symbolic link found, at any depth. A link to a directory is not
        descended into, there being no telling where it leads.
    """
    for here in sorted(directory.iterdir()):
        if here.is_symlink():
            yield here
        elif here.is_dir():
            yield from _links_under(here)


@cli.command("stamp")
@click.argument(
    "path", required=False, type=click.Path(exists=True, file_okay=False)
)
@click.option(
    "--check", is_flag=True,
    help="say what is out of date and write nothing; for a hook or for CI",
)
@click.pass_context
def cmd_stamp(ctx: click.Context, path: Optional[str], check: bool) -> int:
    """
    Record what the published symbolic links in a repository point at.

    A result file too large to commit is published as a symbolic link to
    wherever the pipeline wrote it, together with a stamp in the ``labdata.yml``
    saying which content the link stands for. Git versions the link and not the
    bytes, so it is the stamp that makes the version: this command writes it,
    and committing the manifest publishes the new version.

    Only the two stamp lines of each entry are rewritten, so comments and
    formatting survive and a stamping run reads as itself in a diff. A file must
    already be named in the manifest to be stamped, naming it being how it is
    published in the first place.
    """
    cfg, _ = _settings(ctx)
    start = Path(path).expanduser() if path else Path.cwd()
    try:
        top = str(gitutil.git(start, "rev-parse", "--show-toplevel")).strip()
    except GitError:
        raise click.ClickException(f"{start} is not in a git repository") from None
    root = Path(top)

    done: List[str] = []
    problems: List[str] = []
    current = 0
    for wanted in cfg.labdata_dirs:
        where = _manifest_file(root, wanted)
        if where is None:
            continue
        rel = where.parent.relative_to(root).as_posix()
        directory = "" if rel == "." else rel
        text = where.read_text(encoding="utf-8")
        try:
            governing = manifest.parse(text, directory)
        except manifest.ManifestError as exc:
            problems.append(str(exc))
            continue
        wanted_stamps: Dict[str, manifest.Stamp] = {}
        handled: set = set()
        for key in governing.files:
            if any(c in key for c in "*?["):
                continue                  # a pattern names many files, so none
            path_in_repo = _key_path(governing, key)
            handled.add(path_in_repo)
            here = root / path_in_repo
            if not here.is_symlink():
                if governing.stamp(path_in_repo) is not None:
                    problems.append(
                        f"{path_in_repo} carries a stamp but is not a symbolic "
                        f"link; git holds its content, so the stamp does nothing"
                    )
                continue
            got, why = _stamp_one(root, path_in_repo)
            if got is None:
                problems.append(f"{path_in_repo} {why}")
                continue
            if governing.stamp(path_in_repo) == got:
                current += 1
                continue
            was = "updated" if governing.stamp(path_in_repo) is not None else "stamped"
            done.append(f"  {was}  {path_in_repo}  ({human(got.size)})")
            wanted_stamps[key] = got
            text = manifest.write_stamp(text, key, got)
        # A pattern cannot carry a stamp, so a link published only by one would
        # be passed over in silence by both this and the scan.
        for link in sorted(_links_under(where.parent)):
            path_in_repo = link.relative_to(root).as_posix()
            if path_in_repo in handled:
                continue
            if governing.describe(path_in_repo) is None:
                continue
            problems.append(
                f"{path_in_repo} is published by a pattern, which cannot carry "
                f"a stamp; name the file in full in {where.name} to publish it"
            )
        if not wanted_stamps or check:
            continue
        # A surgical edit is checked by reading the file back: the stamps must be
        # there, and everything else must still parse.
        try:
            after = manifest.parse(text, directory)
        except manifest.ManifestError as exc:
            raise click.ClickException(
                f"stamping {where} would leave it unreadable ({exc}); nothing "
                f"was written"
            ) from None
        if any(after.stamps.get(k) != v for k, v in wanted_stamps.items()):
            raise click.ClickException(
                f"stamping {where} did not take effect as written; nothing was "
                f"written"
            )
        where.write_text(text, encoding="utf-8")

    for line in done:
        click.echo(line)
    for line in problems:
        click.echo(f"  {line}", err=True)
    if not done and not problems:
        click.echo(
            f"{current} published link{'' if current == 1 else 's'} up to date"
            if current else "nothing is published as a link here"
        )
        return 0
    if check:
        if done:
            click.echo(
                f"{len(done)} stamp{'' if len(done) == 1 else 's'} out of date; "
                f"run `labdata stamp`",
                err=True,
            )
        return 1
    if done:
        click.echo(
            f"stamped {len(done)} file{'' if len(done) == 1 else 's'}; "
            f"commit the labdata.yml to publish this version"
        )
    return 1 if problems else 0


@cli.command("cache")
@click.option(
    "--verify", "do_verify", is_flag=True,
    help="re-hash every cached object and report any that do not match its key",
)
@click.option(
    "--repair", is_flag=True,
    help="remove the objects that fail verification; implies --verify",
)
def cmd_cache(do_verify: bool, repair: bool) -> int:
    """
    Show what the local cache holds, and check that it is sound.

    Verification re-hashes every object and compares the digest with the key it
    is stored under, so it says whether the cache still holds what it claims.
    ``--repair`` removes the objects that fail, together with the readable links
    that stand for them; their content is fetched again the next time it is
    asked for.
    """
    stored = cache.objects()
    total = sum(p.stat().st_size for p in stored)
    if not (do_verify or repair):
        click.echo(f"cache: {cache_root()}")
        click.echo(f"  {len(stored)} objects, {human(total)}")
        return 0

    bad = []
    with click.progressbar(
        stored, label=f"verifying {len(stored)} objects", file=sys.stderr
    ) as items:
        for path in items:
            problem = cache.check_object(path)
            if problem:
                bad.append((path, problem))

    if not bad:
        click.echo(f"{len(stored)} objects, {human(total)}, all sound")
        return 0

    click.echo(
        f"{len(bad)} of {len(stored)} objects do not hold what their key promises:",
        err=True,
    )
    for path, problem in bad:
        click.echo(f"  {path.name[:16]}...  {problem}", err=True)
    if not repair:
        click.echo(
            "run `labdata cache --repair` to remove them; "
            "the content is fetched again the next time it is used",
            err=True,
        )
        return 1
    removed = []
    for path, _ in bad:
        removed.extend(cache.discard(path))
    click.echo(
        f"removed {len(bad)} objects and {len(removed) - len(bad)} readable links; "
        "the content is fetched again the next time it is used"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """
    Run the command line interface.

    Expected failures are reported as a short message on standard error rather
    than a traceback, and the exit status is returned rather than raised, so the
    entry point can be called from a test. `OSError` is among them because a
    directory on the way can be unreadable or simply never answer.

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
    try:
        rv = cli.main(args=argv, prog_name="labdata", standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.exceptions.Abort:
        click.echo("aborted", err=True)
        return 130
    except (LookupError, ValueError, OSError, GitError) as exc:
        click.echo(f"error: {exc}", err=True)
        return 1
    return rv if isinstance(rv, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
