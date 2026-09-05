# crossrepo

Catalog and fetch versioned result files across many git repositories, without
submodules.

Repositories are read wherever they are: clones on this machine, clones on a
server over ssh, and repositories on GitHub over the API without being cloned at
all.

A file is in the catalog if it is

1. tracked by git,
2. under the `results/` directory at the repository root, and
3. named by the `crossrepo.yml` in that directory — as a file, or as a directory
   holding one dataset split across many files.

## Publishing

A repository states what it publishes in a `crossrepo.yml` beside the files. A
results directory without one publishes nothing, so working files, intermediates
and scratch output stay out of everyone else's catalog without anyone having to
tidy up. The manifest also carries the one thing a file name cannot: what the
file is.

```yaml
# results/crossrepo.yml
files:
  candidates.csv: Sweep candidates, one row per gene
  tmrca_stats.hdf: TMRCA per 100 kb window, autosomes only
  "SRR*/QC_table.txt": Per-sample Hi-C quality summary
```

Keys are file names, paths relative to the manifest, or glob patterns, so a
directory of three hundred per-sample tables does not need three hundred lines.
An exact key beats a glob, so one file among many can be described on its own.
Values are descriptions; the longer form is accepted too, and leaves the format
somewhere to grow:

```yaml
files:
  hits.csv:
    description: Genome-wide association hits, p < 5e-8
```

### Files kept outside the results directory

A key beginning with `/` is a path from the repository root rather than from the
manifest, which publishes a result that lives with the data it came from:

```yaml
files:
  hits.csv: In the results directory, as usual
  /data/reference/samples.csv: Somewhere else in the repository
  /data/raw/*.tsv: A pattern, matched against the path from the root
```

Any tracked file can be named this way, and a directory named this way is a
dataset like any other. A key cannot climb out with `..`: there is one spelling
for a path that leaves the results directory, so what a manifest reaches is
plain to read from the key alone. A key *without* a leading `/` never reaches
outside, so `hits.csv` publishes the one beside the manifest and not its
namesake elsewhere in the repository.

### Files too large to commit

A file the pipeline writes but nobody wants in git history is published as a
**link**: a symbolic link committed in the results directory, pointing at
wherever the pipeline put it, and a *stamp* in the manifest saying which content
that link stands for.

```console
$ ln -s ../steps/very_large_file.csv results/very_large_file.csv
$ crossrepo stamp
  stamped  results/very_large_file.csv  (41M)
stamped 1 file; commit the crossrepo.yml to publish this version
```

```yaml
files:
  very_large_file.csv:
    description: Merged per-sample table
    sha256: "3f9a...c1"        # written by `crossrepo stamp`
    size: 41231234
```

Git versions the link, not the bytes behind it, so a link on its own would let
the content change without the version changing. The stamp is what closes that
gap, and it is why this works at all: it is committed, so **the commit that
changes a stamp is the new version**, and `versions` lists the commits in which
the stamp moved rather than every commit that touched the manifest. It is also
the cache key, so two links whose target paths read alike cannot be confused for
each other, and `crossrepo cache --verify` can check a linked file like any other.

Regenerating the file means stamping it again and committing that.
`crossrepo stamp --check` writes nothing and exits non-zero when a stamp is out of
date, which is what to put in a pre-commit hook or in CI.

The point of publishing this way is that **nothing is copied**. When the file
and the cache are on one filesystem the cache holds a *hard link* to the file
the pipeline wrote:

```python
>>> os.stat(crossrepo.get("proj", "very_large_file.csv")).st_ino
>>> os.stat("steps/very_large_file.csv").st_ino          # the same inode
```

A copy is the fallback across filesystems, and a stream the fallback over ssh.
Content is checked against its stamp before it is cached, size first, so a file
regenerated since it was stamped is caught without reading it.

