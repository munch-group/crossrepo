"""
Where a repository is, and how to run git there.

A root is usually a directory on this machine. It can also be a directory on
another machine, written the way ssh and scp write one:

```toml
roots = ["~/projects", "kmt@genome.au.dk:~/some/folder"]
```

Nothing is cloned or mounted for the second form. Git runs on the far side and
only its output crosses the network, so a server holding a hundred repositories
costs no local disk and no waiting for a checkout.

Access is whatever ssh already grants, and crossrepo keeps no credentials of its
own. Where a host asks for something -- a key passphrase, or the second factor a
cluster login usually wants -- ssh asks for it at the terminal, and the answer
goes to ssh, never through here.

A notebook has no terminal, but it has somewhere to ask: `getpass` there opens a
prompt at the top of the window. So in a notebook ssh is given an
``SSH_ASKPASS`` program of our own, which asks the kernel that started it, and
the answer goes back to ssh the way it would from a terminal. Where there is
neither -- a scheduled job -- ssh is told not to ask, and the host is reported
along with the command to run somewhere it can be answered.

Cataloguing one repository takes a handful of git commands, and a fresh ssh
handshake for each would cost more than the commands do -- and, on a host that
asks for a code, a fresh prompt each time. So calls to one host share a single
connection, opened before the scan starts and kept open for a while after the
last of them. Anything the user's own ssh configuration settles, from the
connection timeout to their own shared connection, is left alone: a
``ControlPersist`` of a few hours set there is honoured, and is the way to make
one answered prompt last a working session.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import re
import selectors
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple, Union

SPEC = re.compile(r"^(?P<host>[^/:]+):(?P<path>.*)$")
"""
Pattern recognising a root on another machine.

The host part holds no slash, so an ordinary path is never mistaken for one, and
a local directory whose name really does contain a colon can be written
``./odd:name`` to say so.
"""

SSH_ENV = "CROSSREPO_SSH"
"""
Environment variable naming the ssh program, for a setup ssh options cannot
express. Split like a shell command line, so ``ssh -F ~/.ssh/other_config``
works. Defaults to ``ssh``.
"""

CONNECT_TIMEOUT = "10"
"""
Seconds to wait for a host to answer at all, when the user has set no timeout of
their own. It bounds a scan against a server that is down, and is long enough
not to matter for one that is up.
"""

CONTROL_PERSIST = "3600"
"""
Seconds a shared connection is kept open after the last command using it.

Long enough that one answered prompt covers a whole session's work: a key
passphrase or a two-factor code is asked for when the connection is opened, and
a scan followed by a few reads would otherwise ask again each time the
connection had lapsed.

An hour rather than the five minutes this was, because a session is not five
minutes long. Cataloguing and then reading a result is one piece of work with a
person thinking in between, and a connection that lapses in that gap asks again
from inside `crossrepo.core.get` -- where, unlike a scan, nothing has been
printed to say a server is being reached at all. A user whose own ssh
configuration shares connections for the host keeps their own setting: this is
only for hosts that have none.
"""

SOCKET_LIMIT = 104 - 16
"""
Longest a control socket path may be.

