# labdata

Catalog and fetch versioned result files across many git repositories, without
submodules and without a manifest in each repository.

A file is in the catalog if it is

1. tracked by git,
2. under a `results/` directory, and
3. matches the include/exclude patterns.

Nothing has to be added to the producing repository. Committing a file to
`results/` *is* the act of publishing it.

## Versioning

A file's version is **the commit in which that file last changed** — not a
repository tag. This is deliberate: across 25 repositories with a `results/`
directory in the munch-group and kaspermunch mirrors, exactly one carries any
tags. Per-file commits give a version key that already exists as a side effect
of working.

Tags, where present, decorate a version and can be used to address one
(`repo:file@v1.0`), but they never define one.

## Install

```bash
pixi run install-dev
```

Requires python and git. The only runtime dependency is `tomli`, and only on
python older than 3.11. `pandas` is optional and needed just for
`labdata.frame()`.

## Configure

```bash
labdata config --init      # writes ~/.config/labdata/config.toml
```

```toml
roots = ["~/github-backup/kaspermunch", "~/github-backup/munch-group"]
depth = 2
results_dirs = ["results"]
include = ["*.csv", "*.tsv", "*.parquet", "*.h5", "*.hdf", "*.store"]
exclude = ["*.png", "*.md", ".gitkeep"]
max_bytes = 0     # 0 = no limit
```

`results_dirs` is matched case-insensitively, so a repository that committed
`Results/` is found too.

## Use

```bash
labdata repos                                  # one line per repo
labdata list humanXsweeps                      # files in one repo
labdata list -p '*.hdf'                        # by filename glob
labdata versions humanXsweeps:tmrca_stats.hdf
labdata get humanXsweeps:tmrca_stats.hdf@a1b2c3d
labdata get x-gwas:hits.csv -o ./local_copy.csv
```

`get` prints the path of the file in the local cache and nothing else, so it
composes:

```bash
duckdb -c "select * from '$(labdata get x-gwas:hits.csv)' limit 5"
```

From python:

```python
import pandas as pd
from labdata import catalog, fetch, frame

frame()                                   # the whole catalog as a DataFrame
df = pd.read_csv(fetch("munch-group/x-gwas:hits.csv@e4f5a6b"))
```

Specs are `[owner/]repo:path[@version]`. The path may be a bare file name when
it is unambiguous within the repository; `owner` disambiguates repositories that
exist in two organisations, such as `primate-ils`. Omit `@version` for the
latest committed version. When a spec is ambiguous the error lists the
candidates as full specs, so one can be copied straight back into the command.

## Cache

Files are cached under `~/.cache/labdata/` keyed by **git blob sha**, which is a
content hash. Consequences:

- A result file that did not change between two commits is stored once.
- Byte-identical files in two repositories are stored once. In the munch-group
  mirrors, `result_table.csv` appears in ten repositories and is one blob.
- Cached content is immutable, so a pinned version never changes underfoot.

`~/.cache/labdata/files/<repo>/<version>/<name>` gives readable hard links to
the same bytes. `LABDATA_CACHE` overrides the location.

## Git LFS

Pointer files are resolved transparently: the catalog reports the *real* size of
an LFS-tracked file, and `get` serves it from the repository's local LFS object
store. If the object has not been fetched, the error names the `git lfs fetch`
command to run rather than handing back a 130-byte pointer file.

Large files are streamed rather than buffered. Fetching the 489 MB
`primate-ils/results/ils_data.h5` peaks at about 13 MB of resident memory.

## Limits worth knowing

- Only committed files are visible. This is the design, but it means anything a
  workflow produces on the cluster and never commits will not appear.
- Result files live in git history forever. Fine for the median file in these
  repositories (about 5 KB) and for the p90 (about 550 KB); not fine for a
  489 MB HDF5 file, which is why those are in LFS.
- There is no description field. A `results/README.md` beside the files is the
  low-tech answer; a manifest is the higher-tech one, if it is ever wanted.

## Adding a remote backend

`labdata.core.build()` returns `Entry` objects assembled from a git working
tree. A GitHub backend that never clones would produce the same `Entry` objects
from `GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1` plus
`GET /repos/{owner}/{repo}/commits?path=...`, and everything downstream —
resolution, versions, cache, CLI — works unchanged.

## Tests

```bash
pixi run test
```

The suite builds real git repositories — with tags, an LFS pointer, a
capitalised `Results/`, untracked files, one file name in two subdirectories,
and content duplicated across repositories — and runs against them. Git is not
mocked.

## Documentation

```bash
pixi run api      # build quartodoc api pages
pixi run docs     # execute the documentation notebooks
```

## Get updates to upstream fork

Add upstream if not already added

```bash
git remote add upstream https://github.com/munch-group/labdata.git
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