What this costs is that the bytes exist only where the pipeline wrote them.
Someone who clones the repository from GitHub gets the link and no content, and
`get` says so, naming the file and the machine; reading it means configuring a
root that has it, including one on a server over ssh. And because a cached
object is a hard link, a pipeline that *truncates and rewrites* its output in
place changes the cached object with it — one that writes a new file and renames
it over the old, as workflow managers do, leaves the cache alone. `crossrepo cache
--verify` is what catches the difference.

A stamp cannot go on a glob pattern, one digest describing one file, so a link
is named in full. Links inside a dataset directory are not published for the
same reason.

### Datasets split across files

A table too large for one file — a `.parquet` directory partitioned to stay
under GitHub's file size limit — is published by naming the *directory*, as
though it were a file:

```yaml
files:
  variants.parquet: All called variants, partitioned by chromosome
```

crossrepo works out that it is a directory and treats it as the single dataset it
is: one line in the catalog, the total size, the version being the last commit
to touch any part, and `get` returning the directory for `pd.read_parquet` to
read. Nothing about `.parquet` is special; naming a directory is what makes it a
dataset, so `.zarr` and anything else shaped that way work the same.

The content key is the directory's **git tree sha**, which hashes the whole
directory, so two identical datasets are stored once just as two identical files
are. Parts are cached individually, so repartitioning one file of forty costs
one file, not forty.

One manifest governs a repository: the `crossrepo.yml` sitting directly in the
`results/` directory at the repository root. It covers everything beneath, so a
file in a subdirectory is published by naming `sub/table.csv` or a glob, and it
reaches the rest of the repository by naming a path from the root. A
`crossrepo.yml` deeper in the tree is not read, and a `results/` directory that is
not at the repository root is not a results directory — there is exactly one
place to look to see what a repository publishes.

Manifests are read from git rather than from the working tree, so an uncommitted
one publishes nothing, and the same rules hold for a repository read over the
network.

## Versioning

A file's version is **the commit the repository points at** — not a repository
tag, and not the commit in which that particular file last changed. One lookup
stamps everything a repository publishes, which is what makes reading a
repository over the network cost the same whether it publishes one file or three
hundred.

A version therefore names a state of the whole repository, so it changes when
anything in the repository changes, even if the file did not. What it addresses
is still exactly the bytes that were cataloged, because the content is read at
that commit — so a pin stays reproducible, it is just not a claim that the file
is unchanged.

Repository tags are not used: across 25 repositories with a `results/` directory
in the munch-group and kaspermunch mirrors, exactly one carries any. Where they
exist they decorate a version and can address one (`repo:file@v1.0`), but they
never define one.

Version keys are written out as **full 40-character shas**, not abbreviated, so
a spec identifies the version to git and to GitHub without crossrepo in hand — the
sha in `repo:file@770170789d73428efb0193d4cf04a353587db9cd` goes straight into
`git show` or a GitHub URL. A shorter prefix is still accepted as input.

`crossrepo versions` still shows the commits in which a file itself changed, which
is the useful set to pin. Those commits are addressable in the ordinary way.

## Install

```bash
pixi run install-dev
```

Requires python. Git is needed only to read clones, here or on a server; reading
GitHub needs no git at all. The runtime dependencies are `click`, `pyyaml`, and `tomli` on
python older than 3.11. `pandas` is optional and needed just for
`crossrepo.frame()`.

## Configure

```bash
crossrepo config --init      # writes ~/.config/crossrepo/config.toml
```

```toml
roots = [
  "~/github-backup/munch-group",       # clones on this machine
  "kmt@genome.au.dk:~/projects",       # clones on a server, read over ssh
]
crossrepo_dirs = ["results"]
include = ["*.csv", "*.tsv", "*.parquet", "*.h5", "*.hdf", "*.store"]
exclude = ["*.png", "*.md", ".gitkeep"]
max_bytes = 0     # 0 = no limit
```