A unix socket path is capped at 104 bytes on macOS and 108 on Linux, and ssh
builds the master socket under a temporary name a dozen characters longer than
the one it is given, so the room left for the path itself is smaller again. The
smaller limit is used everywhere: a socket that fits on macOS fits anywhere, and
going over it is not a slow connection but a failed one, with every call to that
host reported as unreachable.
"""


def _control_options(host: str) -> List[str]:
    """
    Options that make calls to one host share a single connection.

    The socket is named for the host rather than with ssh's own ``%C``, whose
    forty characters do not fit under `SOCKET_LIMIT` beneath the long temporary
    directory macOS gives each user. Nothing is added when the user's own ssh
    configuration already shares connections for this host, which
    [](`crossrepo.location.ssh_options`) checks first.

    Parameters
    ----------
    host :
        Destination the socket is for.

    Returns
    -------
    :
        The options, or an empty list when no short enough socket path can be
        had, in which case each call opens its own connection and everything
        still works, more slowly.
    """
    tag = hashlib.sha1(host.encode("utf-8")).hexdigest()[:8]
    directory = _private_base(room=len(tag) + 1)
    if directory is None:
        return []
    return [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={directory / tag}",
        "-o", f"ControlPersist={CONTROL_PERSIST}",
    ]


def _private_base(room: int = 0) -> Optional[Path]:
    """
    The directory this user's ssh sockets live in.

    Parameters
    ----------
    room :
        Length of the name that will be put inside it, so that a directory
        leaving no room under `SOCKET_LIMIT` is passed over rather than used to
        build a socket ssh will refuse.

    Returns
    -------
    :
        The directory, or `None` when there is nowhere both private and short
        enough.
    """
    uid = getattr(os, "getuid", lambda: 0)()
    tried: List[Path] = []
    for base in (Path(tempfile.gettempdir()), Path("/tmp")):
        directory = base / f"crossrepo-ssh-{uid}"
        if directory in tried:
            continue
        tried.append(directory)
        if room and len(str(directory).encode("utf-8")) + room > SOCKET_LIMIT:
            continue
        if _private_dir(directory):
            return directory
    return None


def _private_dir(path: Path) -> bool:
    """
    Make a directory only this user can write to, or say that there is none.

    A control socket is a way into an authenticated connection, so it must not
    sit in a directory someone else can write to, which is a real question for
    the fallback under ``/tmp``.

    Parameters
    ----------
    path :
        Directory to make or check.

    Returns
    -------
    :
        `True` when the directory exists, belongs to this user, and is closed to
        everyone else.
    """
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        st = path.stat()
    except OSError:
        return False
    uid = getattr(os, "getuid", lambda: st.st_uid)()
    return st.st_uid == uid and not st.st_mode & 0o077


def _program() -> List[str]:
    """
    The ssh command to run, as a list of arguments.

    Returns
    -------
    :
        What `SSH_ENV` names, split like a shell command line, or ``ssh``.
    """
    return shlex.split(os.environ.get(SSH_ENV) or "ssh")


def _interactive() -> bool:
    """
    Whether there is someone at a terminal to answer ssh.

    A key with a passphrase, and a host asking for a second factor, are both
    answered by typing something. That can only happen where a terminal is
    attached; anywhere else — a notebook, a scheduled job — the same prompt is a
    scan that hangs or fails obscurely, so ssh is told not to ask.

    Returns
    -------
    :
        `True` when standard input is a terminal.
    """
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _configured(host: str) -> Dict[str, str]:
    """
    What the user's own ssh configuration already settles for a host.

    ``ssh -G`` answers this without connecting to anything, which is worth one
    local process per host: options given on the command line beat the
    configuration file, so anything set there has to be left alone rather than
    quietly replaced with what crossrepo would have chosen.

    Parameters
    ----------
    host :
        Destination to ask about.

    Returns
    -------
    :
        The settings in force, lowercased keys to values, empty when ssh could
        not be asked.
    """
    key = (os.environ.get(SSH_ENV) or "", host)
    if key not in _CONFIGURED:
        try:
            proc = subprocess.run(
                [*_program(), "-G", host], capture_output=True, check=False,
            )
        except OSError:
            _CONFIGURED[key] = {}
            return _CONFIGURED[key]
        found: Dict[str, str] = {}
        if proc.returncode == 0:
            for line in proc.stdout.decode("utf-8", "replace").splitlines():
                name, _, value = line.partition(" ")
                found.setdefault(name.strip().lower(), value.strip())
        _CONFIGURED[key] = found
    return _CONFIGURED[key]


_CONFIGURED: Dict[Tuple[str, str], Dict[str, str]] = {}
"""What ``ssh -G`` said about each host, asked once per process."""


def _unset(value: Optional[str]) -> bool:
    """
    Whether an ssh setting is at its default of not being set.

    Parameters
    ----------
    value :
        Value ``ssh -G`` reported, or `None` when it reported nothing.

    Returns
    -------
    :
        `True` when the setting is absent or off.
    """
    return value is None or value.strip().lower() in ("", "none", "no", "false")


def ssh_options(host: str) -> List[str]:
    """
    Options for one host, adding nothing the user has already decided.

    Parameters
    ----------
    host :
        Destination the options are for.

    Returns
    -------
    :
        Options to put before the destination on the command line.
    """
    conf = _configured(host)
    options: List[str] = []
    if not _interactive() and not _bridged():
        # Nothing here could answer a prompt, so a host that would ask is a
        # failure rather than a wait.
        options += ["-o", "BatchMode=yes"]
    if _unset(conf.get("connecttimeout")):
        options += ["-o", f"ConnectTimeout={CONNECT_TIMEOUT}"]
    if _unset(conf.get("controlpath")):
        options += _control_options(host)
    return options


def ssh_argv(host: str) -> List[str]:
    """
    Build the ssh command that runs something on one host.

    Parameters
    ----------
    host :
        Destination as ssh understands it: ``host``, ``user@host``, or a name
        from the user's ssh configuration.

    Returns
    -------
    :
        The command up to but not including what is to be run there.
    """
    return [*_program(), *ssh_options(host), host]


def warm(host: str) -> Optional[str]:
    """
    Open the connection to a host, and see that it can do what is needed of it.

    The first command to a host is the one that authenticates, and a key
    passphrase or a two-factor code has to be typed. Asking for it here means
    one prompt, before a scan has printed anything, and one shared connection
    for every command that follows; letting the scan ask means the prompt lands
    in the middle of a progress bar, and again whenever the connection lapses.

    The command asked for is ``git --version``, because everything after this
    point is git: finding the repositories takes only a shell, but reading one
    takes git, and a login shell that has git while a command over ssh does not
    is a common way for a server to publish nothing without saying why.

    Parameters
    ----------
    host :
        Destination to reach.

    Returns
    -------
    :
        `None` when the host answered and has git, else what was wrong. A prompt
        is written to the terminal by ssh itself, so it is seen and can be
        answered even though the output is being read here; in a notebook, where
        there is no terminal, it is put to the person reading instead.
    """
    proc = execute([*ssh_argv(host), "git --version"], remote=True)
    if proc.returncode == 0:
        return None
    said = proc.stderr.decode("utf-8", "replace").strip()
    said = said or f"ssh exited {proc.returncode}"
    if proc.returncode == 127 or "not found" in said.lower():
        return (
            f"git is not on the PATH of an ssh command there ({said})\n"
            f"crossrepo runs git on the server, so `ssh {host} git --version` has "
            f"to print a version -- a login shell that has git is not enough, "
            f"since ~/.bashrc usually stops before setting the PATH when it is "
            f"not interactive"
        )
    if said.startswith(f"{host}: "):
        said = said[len(host) + 2:]           # the caller already names the host
    if not _interactive() and not _bridged() and "denied" in said.lower():
        said += (
            f"\nno terminal here to answer a key passphrase or a two-factor "
            f"code: run `ssh {host} true` where you can answer it, then try "
            f"again within {int(CONTROL_PERSIST) // 60} minutes"
        )
    return said


def quote(arg: str) -> str:
    """
    Quote one argument for the shell that ssh starts on the far side.

    A leading ``~`` is left for that shell to expand, since a remote path is
    written the way the user would write it there and only the far side knows
    where home is. Everything after it is quoted, so a space or a glob character
    in a directory name is still just part of the name.

    Parameters
    ----------
    arg :
        Argument to quote.

    Returns
    -------
    :
        The argument as one shell word.

    Examples
    --------

    ```python
    quote("~/some folder/x")
    # "~/'some folder/x'"
    ```
    """
    if arg.startswith("~"):
        head, sep, rest = arg.partition("/")
        if not sep:
            return head if head == "~" or head[1:].isalnum() else shlex.quote(arg)
        return f"{head}/{shlex.quote(rest)}"
    return shlex.quote(arg)


@dataclass(frozen=True)
class Location:
    """
    A directory, on this machine or on another one.

    Attributes
    ----------
    path :
        The directory. Absolute, or relative to where the far side puts you,
        and ``~`` is expanded by whichever machine the directory is on.
    host :
        Destination for ssh, empty for a directory on this machine.

    Examples
    --------

    ```python
    Location.parse("kmt@genome.au.dk:~/projects")
    # Location(path='~/projects', host='kmt@genome.au.dk')
    ```

    See Also
    --------
    [](`crossrepo.gitutil.discover_repos`)
    """

    path: str
    host: str = ""

    @classmethod
    def parse(cls, text: Union[str, Path]) -> "Location":
        """
        Read a root or a repository as it is written in the settings.

        Parameters
        ----------
        text :
            ``user@host:path`` for a directory on another machine, anything else
            for one on this machine.

        Returns
        -------
        :
            The location. A remote path that is empty stands for the home
            directory there, as it does for ssh itself.
        """
        s = str(text)
        m = SPEC.match(s)
        if m:
            return cls(path=m.group("path") or "~", host=m.group("host"))
        return cls(path=s)

    @classmethod
    def of(cls, value: Union["Location", str, Path]) -> "Location":
        """
        Accept a location however it is spelled.

        Parameters
        ----------
        value :
            A location, or a path or spec to parse into one.

        Returns
        -------
        :
            The location itself, or the parsed one.
        """
        return value if isinstance(value, cls) else cls.parse(value)

    @property
    def is_remote(self) -> bool:
        """
        Whether reaching this directory means going over ssh.

        Returns
        -------
        :
            `True` when the location names a host.
        """
        return bool(self.host)

    @property
    def name(self) -> str:
        """
        Last component of the path, which for a working tree is its directory
        name.

        Returns
        -------
        :
            The name, empty for an empty path.
        """
        return PurePosixPath(self.path).name

    @property
    def parent(self) -> "Location":
        """
        The directory holding this one.

        Returns
        -------
        :
            The parent, on the same host.
        """
        return Location(path=str(PurePosixPath(self.path).parent), host=self.host)

    def __truediv__(self, other: str) -> "Location":
        return Location(path=str(PurePosixPath(self.path) / other), host=self.host)

    def __str__(self) -> str:
        return f"{self.host}:{self.path}" if self.host else self.path

    def local_path(self) -> Optional[Path]:
        """
        The directory as a path on this machine.

        Returns
        -------
        :
            The path with ``~`` expanded, or `None` when the directory is on
            another machine and there is no such thing.
        """
        return None if self.is_remote else Path(self.path).expanduser()

    def resolved(self) -> "Location":
        """
        The same directory, spelled the way it will be reached.

        Returns
        -------
        :
            A local location with ``~`` expanded and made absolute, and a remote
            one unchanged, since only the far side knows where its home is.
        """
        if self.is_remote:
            return self
        return Location(path=str(Path(self.path).expanduser()))

    def command(self, argv: Sequence[str]) -> List[str]:
        """
        Wrap a command so that running it here runs it in the right place.

        Parameters
        ----------
        argv :
            The command, as a list of arguments.

        Returns
        -------
        :
            The command to run on this machine: `argv` itself for a local
            directory, and an ssh call carrying it for a remote one.
        """
        if not self.is_remote:
            return list(argv)
        return [*ssh_argv(self.host), " ".join(quote(a) for a in argv)]

    def shell(self, script: str) -> List[str]:
        """
        Wrap a shell snippet so that it runs where this directory is.

        Used for the one thing that is not a git command: looking to see which
        subdirectories are working trees, which is one round trip this way and
        one per directory any other way.

        Parameters
        ----------
        script :
            Shell script to run. Any path in it must already be quoted with
            [](`crossrepo.location.quote`).

        Returns
        -------
        :
            The command to run on this machine.
        """
        if not self.is_remote:
            return ["sh", "-c", script]
        return [*ssh_argv(self.host), script]


ASKPASS_SOCKET = "CROSSREPO_ASKPASS"
"""Environment variable telling the helper where to ask."""

HELPER = '''#!{python}
"""Ask crossrepo for what ssh wants, and hand the answer back to ssh.

