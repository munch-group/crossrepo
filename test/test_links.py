"""
Tests for result files published as symbolic links.

A file too large to commit is published by committing a link to wherever the
pipeline wrote it, and a stamp in the manifest saying which content the link
stands for. Git versions the link; the stamp versions the bytes. These tests
run against real repositories, so what is being checked is git's own treatment
of a symbolic link -- the mode it records, and that the blob holds the target
path rather than any content.
"""

import os
import shutil
import warnings
from pathlib import Path

import pytest

from crossrepo import cache, cli, core, gitutil, manifest
from crossrepo.config import Config
from crossrepo.manifest import Stamp

from fixtures import (
    LINK_OTHER, LINK_V1, LINK_V2, commit, digest, fake_ssh, init, make_links,
    restamp, tree_digest, write_dataset_link_repo, write_link_repo,
)


@pytest.fixture(scope="module")
def links(tmp_path_factory):
    """The repositories that publish a file as a link."""
    base = make_links(tmp_path_factory.mktemp("links") / "fx")
    os.environ["CROSSREPO_CACHE"] = str(tmp_path_factory.mktemp("linkcache"))
    return base


@pytest.fixture(scope="module")
def link_cfg(links):
    """Settings covering them."""
    return Config(roots=[str(links / "links")])


@pytest.fixture(scope="module")
def linked(link_cfg):
    """The catalog they publish, keyed by ``repo:path``."""
    return {f"{e.repo}:{e.path}": e for e in linked_entries(link_cfg)}


