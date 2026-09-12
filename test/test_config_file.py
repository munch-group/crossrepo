"""
Tests for which configuration file is read, and for editing one.

There are two: a ``crossrepo.toml`` belonging to the directory the command is
run in, and one that applies everywhere. The first wins outright when it is
there, so what a command is about to do can be read off one file.
"""

from pathlib import Path

import pytest

from crossrepo import cli
from crossrepo.config import (
    Config, config_in_force, config_path, local_config_path,
)


@pytest.fixture()
def here(tmp_path, monkeypatch):
    """A working directory of its own, and a global config of its own with it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path


def run(*args):
    """Run the CLI, letting it find its own config file."""
    return cli.main([*args])


def settings(path: Path) -> dict:
    """The settings a written file holds, by name."""
    return vars(Config.load(path))


@pytest.fixture()
def started(here):
    """A local config file to edit, written the only way one is written."""
    assert run("config", "local", "init") == 0
    return here


# ------------------------------------------------------- which file is read

def test_a_local_file_is_read_before_the_global_one(here):
    config_path().parent.mkdir(parents=True)
    config_path().write_text('owners = ["munch-group"]\n')
    assert config_in_force() == config_path()

    (here / "crossrepo.toml").write_text('roots = ["~/work"]\n')
    assert config_in_force() == here / "crossrepo.toml"
    assert Config.load().roots == ["~/work"]


def test_a_local_file_replaces_the_global_one_rather_than_adding_to_it(here):
    """One file is in force, so what it does not say is the default."""
    config_path().parent.mkdir(parents=True)
    config_path().write_text('owners = ["munch-group"]\nmin_bytes = 99\n')
    (here / "crossrepo.toml").write_text('roots = ["~/work"]\n')

    cfg = Config.load()
    assert cfg.roots == ["~/work"]
    assert cfg.owners == []                    # not inherited
    assert cfg.min_bytes == 0                  # the default, not 99


def test_only_this_directory_is_looked_in(here, monkeypatch):
    """Nothing above the working directory can decide what gets scanned."""
    (here / "crossrepo.toml").write_text('roots = ["~/above"]\n')
    below = here / "sub" / "deeper"
    below.mkdir(parents=True)
    monkeypatch.chdir(below)
    assert config_in_force() == config_path()
    assert Config.load().roots == []


def test_the_local_path_follows_the_working_directory(here, monkeypatch):
    assert local_config_path() == here / "crossrepo.toml"
    (here / "elsewhere").mkdir()
    monkeypatch.chdir(here / "elsewhere")
    assert local_config_path() == here / "elsewhere" / "crossrepo.toml"


def test_config_says_which_of_the_two_it_read(here, capsys):
    assert run("config") == 0
    assert "(global)" in capsys.readouterr().out
    (here / "crossrepo.toml").write_text("roots = []\n")
    assert run("config") == 0
    assert "(local)" in capsys.readouterr().out


# --------------------------------------------------- asking, and creating

def test_naming_a_scope_on_its_own_only_says_what_can_be_done(here, capsys):
    """Being asked about a file must not write one.

    Click puts the help for a group given no command on standard error and
    exits two, as it does for `crossrepo` on its own; what matters here is that
    the four verbs are named and that nothing was written.
    """
    run("config", "local")
    said = capsys.readouterr().err
    for verb in ("init", "show", "set", "append", "reset"):
        assert verb in said
    assert not (here / "crossrepo.toml").exists()


def test_both_scopes_say_it(here, capsys):
    run("config", "global")
    assert "init" in capsys.readouterr().err
    assert not config_path().exists()


def test_init_writes_the_local_file(here, capsys):
    assert not (here / "crossrepo.toml").exists()
    assert run("config", "local", "init") == 0
    assert (here / "crossrepo.toml").exists()
    assert "wrote" in capsys.readouterr().out
    assert settings(here / "crossrepo.toml")["asset_dirs"] == ["results"]


def test_init_leaves_a_file_that_is_already_there_alone(here, capsys):
    (here / "crossrepo.toml").write_text('# mine\nowners = ["munch-group"]\n')
    assert run("config", "local", "init") == 0
    assert "already there" in capsys.readouterr().out
    assert "# mine" in (here / "crossrepo.toml").read_text()


def test_init_writes_the_global_file_when_asked_for_that_one(here, capsys):
    assert run("config", "global", "init") == 0
    capsys.readouterr()
    assert config_path().exists()
    assert not (here / "crossrepo.toml").exists()


# --------------------------------------------------------- show

def test_show_prints_the_file(started, capsys):
    run("config", "local", "set", "owners", "munch-group")
    capsys.readouterr()
    assert run("config", "local", "show") == 0
    out = capsys.readouterr().out
    assert "munch-group" in out
    assert out.splitlines()[0] == f"# {started / 'crossrepo.toml'}"


def test_what_show_prints_is_still_a_config_file(started, tmp_path, capsys):
    """The path it leads with is a comment, so the output can be piped."""
    run("config", "local", "set", "roots", "~/a")
    capsys.readouterr()
    run("config", "local", "show")
    copied = tmp_path / "copied.toml"
    copied.write_text(capsys.readouterr().out)
    assert Config.load(copied).roots == ["~/a"]


def test_show_keeps_the_comments_that_are_in_the_file(started, capsys):
    (started / "crossrepo.toml").write_text("# why\nowners = []\n")
    assert run("config", "local", "show") == 0
    assert "# why" in capsys.readouterr().out


def test_show_refuses_a_file_that_is_not_there(here, capsys):
    """Printing the defaults would read as settings somebody chose."""
    assert run("config", "local", "show") != 0
    assert "config local init" in capsys.readouterr().err


def test_show_works_on_the_global_file_too(here, capsys):
    run("config", "global", "init")
    run("config", "global", "set", "owners", "munch-group")
    capsys.readouterr()
    assert run("config", "global", "show") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"# {config_path()}"
    assert "munch-group" in out


# ------------------------------------------------------------ set

def test_set_writes_a_list_setting(started, capsys):
    assert run("config", "local", "set", "roots", "~/a", "~/b") == 0
    assert settings(started / "crossrepo.toml")["roots"] == ["~/a", "~/b"]


def test_set_writes_a_number(started, capsys):
    assert run("config", "local", "set", "min_bytes", "1024") == 0
    assert settings(started / "crossrepo.toml")["min_bytes"] == 1024


def test_set_replaces_rather_than_adds(started, capsys):
    run("config", "local", "set", "roots", "~/a", "~/b")
    run("config", "local", "set", "roots", "~/c")
    assert settings(started / "crossrepo.toml")["roots"] == ["~/c"]


def test_set_refuses_a_name_that_is_not_a_setting(here, capsys):
    assert run("config", "local", "set", "nosuch", "1") != 0
    assert "no setting called" in capsys.readouterr().err


def test_set_refuses_a_number_that_is_not_one(here, capsys):
    assert run("config", "local", "set", "min_bytes", "lots") != 0
    assert "not a number" in capsys.readouterr().err


def test_set_refuses_several_values_for_one(here, capsys):
    assert run("config", "local", "set", "max_bytes", "1", "2") != 0
    assert "takes one value" in capsys.readouterr().err


def test_set_works_on_the_global_file_too(here, capsys):
    run("config", "global", "init")
    assert run("config", "global", "set", "owners", "munch-group") == 0
    assert settings(config_path())["owners"] == ["munch-group"]
    assert not (here / "crossrepo.toml").exists()


# --------------------------------------------------------- append

def test_append_adds_to_what_is_there(started, capsys):
    run("config", "local", "set", "owners", "munch-group")
    assert run("config", "local", "append", "owners", "kaspermunch") == 0
    assert settings(started / "crossrepo.toml")["owners"] == [
        "munch-group", "kaspermunch",
    ]


def test_append_to_a_setting_never_written_starts_it(started, capsys):
    assert run("config", "local", "append", "roots", "~/a") == 0
    assert settings(started / "crossrepo.toml")["roots"] == ["~/a"]


def test_append_does_not_add_the_same_thing_twice(started, capsys):
    run("config", "local", "append", "owners", "munch-group")
    assert run("config", "local", "append", "owners", "munch-group") == 0
    assert settings(started / "crossrepo.toml")["owners"] == ["munch-group"]
    assert "already has" in capsys.readouterr().out


def test_append_refuses_a_setting_that_is_not_a_list(here, capsys):
    assert run("config", "local", "append", "min_bytes", "5") != 0
    assert "not a list" in capsys.readouterr().err


def test_append_refuses_a_name_that_is_not_a_setting(here, capsys):
    assert run("config", "local", "append", "nosuch", "x") != 0
    assert "no setting called" in capsys.readouterr().err


# ---------------------------------------------------------- reset

def test_reset_puts_a_setting_back_to_its_default(started, capsys):
    run("config", "local", "set", "asset_dirs", "out", "data")
    assert run("config", "local", "reset", "asset_dirs") == 0
    assert settings(started / "crossrepo.toml")["asset_dirs"] == ["results"]
    assert "asset_dirs" not in (started / "crossrepo.toml").read_text()


def test_reset_leaves_the_other_settings_alone(started, capsys):
    run("config", "local", "set", "roots", "~/a")
    run("config", "local", "set", "min_bytes", "10")
    run("config", "local", "reset", "roots")
    got = settings(started / "crossrepo.toml")
    assert got["roots"] == [] and got["min_bytes"] == 10


def test_reset_refuses_a_name_that_is_not_a_setting(here, capsys):
    assert run("config", "local", "reset", "nosuch") != 0
    assert "no setting called" in capsys.readouterr().err


def test_the_verbs_refuse_a_file_that_is_not_there(here, capsys):
    """Only `init` writes one, so the rest say to run it."""
    for args in (
        ("set", "roots", "~/a"), ("append", "roots", "~/a"), ("reset", "roots"),
    ):
        assert run("config", "local", *args) != 0
        assert "config local init" in capsys.readouterr().err
    assert not (here / "crossrepo.toml").exists()


# ------------------------------------------------- what editing leaves alone

def test_editing_keeps_the_comments_and_the_other_settings(here, capsys):
    (here / "crossrepo.toml").write_text(
        "# why these roots\n"
        'roots = [\n  "~/a",\n  "~/b",\n]\n'
        "\n# and this limit\n"
        "min_bytes = 7\n"
    )
    assert run("config", "local", "set", "roots", "~/c") == 0
    text = (here / "crossrepo.toml").read_text()
    assert "# why these roots" in text
    assert "# and this limit" in text
    assert "min_bytes = 7" in text
    assert "~/a" not in text                   # the old entries are gone
    assert settings(here / "crossrepo.toml")["roots"] == ["~/c"]


# ------------------------------- saying why nothing is configured to read

def test_the_refusal_names_the_file_actually_in_force(here, capsys):
    run("config", "global", "init")
    capsys.readouterr()
    assert run("refresh") != 0
    said = capsys.readouterr().err
    assert str(config_path()) in said
    assert "config global append" in said


def test_the_refusal_says_a_local_file_is_shadowing_the_global_one(here, capsys):
    """The whole answer, when an empty local file hides a configured global."""
    run("config", "global", "init")
    run("config", "global", "append", "owners", "munch-group")
    run("config", "local", "init")
    capsys.readouterr()
    assert run("refresh") != 0
    said = capsys.readouterr().err
    assert "read instead of" in said
    assert "does name sources, and they are not being used" in said
    assert "Delete crossrepo.toml" in said
    assert "config local append" in said


def test_it_does_not_claim_the_global_has_sources_when_it_has_none(here, capsys):
    run("config", "global", "init")
    run("config", "local", "init")
    capsys.readouterr()
    assert run("refresh") != 0
    said = capsys.readouterr().err
    assert "read instead of" in said
    assert "does name sources" not in said