ssh runs this with the prompt as its one argument and reads the answer from its
output. It cannot ask the person itself -- it has no terminal either -- so it
asks the crossrepo that started ssh, which is running in the notebook where the
question can be put.
"""
import os
import socket
import sys

client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.connect(os.environ["{variable}"])
client.sendall((sys.argv[1] if len(sys.argv) > 1 else "").encode("utf-8"))
client.shutdown(socket.SHUT_WR)
answer = b""
while True:
    part = client.recv(4096)
    if not part:
        break
    answer += part
sys.stdout.write(answer.decode("utf-8"))
'''
"""
The program ssh calls when it needs something typed.

It is written out rather than shipped as a file so that it runs under the same
python as the notebook, whatever environment that is, and so that it lives
beside the socket it talks to, in a directory only this user can read.
"""


def _kernel():
    """
    The notebook able to put a question to the person reading it, if any.

    Returns
    -------
    :
        The interactive shell when running under a Jupyter kernel -- which is
        what VS Code, JupyterLab and the classic notebook all are -- else
        `None`. A kernel has no terminal, but it can ask: `getpass` there opens
        the prompt at the top of the window.
    """
    module = sys.modules.get("IPython")
    if module is None:
        return None                     # not imported, so certainly not a kernel
    try:
        shell = module.get_ipython()
    except Exception:                   # pragma: no cover - defensive
        return None
    return shell if shell is not None and hasattr(shell, "kernel") else None


def _bridged() -> bool:
    """
    Whether ssh's prompts should be put to a notebook.

    Returns
    -------
    :
        `True` when a kernel is running. The person is at the notebook, so that
        is where the question goes, whatever the terminal the kernel was started
        from may be doing.
    """
    return _kernel() is not None


def _ask(prompt: str) -> str:
    """
    Put ssh's question to the person at the notebook.

    The question is written to standard error as well as being put, because
    `getpass` in a notebook opens its prompt at the top of the window, far from
    the cell that is waiting and easy to miss entirely. Standard error lands in
    that cell's own output, where the person is already looking. A prompt nobody
    notices cannot be told apart from a call that has hung, and waiting for an
    answer that is never going to come is exactly what hanging looks like.

    Parameters
    ----------
    prompt :
        What ssh asked, such as ``Verification code:``.

    Returns
    -------
    :
        What they typed. `getpass` is used rather than `input`, so the answer is
        masked and is not kept in the notebook's history.
    """
    import getpass

    print(
        f"crossrepo is waiting for an answer to an ssh prompt "
        f"({prompt.strip()}). It opens at the top of the window; nothing is "
        f"read from the server until it is answered.",
        file=sys.stderr, flush=True,
    )
    return getpass.getpass(prompt)


def _helper() -> Optional[str]:
    """
    Write the program ssh calls to ask a question, once for this process.

    Returns
    -------
    :
        Path of the program, or `None` when there is nowhere private to put it.
    """
    global _HELPER_PATH
    if _HELPER_PATH is None:
        directory = _private_base()
        if directory is None:
            return None
        path = directory / f"askpass-{os.getpid()}.py"
        path.write_text(
            HELPER.format(python=sys.executable, variable=ASKPASS_SOCKET),
            encoding="utf-8",
        )
        path.chmod(0o700)
        atexit.register(path.unlink, True)
        _HELPER_PATH = str(path)
    return _HELPER_PATH


_HELPER_PATH: Optional[str] = None
"""Where this process wrote the askpass program, once it has."""


def _serve(argv: Sequence[str], stdin: Optional[bytes], stdout):
    """
    Run a command, answering whatever ssh asks for while it runs.

