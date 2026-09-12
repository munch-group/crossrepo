#!/usr/bin/env python
"""
Write the command line reference page from the command line itself.

The commands are read out of click rather than described by hand, so the page
cannot drift from the tool: a command added, an option renamed or a piece of
help reworded shows up here by rerunning this. Run it from the repository root,
or through ``scripts/docs-build-render.sh``, which does it before rendering.

    python scripts/docs-cli-reference.py
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import List, Tuple

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crossrepo.cli import cli                                  # noqa: E402

PAGE = Path(__file__).resolve().parent.parent / "docs" / "pages" / "cli.qmd"

PREAMBLE = """---
title: Command line
# Help text is prose, not markdown, and a spec is written [owner/]repo:path
# [@version]. Pandoc reads `@version` as a citation and renders it as one, so
# citation syntax is turned off for a page that has no citations in it.
from: markdown-citations
---

Every command, as `crossrepo --help` reports it. The python API mirrors it, one
call per command, and is documented in the [API reference](../api/index.qmd).

A `--config PATH` given before a command names the configuration file to use
instead of the one that would be found; `--refresh` before or after one rescans
the repositories rather than reading the stored catalog.

::: {.callout-note collapse="true" title="Where settings come from"}
A `crossrepo.toml` in the working directory is read *instead of*
`~/.config/crossrepo/config.toml`, not on top of it, and only the working
directory itself is looked in. `crossrepo config` says which of the two is in
force.
:::
"""


def plain(text: str) -> str:
    """
    Keep help text meaning what it says once markdown gets hold of it.

    Help is written as prose, not as markdown, so a square bracket in it is a
    square bracket. Pandoc would read ``[owner/]repo`` as a link that goes
    nowhere and drop the brackets, which is the one part of a spec a reader most
    needs to see.

    Parameters
    ----------
    text :
        Help text as click holds it.

    Returns
    -------
    :
        The same, with the characters markdown would take for its own escaped.
    """
    return text.replace("[", "\\[").replace("]", "\\]")


def cell(text: str) -> str:
    """
    Help text as one cell of a table.

    Parameters
    ----------
    text :
        Help text.

    Returns
    -------
    :
        The same on one line, with any pipe escaped: an unescaped one would end
        the cell and shift every column after it.
    """
    return plain(" ".join(text.split())).replace("|", "\\|")


def anchor(name: str) -> str:
    """
    The heading id one command is linked by.

    Parameters
    ----------
    name :
        Full command name, such as ``crossrepo config local set``.

    Returns
    -------
    :
        The id, with the spaces turned into hyphens.
    """
    return name.replace(" ", "-")


def prose(command: click.Command) -> str:
    """
    A command's help, as paragraphs.

    Parameters
    ----------
    command :
        Command to describe.

    Returns
    -------
    :
        Its help text, dedented and with the paragraph breaks kept. Empty when
        the command has none.
    """
    text = inspect.cleandoc(command.help or "")
    return "\n".join(plain(line.rstrip()) for line in text.splitlines())


def usage(command: click.Command, name: str) -> str:
    """
    The usage line, as click would print it.

    Parameters
    ----------
    command :
        Command to describe.
    name :
        Full command name.

    Returns
    -------
    :
        The line, without the leading ``Usage:``.
    """
    ctx = click.Context(command, info_name=name)
    return " ".join([name, *command.collect_usage_pieces(ctx)])


def options(command: click.Command) -> List[Tuple[str, str]]:
    """
    What a command takes, other than its arguments.

    Parameters
    ----------
    command :
        Command to describe.

    Returns
    -------
    :
        ``(spelling, help)`` for each option, in the order they were declared.
        ``--help`` is left out, being on everything and saying the same thing
        every time.
    """
    out = []
    for param in command.params:
        if not isinstance(param, click.Option) or param.name == "help":
            continue
        spelling = ", ".join([*param.opts, *param.secondary_opts])
        if not param.is_flag and param.metavar:
            spelling += f" {param.metavar}"
        elif not param.is_flag:
            spelling += f" {param.name.upper()}"
        out.append((spelling, param.help or ""))
    return out


def arguments(command: click.Command) -> List[str]:
    """
    What a command takes positionally.

    Parameters
    ----------
    command :
        Command to describe.

    Returns
    -------
    :
        The argument names as the usage line spells them. Click carries no help
        for an argument, so what each one is belongs in the command's own help
        and is not invented here.
    """
    return [
        p.make_metavar(click.Context(command))
        if "ctx" in inspect.signature(p.make_metavar).parameters
        else p.make_metavar()
        for p in command.params
        if isinstance(p, click.Argument)
    ]


def walk(command: click.Command, name: str) -> List[Tuple[str, click.Command]]:
    """
    Every command in the tree, parents before children.

    Parameters
    ----------
    command :
        Command to start at.
    name :
        Its full name.

    Returns
    -------
    :
        ``(full_name, command)`` for the command and everything under it, in the
        order they should be read.
    """
    found = [(name, command)]
    if isinstance(command, click.Group):
        for child in sorted(command.commands):
            found.extend(walk(command.commands[child], f"{name} {child}"))
    return found


def render(name: str, command: click.Command, depth: int) -> str:
    """
    One command's section of the page.

    Parameters
    ----------
    name :
        Full command name.
    command :
        Command to render.
    depth :
        How deep it sits, so that a subcommand is a subheading of its group.

    Returns
    -------
    :
        The section, ending in a blank line.
    """
    out = [f"{'#' * min(depth + 2, 5)} `{name}` {{#{anchor(name)}}}", ""]
    out += ["```console", usage(command, name), "```", ""]
    body = prose(command)
    if body:
        out += [body, ""]
    taken = arguments(command)
    if taken:
        out += [f"Takes {' and '.join(f'`{a}`' for a in taken)}.", ""]
    got = options(command)
    if got:
        out += ["| Option | |", "|---|---|"]
        out += [f"| `{spelling}` | {cell(help_)} |" for spelling, help_ in got]
        out += [""]
    if isinstance(command, click.Group):
        out += ["| Subcommand | |", "|---|---|"]
        for child in sorted(command.commands):
            sub = command.commands[child]
            ctx = click.Context(sub, info_name=child)
            out += [
                f"| [`{name} {child}`](#{anchor(f'{name} {child}')}) "
                f"| {cell(sub.get_short_help_str(limit=90))} |"
            ]
        out += [""]
    return "\n".join(out)


def main() -> int:
    """
    Write the page.

    Returns
    -------
    :
        Zero.
    """
    sections = [PREAMBLE]
    for name, command in walk(cli, "crossrepo"):
        sections.append(render(name, command, name.count(" ")))
    PAGE.write_text("\n".join(sections).rstrip("\n") + "\n", encoding="utf-8")
    print(f"wrote {PAGE} ({len(walk(cli, 'crossrepo'))} commands)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
