"""
Tests for repositories on another machine, read over ssh.

No server is involved: ``CROSSREPO_SSH`` points at a stand-in that runs the
command it is given on this machine, with its own home directory, so what is
being tested is everything on this side — how the command is built, how a path
is quoted, that ``~`` is left for the far side, and that the catalog, the
history and the content all come back.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from crossrepo import core, gitutil
from crossrepo.config import Config, SourceWarning
from crossrepo.location import Location, quote

from fixtures import CODE, commit, fake_ssh, init

HOST = "tester@fakehost"


def _made(path: Path) -> Path:
    """Create a directory and its parents, since `init` does not."""
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """A directory of repositories reachable only as ``tester@fakehost:~/...``."""
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("CROSSREPO_SSH", fake_ssh(tmp_path, home))
    monkeypatch.setenv("CROSSREPO_CACHE", str(tmp_path / "cache"))

    repo = init(home / "projects" / "sweep-scan")
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text(
        "files:\n"
        "  hits.csv: Sweep hits, one row per gene\n"
        "  /data/samples.csv: Kept beside the raw data\n"
    )
    (repo / "results" / "hits.csv").write_text("gene,score\nA,1\n")
    (repo / "results" / "private.csv").write_text("not,published\n")
    (repo / "data").mkdir()
    (repo / "data" / "samples.csv").write_text("s,1\n")
    commit(repo, "first results")
    (repo / "results" / "hits.csv").write_text("gene,score\nA,1\nB,2\n")
    commit(repo, "add gene B")
    return home


# ------------------------------------------------------- what a root means

def test_a_root_naming_a_host_is_remote():
    loc = Location.parse("kmt@genome.au.dk:~/some/folder")
    assert (loc.host, loc.path) == ("kmt@genome.au.dk", "~/some/folder")
    assert loc.is_remote and str(loc) == "kmt@genome.au.dk:~/some/folder"


def test_a_host_alias_needs_no_user():
    assert Location.parse("genome:~/x") == Location(path="~/x", host="genome")


def test_an_ordinary_path_is_not_remote():
    for spelled in ("~/projects", "/home/kmt/projects", "./odd:name", "projects"):
        assert not Location.parse(spelled).is_remote, spelled


def test_a_remote_path_with_nothing_after_the_colon_is_the_home_directory():
    assert Location.parse("genome:").path == "~"


def test_quoting_leaves_the_tilde_for_the_far_side():
    """Only the far side knows where its home is, so `~` must reach it unquoted."""
    assert quote("~/some folder/x") == "~/'some folder/x'"
    assert quote("~") == "~"
    assert quote("/plain/path") == "/plain/path"
    assert quote("/odd name/x") == "'/odd name/x'"


def test_a_remote_command_is_one_ssh_call(monkeypatch):
    monkeypatch.delenv("CROSSREPO_SSH", raising=False)
    argv = Location(path="~/x", host=HOST).command(["git", "-C", "~/x", "status"])
    assert argv[0] == "ssh"
    assert HOST in argv
    assert argv[-1] == "git -C ~/x status"          # one word for the far shell


def test_a_shell_snippet_runs_here_when_there_is_no_host():
    """The same call covers both sides, so a local location runs it locally."""
    argv = Location(path="/tmp").shell("printf ok")
    assert subprocess.run(argv, capture_output=True).stdout == b"ok"


# ------------------------------------------------------------- discovery

def test_repos_are_found_on_the_far_side(server):
    found = gitutil.discover_repos([f"{HOST}:~/projects"])
    assert [(f.host, f.name) for f in found] == [(HOST, "sweep-scan")]
    assert found[0].path.startswith(str(server))    # `~` expanded over there


def test_a_root_that_is_itself_a_repo_counts(server):
    found = gitutil.discover_repos([f"{HOST}:~/projects/sweep-scan"])
    assert [f.name for f in found] == ["sweep-scan"]


def test_a_repo_two_levels_down_is_not_found(server):
    found = gitutil.discover_repos([f"{HOST}:~"])
    assert found == []                              # projects/ is not a repo


def test_a_host_that_does_not_answer_warns_instead_of_stopping(server):
    """One unreachable server must not cost the roots that do answer."""
    with pytest.warns(UserWarning, match="Connection refused"):
        found = gitutil.discover_repos(
            ["unreachable@nowhere:~/x", f"{HOST}:~/projects"]
        )
    assert [f.name for f in found] == ["sweep-scan"]


# --------------------------------------------------------- the catalog

def test_a_remote_repo_is_cataloged(server):
    entries = core.build(Config(roots=[f"{HOST}:~/projects"]))
    assert sorted(e.path for e in entries) == ["data/samples.csv", "results/hits.csv"]
    entry = entries[0]
    assert entry.source == "ssh"
    assert entry.root.is_remote and entry.root.host == HOST
    assert entry.repo == "sweep-scan"
    assert "private.csv" not in {e.name for e in entries}


def test_content_is_read_over_ssh(server):
    cfg = Config(roots=[f"{HOST}:~/projects"])
    path = core.get("sweep-scan", "hits.csv", cfg=cfg, quiet=True)
    assert path.read_text() == "gene,score\nA,1\nB,2\n"


def test_history_is_read_over_ssh(server):
    cfg = Config(roots=[f"{HOST}:~/projects"])
    spec = core.Spec(repo="sweep-scan", path="hits.csv")
    entry = core.resolve_one(core.build(cfg), spec)
    got = core.versions(entry)
    assert [v.subject for v in got] == ["add gene B", "first results"]
    older = core.materialize(entry, got[-1])
    assert older.read_text() == "gene,score\nA,1\n"


def test_a_remote_root_survives_the_catalog_cache(server):
    cfg = Config(roots=[f"{HOST}:~/projects"])
    core.save(core.build(cfg), cfg)
    stored = core.load_cached(cfg=cfg)
    assert stored is not None
    assert stored[0].root == core.build(cfg)[0].root
    assert stored[0].root.is_remote


def test_a_clone_on_this_machine_wins_over_one_on_a_server(server, tmp_path):
    """Both can supply the file; the one that costs no round trip is kept."""
    here = tmp_path / "here" / "projects"          # same owner, from the parent
    repo = init(_made(here / "sweep-scan"))
    (repo / "results").mkdir(parents=True)
    (repo / "results" / "crossrepo.yml").write_text("files:\n  hits.csv: The same file\n")
    (repo / "results" / "hits.csv").write_text("gene,score\nA,1\nB,2\n")
    commit(repo, "the same results, checked out here")

    cfg = Config(roots=[f"{HOST}:~/projects", str(here)])
    entries = [e for e in core.build(cfg) if e.name == "hits.csv"]
    assert len(entries) == 1
    assert entries[0].source == "local"


def test_diagnose_reports_a_host_that_does_not_answer(server):
    lines = core.diagnose(Config(roots=["unreachable@nowhere:~/x"]))
    assert any("Connection refused" in line for line in lines)


def test_diagnose_counts_what_a_server_publishes(server):
    lines = core.diagnose(Config(roots=[f"{HOST}:~/projects"]))
    assert any("1 repos" in line and "1 with a results directory" in line
               for line in lines)


# ------------------------------------------------------------- Git LFS

def test_an_lfs_object_is_streamed_from_the_far_side(server):
    repo = server / "projects" / "big-thing"
    init(repo)
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text("files:\n  big.h5: Held in LFS\n")
    oid = "ab" * 32
    (repo / "results" / "big.h5").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\nsize 7\n"
    )
    commit(repo, "a file held in lfs")
    obj = repo / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    obj.parent.mkdir(parents=True)
    obj.write_text("real!!\n")

    cfg = Config(roots=[f"{HOST}:~/projects"])
    path = core.get("big-thing", "big.h5", cfg=cfg, quiet=True)
    assert path.read_text() == "real!!\n"


def test_a_missing_lfs_object_names_the_machine_it_is_on(server):
    repo = server / "projects" / "no-object"
    init(repo)
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text("files:\n  gone.h5: Held in LFS\n")
    (repo / "results" / "gone.h5").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "cd" * 32 + "\nsize 512\n"
    )
    commit(repo, "a pointer with nothing behind it")

    cfg = Config(roots=[f"{HOST}:~/projects"])
    with pytest.raises(FileNotFoundError, match=f"ssh {HOST} git -C .* lfs fetch"):
        core.get("no-object", "gone.h5", cfg=cfg, quiet=True)


def test_a_dataset_is_assembled_from_the_far_side(server):
    """A directory published as one table must come back whole, over ssh."""
    repo = server / "projects" / "sweep-scan"
    parts = repo / "results" / "table.parquet"
    parts.mkdir()
    for i in range(3):
        (parts / f"part-{i}.parquet").write_text(f"row,{i}\n")
    (repo / "results" / "crossrepo.yml").write_text(
        "files:\n"
        "  hits.csv: Sweep hits, one row per gene\n"
        "  table.parquet: One table split over files\n"
    )
    commit(repo, "publish a dataset too")

    cfg = Config(roots=[f"{HOST}:~/projects"])
    got = core.get("sweep-scan", "table.parquet", cfg=cfg, quiet=True)
    assert got.is_dir()
    assert sorted(p.name for p in got.iterdir()) == [
        "part-0.parquet", "part-1.parquet", "part-2.parquet",
    ]
    assert (got / "part-1.parquet").read_text() == "row,1\n"


# ------------------------------------------------ the shared connection

def test_the_control_socket_fits_in_a_unix_socket_path(monkeypatch):
    """A socket path over the limit is not a slow connection but a failed one."""
    from crossrepo import location

    long_tmp = "/var/folders/" + "x" * 40 + "/T"          # as macOS gives one
    monkeypatch.setattr(location, "_configured", lambda host: {})
    monkeypatch.setattr(location.tempfile, "gettempdir", lambda: long_tmp)
    path = _control_path(location.ssh_argv("kmt@login.genome.au.dk"))
    assert path is not None
    assert len(path.encode()) <= location.SOCKET_LIMIT
    assert not path.startswith(long_tmp)                  # passed over, too long


def test_a_socket_path_that_cannot_be_had_costs_only_the_sharing(monkeypatch):
    from crossrepo import location

    monkeypatch.setattr(location.tempfile, "gettempdir", lambda: "/" + "x" * 200)
    monkeypatch.setattr(location, "_private_dir", lambda path: False)
    argv = location.ssh_argv("kmt@host")
    assert _control_path(argv) is None
    assert argv[-1] == "kmt@host"                         # still a usable call


def test_each_host_gets_its_own_socket(monkeypatch):
    from crossrepo import location
    from crossrepo.location import ssh_argv

    monkeypatch.setattr(location, "_configured", lambda host: {})

    one, two = _control_path(ssh_argv("a@host")), _control_path(ssh_argv("b@host"))
    assert one and two and one != two
    assert one == _control_path(ssh_argv("a@host"))       # and the same one twice


def _control_path(argv):
    """The socket ssh was told to share, or None when it was not told to."""
    for arg in argv:
        if arg.startswith("ControlPath="):
            return arg.split("=", 1)[1]
    return None


# ------------------------------------------------ reporting from the cli

def test_refresh_names_a_server_that_did_not_answer(server, tmp_path, capsys):
    from crossrepo import cli

    conf = tmp_path / "c.toml"
    conf.write_text(
        f'roots = ["unreachable@nowhere:~/x", "{HOST}:~/projects"]\n'
    )
    assert cli.main(["--config", str(conf), "refresh"]) == 0
    out, err = capsys.readouterr()
    assert "cataloged 2 files in 1 repos" in out          # the server that answered
    assert "1 configured source could not be read:" in err
    assert "unreachable@nowhere:~/x: ssh: connect to host" in err


# ------------------------------------------- what ssh is allowed to ask

def test_ssh_may_ask_when_a_terminal_can_answer(monkeypatch):
    """A key passphrase or a two-factor code is typed, so the prompt must reach
    the terminal rather than being turned into a refusal."""
    from crossrepo import location

    monkeypatch.setattr(location, "_configured", lambda host: {})
    monkeypatch.setattr(location, "_interactive", lambda: True)
    assert "BatchMode=yes" not in location.ssh_options("kmt@genome.au.dk")


def test_ssh_is_told_not_to_ask_when_nothing_could_answer(monkeypatch):
    from crossrepo import location

    monkeypatch.setattr(location, "_configured", lambda host: {})
    monkeypatch.setattr(location, "_interactive", lambda: False)
    assert "BatchMode=yes" in location.ssh_options("kmt@genome.au.dk")


def test_what_the_users_own_ssh_config_settles_is_left_alone(monkeypatch):
    """Command line options beat the config file, so anything set there stands."""
    from crossrepo import location

    monkeypatch.setattr(location, "_configured", lambda host: {
        "controlpath": "~/.ssh/cm-%r@%h:%p",
        "connecttimeout": "30",
    })
    options = location.ssh_options("kmt@genome.au.dk")
    assert not [o for o in options if o.startswith("Control")]
    assert not [o for o in options if o.startswith("ConnectTimeout")]


def test_our_own_multiplexing_is_added_when_there_is_none(monkeypatch):
    from crossrepo import location

    monkeypatch.setattr(location, "_configured", lambda host: {
        "controlmaster": "false", "connecttimeout": "none",
    })
    options = location.ssh_options("kmt@genome.au.dk")
    assert "ControlMaster=auto" in options
    assert "ConnectTimeout=10" in options


def test_a_host_answering_is_how_the_connection_is_opened(server):
    from crossrepo.location import warm

    assert warm(HOST) is None
    assert "Connection refused" in warm("unreachable@nowhere")


def test_a_refusal_says_where_to_answer_the_prompt(server, monkeypatch):
    """Without a terminal, a host that wanted a code must say what to do."""
    from crossrepo import location

    monkeypatch.setattr(location, "_interactive", lambda: False)
    said = location.warm("kmt@twofactor.example")
    assert "Permission denied" in said
    assert "run `ssh kmt@twofactor.example true`" in said
    assert "no terminal here" in said


def test_a_refusal_on_a_terminal_is_left_to_speak_for_itself(server, monkeypatch):
    from crossrepo import location

    monkeypatch.setattr(location, "_interactive", lambda: True)
    said = location.warm("kmt@twofactor.example")
    assert "Permission denied" in said
    assert "no terminal here" not in said


# ------------------------------------ asking where there is no terminal

@pytest.fixture()
def notebook(monkeypatch):
    """Stand in for a kernel: no terminal, but able to put a question."""
    from crossrepo import location

    asked = []

    def ask(prompt):
        asked.append(prompt)
        return CODE

    monkeypatch.setattr(location, "_interactive", lambda: False)
    monkeypatch.setattr(location, "_kernel", lambda: object())
    monkeypatch.setattr(location, "_ask", ask)
    return asked


def test_a_notebook_is_asked_for_the_code(server, notebook):
    """ssh has no terminal either, so it asks crossrepo, which asks the notebook."""
    from crossrepo.location import warm

    assert warm("kmt@askpass.example") is None
    assert notebook == ["Verification code: "]


def test_the_answer_is_what_lets_the_connection_through(server, monkeypatch):
    from crossrepo import location

    monkeypatch.setattr(location, "_interactive", lambda: False)
    monkeypatch.setattr(location, "_kernel", lambda: object())
    monkeypatch.setattr(location, "_ask", lambda prompt: "000000")
    assert "Permission denied" in location.warm("kmt@askpass.example")


def test_a_notebook_is_not_told_to_stay_quiet(notebook):
    """BatchMode would silence the prompt this whole arrangement exists for."""
    from crossrepo import location

    assert "BatchMode=yes" not in location.ssh_options("kmt@askpass.example")


def test_the_catalog_is_read_through_the_prompt(server, notebook):
    cfg = Config(roots=["kmt@askpass.example:~/projects"])
    entries = core.build(cfg)
    assert sorted(e.path for e in entries) == ["data/samples.csv", "results/hits.csv"]
    assert notebook                                  # it was asked at least once


def test_content_comes_back_through_the_prompt(server, notebook):
    """The path that streams a file is the one that must not deadlock."""
    cfg = Config(roots=["kmt@askpass.example:~/projects"])
    path = core.get("sweep-scan", "hits.csv", cfg=cfg, quiet=True)
    assert path.read_text() == "gene,score\nA,1\nB,2\n"


def test_a_repo_with_many_files_does_not_wedge_on_its_own_input(server, notebook):
    """Sizing blobs writes more to ssh than a pipe would hold in one go."""
    repo = server / "projects" / "wide"
    init(repo)
    (repo / "results").mkdir()
    (repo / "results" / "crossrepo.yml").write_text('files:\n  "*.csv": one of many\n')
    for i in range(400):
        (repo / "results" / f"table-{i:03}.csv").write_text(f"n\n{i}\n")
    commit(repo, "many small tables")

    cfg = Config(roots=["kmt@askpass.example:~/projects"])
    entries = [e for e in core.build(cfg) if e.repo == "wide"]
    assert len(entries) == 400
    assert all(e.latest.size > 0 for e in entries)


def test_the_helper_ssh_calls_is_private_and_runs_this_python():
    from crossrepo.location import _helper

    helper = Path(_helper())
    assert helper.stat().st_mode & 0o777 == 0o700
    assert helper.read_text().splitlines()[0] == f"#!{sys.executable}"


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs openssh")
def test_real_openssh_asks_us_and_takes_the_answer(tmp_path, monkeypatch):
    """The stand-in for ssh proves our side; this proves ssh's.

    ssh-keygen wants a passphrase exactly as ssh wants a code, and reads it the
    same way, so it stands in for a host asking — without an account, a network,
    or a failed login attempt anywhere.
    """
    from crossrepo import location

    key = tmp_path / "k"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "s3cret-pass", "-f", str(key), "-q"],
        check=True,
    )
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return "s3cret-pass"

    monkeypatch.setattr(location, "_kernel", lambda: object())
    monkeypatch.setattr(location, "_ask", ask)
    proc = location.execute(["ssh-keygen", "-y", "-f", str(key)], remote=True)

    assert proc.returncode == 0                     # the answer was accepted
    assert asked and "passphrase" in asked[0]       # and ssh asked us for it
    assert proc.stdout.startswith(b"ssh-ed25519 ")


# ------------------------------------------- a root written relative to home

def test_a_root_relative_to_the_home_directory_is_read(tmp_path, monkeypatch):
    """`host:some/dir` is where ssh puts you plus that path, as scp reads it."""
    home = tmp_path / "home"
    repo = home / "xy-drive" / "people" / "kmt" / "hic-xy-sperm"
    (repo / "results").mkdir(parents=True)
    init(repo)
    (repo / "results" / "crossrepo.yml").write_text(
        "files:\n  all_genes.h5: all genes\n  segments_50000.csv: 50kb segments\n"
    )
    (repo / "results" / "all_genes.h5").write_text("h\n")
    (repo / "results" / "segments_50000.csv").write_text("s\n")
    commit(repo, "publish")
    monkeypatch.setenv("CROSSREPO_SSH", fake_ssh(tmp_path, home))
    monkeypatch.setenv("CROSSREPO_CACHE", str(tmp_path / "cache"))

    cfg = Config(roots=[f"{HOST}:xy-drive/people/kmt"])
    assert [str(f) for f in gitutil.discover_repos(cfg.roots)] == [
        f"{HOST}:xy-drive/people/kmt/hic-xy-sperm"
    ]
    assert sorted(e.path for e in core.build(cfg)) == [
        "results/all_genes.h5", "results/segments_50000.csv",
    ]


# --------------------------------------------------- git on the far side

def test_a_host_without_git_says_so(server):
    """Finding repos takes a shell; reading one takes git, and a login shell
    having git is not the same as an ssh command having it."""
    from crossrepo.location import warm

    said = warm("kmt@nogit.example")
    assert said is not None
    assert "git is not on the PATH" in said
    assert "ssh kmt@nogit.example git --version" in said


def test_a_host_without_git_publishes_nothing_and_reports_it(server, tmp_path):
    from crossrepo import cli

    conf = tmp_path / "c.toml"
    conf.write_text(f'roots = ["kmt@nogit.example:~/projects"]\n')
    with pytest.warns(SourceWarning, match="git is not on the PATH"):
        assert core.build(Config(roots=["kmt@nogit.example:~/projects"])) == []


def test_a_host_with_git_is_not_complained_about(server, recwarn):
    assert core.build(Config(roots=[f"{HOST}:~/projects"]))
    assert not [w for w in recwarn if issubclass(w.category, SourceWarning)]