The question has to be answered from this thread: a kernel takes input over
    the same connection it takes code over, and that is not something to use
    from anywhere else. So the command's output goes to temporary files rather
    than pipes -- a pipe would have to be drained here, and this is waiting for
    ssh to ask something.

    Parameters
    ----------
    argv :
        The command, already wrapped in its ssh call.
    stdin :
        Bytes to write to the command's standard input.
    stdout :
        Open file for the command's output, or `None` to collect it.

    Returns
    -------
    :
        The finished process, as [](`subprocess.run`) would report it.
    """
    helper = _helper()
    directory = _private_base()
    if helper is None or directory is None:            # pragma: no cover
        return subprocess.run(
            argv, input=stdin, stdout=stdout or subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
    address = str(directory / f"ask-{os.getpid()}")
    Path(address).unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    environment = {
        **os.environ,
        "SSH_ASKPASS": helper,
        "SSH_ASKPASS_REQUIRE": "force",
        ASKPASS_SOCKET: address,
        # Older ssh only reaches for an askpass program when it thinks a display
        # is there; newer ssh goes by SSH_ASKPASS_REQUIRE alone.
        "DISPLAY": os.environ.get("DISPLAY") or ":0",
    }
    try:
        server.bind(address)
        server.listen(1)
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            with _fed(stdin) as feed:
                proc = subprocess.Popen(
                    argv, stdin=feed, stdout=stdout or out, stderr=err,
                    env=environment,
                )
                _answer_until_done(proc, server)
            proc.wait()
            out.seek(0)
            err.seek(0)
            return subprocess.CompletedProcess(
                argv, proc.returncode,
                b"" if stdout is not None else out.read(), err.read(),
            )
    finally:
        server.close()
        Path(address).unlink(missing_ok=True)


def _answer_until_done(proc: "subprocess.Popen", server: socket.socket) -> None:
    """
    Answer what ssh asks, until the command it was asking for is finished.

    Waiting is on both at once rather than by polling either: a scan makes a
    command of every question it has for a repository, and a fifth of a second
    spent noticing that each one has ended would be the slowest thing about it.
    The waiting for the command to end is what the thread does; the asking stays
    here, because a kernel takes input over the same connection it takes code
    over, which is not something to use from two places.

    Parameters
    ----------
    proc :
        The running command.
    server :
        Listening socket the askpass program connects back to.
    """
    finished, ended = socket.socketpair()
    watch = threading.Thread(target=_signal_end, args=(proc, finished), daemon=True)
    watch.start()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(server, selectors.EVENT_READ)
            selector.register(ended, selectors.EVENT_READ)
            while True:
                for key, _mask in selector.select():
                    if key.fileobj is not server:
                        return
                    conn, _who = server.accept()
                    with conn:
                        asked = conn.recv(65536).decode("utf-8", "replace")
                        try:
                            answer = _ask(asked or "Password: ")
                        except (EOFError, KeyboardInterrupt):
                            answer = ""     # unanswered: let ssh refuse itself
                        conn.sendall(answer.encode("utf-8") + b"\n")
    finally:
        watch.join(timeout=1)
        finished.close()
        ended.close()


def _signal_end(proc: "subprocess.Popen", sentinel: socket.socket) -> None:
    """
    Wait for a command to finish and say so, from a thread of its own.

    Parameters
    ----------
    proc :
        The running command.
    sentinel :
        Socket to write a byte to once it has ended.
    """
    try:
        proc.wait()
    finally:
        try:
            sentinel.send(b".")
        except OSError:                     # pragma: no cover - already closed
            pass


@contextmanager
def _fed(stdin: Optional[bytes]):
    """
    Give a command its standard input as a file rather than a pipe.

    A pipe would have to be written while the command is read, and the thread
    that would do the writing is the one waiting on ssh's questions. A file has
    no such trouble, whatever its size.

    Parameters
    ----------
    stdin :
        Bytes the command should read, or `None` for nothing.

    Yields
    ------
    :
        What to give `subprocess.Popen` as its ``stdin``.
    """
    if stdin is None:
        yield subprocess.DEVNULL
        return
    with tempfile.TemporaryFile() as fh:
        fh.write(stdin)
        fh.seek(0)
        yield fh


def execute(
    argv: Sequence[str], *, remote: bool = False, stdin: Optional[bytes] = None,
    stdout=None,
) -> "subprocess.CompletedProcess":
    """
    Run a command, answering ssh where it would otherwise have nobody to ask.

    Parameters
    ----------
    argv :
        The command, already wrapped in its ssh call where it needs one.
    remote :
        Whether the command goes over ssh, and so may ask for something.
    stdin :
        Bytes to write to the command's standard input.
    stdout :
        Open file to write the command's standard output to, streamed rather
        than buffered. `None` captures it.

    Returns
    -------
    :
        The finished process. Its status is not checked here, since what a
        non-zero one means differs at each call site.
    """
    if remote and _bridged():
        return _serve(argv, stdin, stdout)
    return subprocess.run(
        argv,
        input=stdin,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def run(
    location: Location, argv: Sequence[str], *, stdin: Optional[bytes] = None,
    stdout=None,
) -> "subprocess.CompletedProcess":
    """
    Run a command where a location is.

    Parameters
    ----------
    location :
        Where to run it.
    argv :
        The command, as a list of arguments.
    stdin :
        Bytes to write to the command's standard input.
    stdout :
        Open file to write the command's standard output to, streamed rather
        than buffered. `None` captures it.

    Returns
    -------
    :
        The finished process. Its status is not checked here, since what a
        non-zero one means differs at each call site.
    """
    return execute(
        location.command(argv), remote=location.is_remote, stdin=stdin,
        stdout=stdout,
    )