def linked_entries(cfg: Config):
    """Build the catalog, the `bare` repository's warning being expected."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return core.build(cfg)


def one(links: Path, repo: str) -> Path:
    """The target of the link a fixture repository publishes."""
    return links / "links" / repo / "steps" / "big.csv"


# ------------------------------------------------------------------ publishing

def test_a_stamped_link_is_published(linked):
    entry = linked["proj:results/big.csv"]
    assert entry.description == "Merged per-sample table"
    assert entry.latest.link == "../steps/big.csv"
    assert entry.manifest == "results/crossrepo.yml"


def test_the_size_is_the_content_not_the_link(linked):
    # The blob git holds is the target path, sixteen bytes of it.
    assert linked["proj:results/big.csv"].latest.size == len(LINK_V2)


def test_the_content_key_is_the_stamp(linked):
    assert linked["proj:results/big.csv"].latest.blob == digest(LINK_V2)


def test_an_ordinary_file_beside_it_is_unaffected(linked):
    plain = linked["proj:results/plain.csv"]
    assert plain.latest.link is None
    assert plain.latest.blob != digest(LINK_V2)


def test_a_link_with_no_stamp_is_not_published(link_cfg):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        entries = core.build(link_cfg)
    said = [str(w.message) for w in caught]
    assert not [e for e in entries if e.repo == "bare" and e.name == "big.csv"]
    assert any("bare" in s and "no stamp" in s for s in said)
    assert any("crossrepo stamp" in s for s in said)


def test_a_missing_target_is_still_published(linked):
    # What a link stands for is what the manifest says, not what this machine
    # happens to hold; the absence is reported when the content is asked for.
    assert "gone:results/big.csv" in linked


# ------------------------------------------------------- the cache holds a link

def test_the_cache_hard_links_what_the_pipeline_wrote(links, linked):
    entry = linked["proj:results/big.csv"]
    path = core.materialize(entry, entry.latest)
    assert path.read_text() == LINK_V2
    assert path.stat().st_ino == one(links, "proj").stat().st_ino


def test_reading_it_again_reads_nothing(linked):
    entry = linked["proj:results/big.csv"]
    first = core.materialize(entry, entry.latest)
    blob = cache.blob_path(entry.latest.blob)
    before = blob.stat().st_ino
    assert core.materialize(entry, entry.latest) == first
    assert blob.stat().st_ino == before


def test_a_stamped_object_verifies(linked):
    entry = linked["proj:results/big.csv"]
    core.materialize(entry, entry.latest)
    assert cache.check_object(cache.blob_path(entry.latest.blob)) is None


def test_two_links_that_read_alike_do_not_collide(links, linked):
    proj = linked["proj:results/big.csv"]
    twin = linked["twin:results/big.csv"]
    # Git gives both links the same blob, the target path being the same text.
    at = gitutil.tracked_entries(links / "links" / "proj", ":(icase)results")
    also = gitutil.tracked_entries(links / "links" / "twin", ":(icase)results")
    assert at["results/big.csv"][0] == also["results/big.csv"][0]
    # Keying on the content instead is what keeps them apart.
    assert proj.latest.blob != twin.latest.blob
    assert core.materialize(twin, twin.latest).read_text() == LINK_OTHER
    assert core.materialize(proj, proj.latest).read_text() == LINK_V2


# ------------------------------------------------------------------- versioning

def test_a_version_per_stamp(linked):
    got = core.versions(linked["proj:results/big.csv"])
    assert [v.blob for v in got] == [digest(LINK_V2), digest(LINK_V1)]
    assert [v.size for v in got] == [len(LINK_V2), len(LINK_V1)]


def test_a_commit_that_leaves_the_stamp_alone_is_not_a_version(linked):
    got = core.versions(linked["proj:results/big.csv"])
    assert len(got) == 2
    assert [v.subject for v in got] == [
        "regenerate the big table", "first results",
    ]


def test_the_commit_kept_is_the_one_that_introduced_the_content(linked):
    got = core.versions(linked["proj:results/big.csv"])
    assert got[0].subject == "regenerate the big table"


def test_a_stamp_can_be_pinned(linked):
    entry = linked["proj:results/big.csv"]
    first = core.versions(entry)[-1]
    found = core.find_version(entry, first.sha)
    assert found.blob == digest(LINK_V1)
    assert found.size == len(LINK_V1)


def test_a_pin_uncached_and_overwritten_is_gone(linked):
    entry = linked["proj:results/big.csv"]
    first = core.versions(entry)[-1]
    # The first version was never read, and the file has since been written
    # over, so there is nowhere left for those bytes to come from.
    with pytest.raises(ValueError, match="regenerated"):
        core.materialize(entry, first)


def test_a_pin_read_before_the_file_moved_on_survives_in_the_cache(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CROSSREPO_CACHE", str(tmp_path / "own-cache"))
    repo = write_link_repo(tmp_path / "kept", LINK_V1)
    cfg = Config(roots=[str(tmp_path)])
    entry = [e for e in core.build(cfg) if e.name == "big.csv"][0]
    core.materialize(entry, entry.latest)          # cached while still on disk
    restamp(repo, LINK_V2, "regenerate the big table")

    entry = [e for e in core.build(cfg) if e.name == "big.csv"][0]
    old, new = core.versions(entry)[-1], core.versions(entry)[0]
    assert new.blob == digest(LINK_V2)
    # The cache holds a hard link, so writing a new file over the old name left
    # the bytes the pin names alive and readable.
    assert core.materialize(entry, old).read_text() == LINK_V1
    assert core.materialize(entry, new).read_text() == LINK_V2


def test_rewriting_a_target_in_place_is_caught_by_verification(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CROSSREPO_CACHE", str(tmp_path / "own-cache"))
    repo = write_link_repo(tmp_path / "inplace", LINK_V1)
    cfg = Config(roots=[str(tmp_path)])
    entry = [e for e in core.build(cfg) if e.name == "big.csv"][0]
    blob = cache.blob_path(entry.latest.blob)
    core.materialize(entry, entry.latest)
    assert cache.check_object(blob) is None

    # A cached object is a hard link to the file the pipeline wrote, so a
    # pipeline that truncates and rewrites that file rather than renaming a new
    # one over it changes the cache with it. Nothing can prevent that; what
    # matters is that the cache can be asked, and says so.
    (repo / "steps" / "big.csv").write_text(LINK_V2)
    assert cache.check_object(blob) == "content does not hash to its key"


def test_outdated_fires_when_the_stamp_moves(linked):
    entry = linked["proj:results/big.csv"]
    first = core.versions(entry)[-1]
    assert "newer version" in (core.outdated(entry, first) or "")
    assert core.outdated(entry, entry.latest) is None


# ---------------------------------------------------------- what can go wrong

def test_a_missing_target_names_the_machine(linked):
    entry = linked["gone:results/big.csv"]
    with pytest.raises(FileNotFoundError) as raised:
        core.materialize(entry, entry.latest)
    said = str(raised.value)
    assert "../steps/big.csv" in said and "on this machine" in said


def test_content_that_does_not_match_its_stamp_is_refused(linked):
    entry = linked["stale:results/big.csv"]
    with pytest.raises(ValueError) as raised:
        core.materialize(entry, entry.latest)
    said = str(raised.value)
    assert "regenerated" in said and "crossrepo stamp" in said


def test_nothing_is_cached_when_the_stamp_does_not_match(linked):
    entry = linked["stale:results/big.csv"]
    with pytest.raises(ValueError):
        core.materialize(entry, entry.latest)
    assert not cache.blob_path(entry.latest.blob).exists()
    assert not list(cache.blob_path(entry.latest.blob).parent.glob("*.tmp"))


def test_a_stamp_on_a_committed_file_is_ignored_with_a_warning(tmp_path):
    repo = init(tmp_path / "odd")
    (repo / "results").mkdir()
    (repo / "results" / "plain.csv").write_text("k,v\nx,1\n")
    (repo / "results" / "crossrepo.yml").write_text(
        "files:\n  plain.csv:\n    description: committed all along\n"
        f'    sha256: "{digest("k,v\\nx,1\\n")}"\n    size: 8\n'
    )
    commit(repo, "stamped by mistake")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        entries = core.scan_repo(gitutil.Location.of(repo), Config())
    assert [e.name for e in entries] == ["plain.csv"]
    assert entries[0].latest.link is None       # git's own version, as before
    assert any("stamp is ignored" in str(w.message) for w in caught)


def test_a_link_named_from_the_repository_root_is_published(tmp_path):
    repo = init(tmp_path / "anchored")
    (repo / ".gitignore").write_text("steps/\n")
    (repo / "steps").mkdir()
    (repo / "data").mkdir()
    (repo / "results").mkdir()
    (repo / "steps" / "big.csv").write_text(LINK_V1)
    (repo / "data" / "big.csv").symlink_to("../steps/big.csv")
    (repo / "results" / "crossrepo.yml").write_text(
        "files:\n  /data/big.csv:\n    description: Kept beside the raw data\n"
        f'    sha256: "{digest(LINK_V1)}"\n    size: {len(LINK_V1)}\n'
    )
    commit(repo, "publish a link from outside the results directory")
    entries = core.build(Config(roots=[str(tmp_path)]))
    assert [e.path for e in entries] == ["data/big.csv"]
    assert entries[0].latest.link == "../steps/big.csv"
    assert core.materialize(entries[0], entries[0].latest).read_text() == LINK_V1


# --------------------------------------------------- the manifest format itself

def test_a_stamp_is_read_from_the_mapping_form():
    m = manifest.parse(
        f'files:\n  big.csv:\n    description: d\n    sha256: "{"a" * 64}"\n'
        "    size: 12\n",
        "results",
    )
    assert m.describe("results/big.csv") == "d"
    assert m.stamp("results/big.csv") == Stamp("a" * 64, 12)


def test_a_file_with_no_stamp_has_none():
    m = manifest.parse("files:\n  plain.csv: d\n", "results")
    assert m.stamp("results/plain.csv") is None


def test_a_stamp_is_found_under_an_anchored_key():
    m = manifest.parse(
        f'files:\n  /steps/big.csv:\n    sha256: "{"a" * 64}"\n    size: 1\n',
        "results",
    )
    assert m.stamp("steps/big.csv") == Stamp("a" * 64, 1)
    assert m.stamp("results/steps/big.csv") is None


def test_a_stamp_is_found_under_a_bare_file_name():
    m = manifest.parse(
        f'files:\n  big.csv:\n    sha256: "{"a" * 64}"\n    size: 1\n', "results"
    )
    assert m.stamp("results/sub/big.csv") == Stamp("a" * 64, 1)


@pytest.mark.parametrize(
    "text, complaint",
    [
        (f'files:\n  "*.csv":\n    sha256: "{"a" * 64}"\n    size: 1\n', "pattern"),
        (f'files:\n  b.csv:\n    sha256: "{"a" * 64}"\n', "no `size`"),
        ("files:\n  b.csv:\n    size: 1\n", "no `sha256`"),
        ('files:\n  b.csv:\n    sha256: "nope"\n    size: 1\n', "not a digest"),
        (f'files:\n  b.csv:\n    sha256: "{"A" * 64}"\n    size: 1\n', "not a digest"),
        (f'files:\n  b.csv:\n    sha256: "{"a" * 64}"\n    size: -1\n', "bytes"),
        (f'files:\n  b.csv:\n    sha256: "{"a" * 64}"\n    size: big\n', "bytes"),
    ],
)
def test_a_stamp_that_says_nothing_useful_is_refused(text, complaint):
    with pytest.raises(manifest.ManifestError, match=complaint):
        manifest.parse(text, "results")


# ------------------------------------------------------- writing a stamp back

S = Stamp("a" * 64, 12)
"""A stamp to write in the tests below."""


@pytest.mark.parametrize(
    "text",
    [
        "files:\n  big.csv: a table\n  other.csv: x\n",
        "files:\n  big.csv:\n    description: a table\n  other.csv: x\n",
        f'files:\n  big.csv:\n    sha256: "{"c" * 64}"\n    size: 1\n  other.csv: x\n',
        'files:\n  "big.csv": a table\n  other.csv: x\n',
        "files:\n  big.csv:\n  other.csv: x\n",
        "files:\n  big.csv:\n    description: a table\n\n  other.csv: x\n",
        "files:\n    big.csv:\n        description: a table\n    other.csv: x\n",
    ],
)
def test_a_stamp_is_written_wherever_the_entry_stands(text):
    after = manifest.parse(manifest.write_stamp(text, "big.csv", S), "results")
    assert after.stamp("results/big.csv") == S
    assert after.describe("results/other.csv") == "x"


def test_writing_a_stamp_leaves_everything_else_alone():
    text = (
        "# what this project publishes\n"
        "files:\n"
        "  # the big one\n"
        "  big.csv: a table   # trailing note\n"
        "  other.csv: x\n"
    )
    after = manifest.write_stamp(text, "big.csv", S)
    assert "# what this project publishes" in after
    assert "# the big one" in after
    assert "a table   # trailing note" in after
    assert after.count("other.csv: x") == 1


def test_writing_a_stamp_changes_two_lines():
    text = (
        "files:\n  big.csv:\n    description: a table\n"
        f'    sha256: "{"c" * 64}"\n    size: 1\n'
    )
    before = text.splitlines()
    after = manifest.write_stamp(text, "big.csv", S).splitlines()
    assert len(after) == len(before)
    assert [i for i, (a, b) in enumerate(zip(before, after)) if a != b] == [3, 4]


def test_writing_the_same_stamp_twice_changes_nothing():
    once = manifest.write_stamp("files:\n  big.csv: a table\n", "big.csv", S)
    assert manifest.write_stamp(once, "big.csv", S) == once


def test_a_stamp_needs_a_key_to_go_under():
    with pytest.raises(manifest.ManifestError, match="not in the manifest"):
        manifest.write_stamp("files:\n  a.csv: x\n", "nope.csv", S)


# --------------------------------------------------------------- crossrepo stamp

@pytest.fixture()
def stampable(tmp_path):
    """A repository with an unstamped link, and a config file naming nothing."""
    repo = write_link_repo(tmp_path / "fresh", LINK_V1, stamped=False)
    conf = tmp_path / "config.toml"
    conf.write_text('roots = []\nasset_dirs = ["results"]\n')
    return repo, str(conf)


def run_in(repo: Path, conf: str, *args: str) -> int:
    """Run the CLI as though from inside a repository."""
    was = Path.cwd()
    os.chdir(repo)
    try:
        return cli.main(["--config", conf, *args])
    finally:
        os.chdir(was)


def test_stamp_writes_what_the_link_points_at(stampable, capsys):
    repo, conf = stampable
    assert run_in(repo, conf, "stamp") == 0
    capsys.readouterr()
    text = (repo / "results" / "crossrepo.yml").read_text()
    got = manifest.parse(text, "results")
    assert got.stamp("results/big.csv") == Stamp(digest(LINK_V1), len(LINK_V1))


def test_stamp_says_what_it_did(stampable, capsys):
    repo, conf = stampable
    run_in(repo, conf, "stamp")
    out = capsys.readouterr().out
    assert "stamped" in out and "results/big.csv" in out
    assert "commit" in out


def test_stamp_is_idempotent(stampable, capsys):
    repo, conf = stampable
    run_in(repo, conf, "stamp")
    before = (repo / "results" / "crossrepo.yml").read_text()
    capsys.readouterr()
    assert run_in(repo, conf, "stamp") == 0
    assert (repo / "results" / "crossrepo.yml").read_text() == before
    assert "up to date" in capsys.readouterr().out


def test_check_reports_without_writing(stampable, capsys):
    repo, conf = stampable
    before = (repo / "results" / "crossrepo.yml").read_text()
    assert run_in(repo, conf, "stamp", "--check") == 1
    assert (repo / "results" / "crossrepo.yml").read_text() == before
    assert "out of date" in capsys.readouterr().err


def test_check_passes_once_stamped(stampable, capsys):
    repo, conf = stampable
    run_in(repo, conf, "stamp")
    capsys.readouterr()
    assert run_in(repo, conf, "stamp", "--check") == 0


def test_stamp_reports_a_link_that_leads_nowhere(tmp_path, capsys):
    repo = write_link_repo(tmp_path / "dangling", None, stamped=False)
    conf = tmp_path / "config.toml"
    conf.write_text('roots = []\nasset_dirs = ["results"]\n')
    assert run_in(repo, str(conf), "stamp") == 1
    assert "not there" in capsys.readouterr().err


def test_stamp_reports_a_link_published_only_by_a_pattern(tmp_path, capsys):
    repo = write_link_repo(
        tmp_path / "globbed", LINK_V1, stamped=False, key='"*.csv"'
    )
    conf = tmp_path / "config.toml"
    conf.write_text('roots = []\nasset_dirs = ["results"]\n')
    assert run_in(repo, str(conf), "stamp") == 1
    said = capsys.readouterr().err
    assert "pattern" in said and "name the file in full" in said


def test_a_link_named_from_the_root_can_be_stamped(tmp_path, capsys):
    repo = init(tmp_path / "anchor-stamp")
    (repo / ".gitignore").write_text("steps/\n")
    (repo / "steps").mkdir()
    (repo / "data").mkdir()
    (repo / "results").mkdir()
    (repo / "steps" / "big.csv").write_text(LINK_V1)
    (repo / "data" / "big.csv").symlink_to("../steps/big.csv")
    (repo / "results" / "crossrepo.yml").write_text(
        "files:\n  /data/big.csv: Kept beside the raw data\n"
    )
    commit(repo, "publish it unstamped")
    conf = tmp_path / "config.toml"
    conf.write_text('roots = []\nasset_dirs = ["results"]\n')
    assert run_in(repo, str(conf), "stamp") == 0
    capsys.readouterr()
    got = manifest.parse((repo / "results" / "crossrepo.yml").read_text(), "results")
    assert got.stamp("data/big.csv") == Stamp(digest(LINK_V1), len(LINK_V1))


def test_stamp_needs_a_repository(tmp_path, capsys):
    conf = tmp_path / "config.toml"
    conf.write_text("roots = []\n")
    plain = tmp_path / "elsewhere"
    plain.mkdir()
    assert run_in(plain, str(conf), "stamp") == 1
    assert "not in a git repository" in capsys.readouterr().err


def test_stamping_then_committing_publishes_the_version(tmp_path):
    repo = write_link_repo(tmp_path / "flow", LINK_V1, stamped=False)
    conf = tmp_path / "config.toml"
    conf.write_text('roots = []\nasset_dirs = ["results"]\n')
    run_in(repo, str(conf), "stamp")
    commit(repo, "stamp the big table")
    cfg = Config(roots=[str(tmp_path)])
    entries = core.build(cfg)
    got = [e for e in entries if e.name == "big.csv"]
    assert [e.latest.blob for e in got] == [digest(LINK_V1)]


def test_the_note_column_says_a_version_is_a_link(linked):
    assert cli.note(linked["proj:results/big.csv"].latest) == "link"
    assert cli.note(linked["proj:results/plain.csv"].latest) == ""


def test_the_listing_says_a_file_is_a_link(links, tmp_path, capsys):
    conf = tmp_path / "config.toml"
    conf.write_text(f'roots = ["{links}/links"]\n')
    assert cli.main(["--config", str(conf), "--refresh", "list", "proj"]) == 0
    rows = [
        l.split() for l in capsys.readouterr().out.splitlines() if "results/" in l
    ]
    big = [r for r in rows if r[1].endswith("big.csv")][0]
    assert "link" in big


def test_get_serves_a_link_from_the_command_line(links, tmp_path, capsys):
    conf = tmp_path / "config.toml"
    conf.write_text(f'roots = ["{links}/links"]\n')
    assert cli.main(["--config", str(conf), "get", "proj:big.csv"]) == 0
    out = capsys.readouterr()
    assert Path(out.out.strip()).read_text() == LINK_V2
    assert "results/big.csv@" in out.err          # the version it chose


# ------------------------------------------------------------- stored catalog

def test_a_link_survives_the_stored_catalog(links, link_cfg):
    entries = [
        e for e in linked_entries(link_cfg)
        if e.repo == "proj" and e.name == "big.csv"
    ]
    core.save(entries, link_cfg)
    back = core.load_cached(cfg=link_cfg)
    assert back == entries
    assert back[0].latest.link == "../steps/big.csv"
    assert back[0].manifest == "results/crossrepo.yml"


def test_the_json_listing_carries_the_link(links, link_cfg):
    entry = [
        e for e in linked_entries(link_cfg) if e.repo == "proj" and e.name == "big.csv"
    ][0]
    got = entry.to_dict()
    assert got["latest"]["link"] == "../steps/big.csv"
    assert got["manifest"] == "results/crossrepo.yml"


# ----------------------------------------------------------- over the network

@pytest.fixture()
def server(tmp_path, monkeypatch):
    """A repository publishing a link, reachable only as ``tester@fakehost``."""
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("CROSSREPO_SSH", fake_ssh(tmp_path, home))
    monkeypatch.setenv("CROSSREPO_CACHE", str(tmp_path / "cache"))
    write_link_repo(home / "projects" / "far", LINK_V1)
    return home


def test_a_link_is_resolved_on_the_machine_it_is_on(server):
    cfg = Config(roots=["tester@fakehost:~/projects"])
    entries = core.build(cfg)
    entry = [e for e in entries if e.name == "big.csv"][0]
    assert entry.source == "ssh"
    assert entry.latest.blob == digest(LINK_V1)


def test_content_behind_a_link_is_streamed_from_the_far_side(server):
    cfg = Config(roots=["tester@fakehost:~/projects"])
    entry = [e for e in core.build(cfg) if e.name == "big.csv"][0]
    path = core.materialize(entry, entry.latest)
    assert path.read_text() == LINK_V1
    assert cache.check_object(cache.blob_path(entry.latest.blob)) is None


def test_a_links_history_is_read_over_ssh(server):
    cfg = Config(roots=["tester@fakehost:~/projects"])
    entry = [e for e in core.build(cfg) if e.name == "big.csv"][0]
    restamp(server / "projects" / "far", LINK_V2, "regenerate the big table")

    entry = [e for e in core.build(cfg) if e.name == "big.csv"][0]
    got = core.versions(entry)
    assert [v.blob for v in got] == [digest(LINK_V2), digest(LINK_V1)]
    assert core.find_version(entry, got[-1].sha).size == len(LINK_V1)


def test_a_missing_target_names_the_far_machine(server):
    (server / "projects" / "far" / "steps" / "big.csv").unlink()
    cfg = Config(roots=["tester@fakehost:~/projects"])
    entry = [e for e in core.build(cfg) if e.name == "big.csv"][0]
    with pytest.raises(FileNotFoundError, match="fakehost"):
        core.materialize(entry, entry.latest)


# ------------------------------------------- a link pointing at a directory

PARTS = {"part-0.parquet": "a,1\n", "sub/part-1.parquet": "b,2\nc,3\n"}
"""A dataset of two parts, one of them a directory down, as parquet writes."""


@pytest.fixture()
def dataset(tmp_path):
    """A repository publishing a directory as a link, and settings for it."""
    repo = write_dataset_link_repo(tmp_path / "ds", PARTS)
    conf = tmp_path / "config.toml"
    conf.write_text('roots = []\nasset_dirs = ["results"]\n')
    return repo, str(conf)


def stamped_dataset(repo: Path, conf: str, capsys) -> Config:
    """Stamp a dataset repository, commit it, and return settings covering it."""
    assert run_in(repo, conf, "stamp") == 0
    capsys.readouterr()
    commit(repo, "stamp the dataset")
    return Config(roots=[str(repo)])


def test_stamp_writes_a_digest_over_the_whole_directory(dataset, capsys):
    repo, conf = dataset
    assert run_in(repo, conf, "stamp") == 0
    capsys.readouterr()
    got = manifest.parse(
        (repo / "results" / "crossrepo.yml").read_text(), "results"
    )
    assert got.stamp("results/big.parquet") == Stamp(
        sha256=tree_digest(PARTS),
        size=sum(len(v) for v in PARTS.values()),
        parts=len(PARTS),
    )


def test_stamp_counts_the_parts_out_loud(dataset, capsys):
    repo, conf = dataset
    run_in(repo, conf, "stamp")
    assert "in 2 parts" in capsys.readouterr().out


def test_stamp_refuses_a_directory_with_nothing_in_it(tmp_path, capsys):
    repo = write_dataset_link_repo(tmp_path / "empty", {})
    conf = tmp_path / "config.toml"
    conf.write_text('roots = []\nasset_dirs = ["results"]\n')
    assert run_in(repo, str(conf), "stamp") == 1
    assert "empty directory" in capsys.readouterr().err


def test_a_stamped_directory_is_up_to_date_on_the_next_run(dataset, capsys):
    repo, conf = dataset
    run_in(repo, conf, "stamp")
    capsys.readouterr()
    assert run_in(repo, conf, "stamp", "--check") == 0


def test_a_linked_directory_is_cataloged_as_a_dataset(dataset, capsys):
    repo, conf = dataset
    cfg = stamped_dataset(repo, conf, capsys)
    entry = {e.path: e for e in linked_entries(cfg)}["results/big.parquet"]
    assert entry.latest.parts == len(PARTS)
    assert entry.latest.size == sum(len(v) for v in PARTS.values())
    assert entry.latest.link == "../steps/out.parquet"


def test_a_linked_directory_is_assembled_on_reading(dataset, capsys):
    repo, conf = dataset
    cfg = stamped_dataset(repo, conf, capsys)
    got = core.get("ds", "big.parquet", cfg=cfg, quiet=True)
    assert got.is_dir()
    assert {
        p.relative_to(got).as_posix(): p.read_text()
        for p in got.rglob("*") if p.is_file()
    } == PARTS


def test_a_part_is_the_pipelines_own_file_and_not_a_copy(tmp_path, capsys):
    """The whole point of publishing this way: the bytes are written once.

    The content is unique to this test because the store is shared and keyed by
    content: a part another test cached first is already there under its hash,
    and this one's copy of those bytes is then rightly left where it lies.
    """
    only = {"part-0.parquet": "only,this,test,writes,these,bytes\n"}
    repo = write_dataset_link_repo(tmp_path / "inode", only)
    conf = tmp_path / "config.toml"
    conf.write_text('roots = []\nasset_dirs = ["results"]\n')
    cfg = stamped_dataset(repo, str(conf), capsys)
    got = core.get("inode", "big.parquet", cfg=cfg, quiet=True)
    wrote = repo / "steps" / "out.parquet" / "part-0.parquet"
    assert os.stat(got / "part-0.parquet").st_ino == os.stat(wrote).st_ino


def test_a_part_regenerated_without_stamping_is_caught(dataset, capsys):
    repo, conf = dataset
    cfg = stamped_dataset(repo, conf, capsys)
    (repo / "steps" / "out.parquet" / "part-0.parquet").write_text("a,9\n")
    with pytest.raises(ValueError, match="stamped with"):
        core.get("ds", "big.parquet", cfg=cfg, quiet=True, refresh=True)


def test_a_part_renamed_without_stamping_is_caught(dataset, capsys):
    """Same bytes and the same total, so only the digest over the whole says so."""
    repo, conf = dataset
    cfg = stamped_dataset(repo, conf, capsys)
    out = repo / "steps" / "out.parquet"
    (out / "part-0.parquet").rename(out / "part-2.parquet")
    with pytest.raises(ValueError, match="hashes to"):
        core.get("ds", "big.parquet", cfg=cfg, quiet=True, refresh=True)


def test_a_part_added_without_stamping_is_caught(dataset, capsys):
    repo, conf = dataset
    cfg = stamped_dataset(repo, conf, capsys)
    (repo / "steps" / "out.parquet" / "part-3.parquet").write_text("d,4\n")
    with pytest.raises(ValueError, match="not "):
        core.get("ds", "big.parquet", cfg=cfg, quiet=True, refresh=True)


def test_a_directory_the_pipeline_never_wrote_says_so(tmp_path, capsys):
    repo = write_dataset_link_repo(tmp_path / "gone", PARTS)
    conf = tmp_path / "config.toml"
    conf.write_text('roots = []\nasset_dirs = ["results"]\n')
    cfg = stamped_dataset(repo, str(conf), capsys)
    entries = linked_entries(cfg)                 # cataloged while it is there
    shutil.rmtree(repo / "steps" / "out.parquet")
    entry = {e.path: e for e in entries}["results/big.parquet"]
    with pytest.raises(FileNotFoundError, match="no such directory"):
        core.materialize(entry, entry.latest)