`roots` is the one setting with no useful default, and starts empty: nothing is
scanned until you name the directories your repositories are in. Each root is
either a repository itself or a directory whose immediate subdirectories are
repositories; nothing deeper is looked at. A root written `user@host:path` is on
another machine and is read over ssh — see [Reading a
server](#reading-a-server-over-ssh). Pointing it at a whole home directory
is a bad idea, because the scan then reaches into synced folders such as
OneDrive and into network mounts, which can block for a long time on a directory
that is not there.

`crossrepo_dirs` entries are paths relative to the repository root and may be at
any depth, so `analysis/step3/results` works as well as `results`. Each is
searched for a `crossrepo.yml`, which must sit directly in it — nothing deeper is
read — and matching is case-insensitive, so a repository that committed
`Results/` is found too. What that manifest publishes may live anywhere in the
repository, by being named from the repository root.

`include` and `exclude` are empty by default. What a repository publishes is now
its own to state, in its `crossrepo.yml`; these narrow that on the reading side,
for someone who wants to see only part of it.

### When a source cannot be read

A misspelled root, a server that is down, an organisation the token does not
cover: each costs only itself. The scan carries on, the catalog is written from
what could be reached, and what could not is named when it ends:

```
$ crossrepo refresh
cataloged 128 files in 21 repos
2 configured sources could not be read:
  ~/projcts: no such directory
  kmt@genome.au.dk:~/projects: ssh: connect to host genome.au.dk port 22: Connection timed out
```

The status stays zero — a scan that reached most of its sources has done its job
— so this is a notice, not a failure. The same account is given by any command
that scans, such as `crossrepo list --refresh`; reading the stored catalog says
nothing, having looked at nothing. From python these are ordinary warnings, of
class `crossrepo.config.SourceWarning`.

## Use

```bash
crossrepo repos                                  # one line per repo
crossrepo list humanXsweeps                      # files in one repo
crossrepo list -p '*.hdf'                        # by filename glob
crossrepo versions humanXsweeps:tmrca_stats.hdf
crossrepo get humanXsweeps:tmrca_stats.hdf@a1b2c3d4e5f6...   # full sha, or a prefix
crossrepo get x-gwas:hits.csv -o ./local_copy.csv
```

`get` prints the path of the file in the local cache and nothing else, so it
composes:

```bash
duckdb -c "select * from '$(crossrepo get x-gwas:hits.csv)' limit 5"
```

`--config` and `--refresh` may be given on either side of the subcommand, so
`crossrepo --refresh list` and `crossrepo list --refresh` are the same.

## From python

`get(repo, filename, hash)` is the notebook form. Give the hash to pin a result;
leave it out to take the latest, and the hash is printed together with the call
that pins it, ready to be copied back into the cell.

```python
import pandas as pd
import crossrepo

df = pd.read_csv(crossrepo.get("x-gwas", "hits.csv"))
# munch-group/x-gwas:results/hits.csv@e4f5a6b  (2026-04-11, 1.2M)
# pin this version:  crossrepo.get("x-gwas", "hits.csv", "e4f5a6b")
```

Paste that line back and the notebook reads the same bytes next year. A pinned
call is silent — unless the file has changed since, in which case it says so and
names the version to move to:

```
a newer version of munch-group/x-gwas:results/hits.csv exists:
  9f3c1a2... (2026-04-11); you asked for e4f5a6b... (2025-11-02)
```

That compares content, not commits. A version names a state of the whole
repository, so a pin falls behind whenever anything in the repository changes;
that is not news. The file itself having changed is.

```python
df = pd.read_csv(crossrepo.get("x-gwas", "hits.csv", "e4f5a6b"))
```

`filename` may be a bare name, or as much of the path as it takes to be
unambiguous. `repo` may be written `owner/repo` where two organisations use the
same repository name. `out=` also writes a copy somewhere, `quiet=True` drops
the printed line, and a tag works wherever a hash does.

```python
crossrepo.get("primate-ils", "ils_data.h5", out="./data/")   # keeps the file name
crossrepo.get("hic-borders", "borders.tsv", "v1.0")          # a tag pins too
```

Every command has a python counterpart returning a DataFrame:

```python
crossrepo.list()                     # what is published
crossrepo.list("x-gwas")             # one repository
crossrepo.list(pattern="*.parquet")  # by file name
crossrepo.list(brief=True)           # just what each file is, and how big
crossrepo.list(version=True)         # with the sha that pins each file
crossrepo.list(url=True)             # with the GitHub URL of each version

crossrepo.repos()                    # one row per repository
crossrepo.versions("x-gwas", "hits.csv")   # when the file itself changed
crossrepo.refresh()                  # rescan, then list
crossrepo.diagnose()                 # why is the catalog empty
```

The columns are `owner`, `repo`, `name`, `description`, `date`, `github`,
`path`, `dir`, `bytes`, `tags`, `lfs`. The repository is carried whole as
`github` (`owner/repo`) and in halves as `owner` and `repo`; the file likewise
as `path` and as `dir` plus `name` — so grouping by account, by repository or by
directory needs no string splitting. `crossrepo.frame()` adds `version`, `parts`,
`spec` and `url`.

`brief=True` cuts it to `owner`, `repo`, `name`, `size`, `description`, `date` —
what each file is and how big, with `size` written for reading (`512.2 MB`)
rather than counted in bytes.

`refresh` draws a progress bar, one step per repository — a widget in a
notebook, a text bar in a terminal. Reading a whole organisation takes about a
minute for 150 repositories, so it is worth seeing. `crossrepo refresh` does the
same when standard error is a terminal, and stays silent in a pipe or a log.
Pass `progress=False` to turn it off.

`fetch` takes a spec string instead, if that suits better:

```python
fetch("munch-group/x-gwas:hits.csv@e4f5a6b...")
```

Specs are `[owner/]repo:path[@version]`. The path may be a bare file name when
it is unambiguous within the repository; `owner` disambiguates repositories that
exist in two organisations, such as `primate-ils`. Omit `@version` for the
latest committed version. When a spec is ambiguous the error lists the
candidates as full specs, so one can be copied straight back into the command.

## Cache

Files are cached under `~/.cache/crossrepo/` keyed by a **content hash**: the git
blob sha, the Git LFS object id, or the manifest stamp of a file published as a
link. Consequences:

- A result file that did not change between two commits is stored once.
- Byte-identical files in two repositories are stored once. In the munch-group
  mirrors, `result_table.csv` appears in ten repositories and is one blob. This
  holds across kinds too: a linked file and an LFS object of the same bytes are
  one object.
- Cached content is immutable, so a pinned version never changes underfoot.

`~/.cache/crossrepo/files/<repo>/<version>/<path>` gives readable hard links to
the same bytes. The whole path is kept, not just the file name, because one
repository may hold two files of the same name that last changed in the same
commit. `CROSSREPO_CACHE` overrides the location.

The stored catalog records the settings it was built with, so changing `roots`
takes effect at once rather than when the stored catalog ages out.

## Git LFS

Pointer files are resolved transparently: the catalog reports the *real* size of
an LFS-tracked file, and `get` serves it from the repository's local LFS object
store. If the object has not been fetched, the error names the `git lfs fetch`
command to run rather than handing back a 130-byte pointer file.

Large files are streamed rather than buffered. Fetching the 489 MB
`primate-ils/results/ils_data.h5` peaks at about 13 MB of resident memory.

## Limits worth knowing

- Only committed files are visible, and only those a committed manifest names.
  This is the design, but it means anything a workflow produces on the cluster
  and never commits will not appear — unless it is published as a link, which
  is what links are for.
- Result files live in git history forever. Fine for the median file in these
  repositories (about 5 KB) and for the p90 (about 550 KB); not fine for a
  489 MB HDF5 file, which is why those are in LFS or published as links.
- A file is published only once it is named in a `crossrepo.yml`. That is the
  point — it is what keeps a results directory from publishing its scratch
  output — but it does mean a repository publishes nothing until someone writes
  the manifest.

## Reading GitHub without cloning

Nothing has to be checked out. Name the organisations, or single repositories,
and crossrepo reads them over the API:

```toml
owners = ["munch-group", "kaspermunch"]
repos  = ["someone-else/shared-results"]   # optional extras
```

```bash
crossrepo list --url        # files, with the URL of each version
crossrepo get x-gwas:hits.csv --url   # just the URL
crossrepo get x-gwas:hits.csv         # download it, print the cached path
```

```python
df = pd.read_csv(crossrepo.get("x-gwas", "hits.csv"))   # in a notebook
```

Authentication is `GITHUB_TOKEN`, or whatever `gh auth login` already stored.
Without a token only public repositories are readable and the rate limit is 60
requests an hour instead of 5000.

URLs name the **commit**, not the branch, so a URL keeps pointing at the bytes
that were cataloged:

```
https://raw.githubusercontent.com/munch-group/tree-stats/7701707…/results/dummy.csv
```

### What it costs

Three or four requests per repository, and **flat in the number of files** — a
repository publishing three hundred result files costs no more to catalog than
one publishing a single file. Measured against `munch-group/tree-stats`:

```
git/trees/HEAD?recursive=1     does it publish anything, and what
git/blobs/<manifest>           the crossrepo.yml
git/blobs/<.gitattributes>     only if some published file is small enough to be an LFS pointer
commits/HEAD                   the commit that stamps everything
```

A repository without a `crossrepo.yml` stops after the first, so scanning a whole
organisation costs about one request per repository that publishes nothing. A
repository named outright in `repos` that publishes nothing costs one more, to
tell "publishes nothing" from "not there" — one for a handful of named
repositories, never for the hundreds an organisation may hold.
`HEAD` is a ref GitHub resolves itself, so finding the default branch costs
nothing extra.

### Alongside local clones

Local `roots` and GitHub `owners` can both be set. Where both know a file,
GitHub decides the version, because a clone is only as current as its last pull;
the clone is kept as somewhere to read content from. Since the cache is keyed by
git's own blob sha — the same sha over the API as on disk — content you already
have is never downloaded again, whichever way it arrived.

## Reading a server over ssh

Results that live on a cluster or a group server, and are never pushed to
GitHub, are read where they are. Write the root the way ssh and scp write one:

```toml
roots = ["~/projects", "kmt@genome.au.dk:~/projects"]
```

Nothing is cloned and nothing is mounted. Git runs on the far side and only its
output crosses the network, so a server holding a hundred repositories costs no
local disk, and the catalog, the history and the file content all come back the
same way they do from a clone here:

```bash
crossrepo list                          # repos on the server are simply in the list
crossrepo get sweep-scan:hits.csv       # content streamed into the local cache
crossrepo versions sweep-scan:hits.csv  # history read over ssh
```

A root can be absolute, `~`-relative, or relative to where ssh puts you:
`me@server:projects` and `me@server:~/projects` are the same directory.

What it needs:

- **ssh access, as you already have it.** crossrepo keeps no credentials and adds
  no configuration of its own; it runs `ssh` and lets it do what it does.
- **git on the `PATH` of an ssh *command*.** Nothing else — no crossrepo, no
  python, no daemon — but this one is worth checking, because it is not the
  same as git working when you log in:

  ```bash
  ssh me@server git --version      # must print a version
  ```

  A cluster where git comes from a module, or a `~/.bashrc` that returns early
  when it is not interactive, will have git for a login shell and not for this.
  Repositories are still *found* — that takes only a shell — but none of them
  can be read, so crossrepo checks once per host and says so rather than
  cataloging nothing.
- The same rules as anywhere else — a `crossrepo.yml`, committed, and the files it
  names committed too.

### When the host asks for something

A key with a passphrase, or a cluster login wanting a second factor, is answered
the way it always is: ssh prompts at the terminal, you type it, and the answer
goes to ssh rather than through crossrepo. The connection to each host is opened
before the scan starts, so the prompt comes first and once, rather than in the
middle of a progress bar:

```
$ crossrepo refresh
(kmt@login.genome.au.dk) Verification code: ······
cataloged 128 files in 21 repos
```

**In a notebook there is no terminal, but there is somewhere to ask**: the
prompt opens at the top of the window, the way any `getpass` in a cell does, and
what you type goes to ssh.

```python
crossrepo.catalog(refresh=True)
# ┌────────────────────────────────────────────┐
# │ Verification code:                         │  ← VS Code, JupyterLab, …
# └────────────────────────────────────────────┘
```

That works by giving ssh an `SSH_ASKPASS` program of crossrepo's own, which asks
the kernel that started ssh rather than a terminal it does not have. The answer
goes from the prompt to ssh and is not kept, printed, or written anywhere.

**In a scheduled job there is neither**, so ssh is told not to ask and the host
is reported with what to do about it:

```
1 configured source could not be read:
  kmt@login.genome.au.dk:~/projects: Permission denied (keyboard-interactive).
    no terminal here to answer a key passphrase or a two-factor code: run
    `ssh kmt@login.genome.au.dk true` where you can answer it, then try again
    within 5 minutes
```

Five minutes is how long crossrepo keeps its own shared connection open. To be
asked once a day rather than once a session, set the connection up in your own
ssh config, which is used as it stands — crossrepo adds nothing where you have
decided something:

```
Host gdk
    HostName        login.genome.au.dk
    User            kmt
    ControlMaster   auto
    ControlPath     ~/.ssh/cm-%r@%h:%p
    ControlPersist  4h
```

Then one answered prompt covers the working day, and `roots = ["gdk:~/projects"]`
reads through that same connection from a terminal and a notebook alike.

`~` in the path is expanded on the server, since that is the machine that knows
where home is. Cataloguing a repository takes a handful of git commands, so
calls to one host share a single ssh connection, kept open for a minute after
the last of them; a scan of a whole server is one connection, not one per
command. A host that does not answer costs a warning and the other roots are
still read.

If a repository is checked out both here and on the server, the clone here is
used: either can supply the file, and reading from a clone on this machine costs
no round trip. `CROSSREPO_SSH` overrides the ssh command for a setup that ssh's
own options cannot express, e.g. `CROSSREPO_SSH="ssh -F ~/.ssh/other_config"`.

One caveat, shared with `rsync` and `scp`: what git prints on the server is
parsed here, so a shell startup file that prints something for non-interactive
commands — a banner or a `module load` message in `.bashrc` — ends up in the
middle of that output. Guarding it with the usual interactive check keeps it out
of the way:

```bash
case $- in *i*) ;; *) return;; esac    # near the top of ~/.bashrc
```

## Tests

```bash
pixi run test
```

The suite builds real git repositories — with tags, an LFS pointer, a
capitalised `Results/`, untracked files, one file name in two subdirectories, a
non-ASCII file name, a manifest too deep in the tree to be read, a
repository that commits results but publishes none of them, a `.parquet`
directory published as one dataset across two versions, and content duplicated
across repositories — and runs against them. Git is not mocked.

## Documentation

```bash
pixi run api      # build quartodoc api pages
pixi run docs     # execute the documentation notebooks
```

## Get updates to upstream fork

Add upstream if not already added

```bash
git remote add upstream https://github.com/munch-group/crossrepo.git
```

Fetch upstream changes

```bash
git fetch upstream
```

Either rebase your changes on top of upstream (cleaner history)

```bash
git rebase upstream/main
```

Or, merge upstream into your fork (preserves history)

```bash
git merge upstream/main
```

If you want to see what's changed upstream before applying:

```bash
git log HEAD..upstream/main
```

See the actual diff

```bash
git diff HEAD...upstream/main
```

Then push your updated fork:

```bash
git push origin main
```

If you rebased and need to force push
    
```bash
git push origin main --force-with-lease
```
