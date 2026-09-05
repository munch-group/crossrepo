"""Tests for materialising content into the cache."""

import os
import resource
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from crossrepo import cache, core
from crossrepo.model import Spec

from fixtures import ENV, commit


def test_fetch_latest_and_historical(by_spec):
    e = by_spec["acme/sweep-scan:results/candidates.csv"]
    hist = core.versions(e)
    newest = core.materialize(e, hist[0]).read_text()
    oldest = core.materialize(e, hist[-1]).read_text()
    assert newest.count("\n") == 4
    assert oldest.count("\n") == 2
    assert "C,3" in newest and "C,3" not in oldest


def test_cache_dedups_identical_content_across_repos(by_spec):
    a = by_spec["acme/sweep-scan:results/stable.csv"]
    b = by_spec["other-org/hic-borders:Results/stable.csv"]
    assert a.latest.blob == b.latest.blob            # same git blob sha
    pa = core.materialize(a, a.latest)
    pb = core.materialize(b, b.latest)
    assert pa != pb                                  # distinct readable paths
    assert pa.stat().st_ino == pb.stat().st_ino      # one copy on disk


def test_lfs_without_local_object_gives_actionable_error(by_spec):
    big = by_spec["other/big-thing:results/big.h5"]
    with pytest.raises(FileNotFoundError, match="lfs fetch"):
        core.materialize(big, big.latest)


def test_large_blob_is_streamed_not_buffered(repos, cfg):
    """A big result file must never be read into memory in one piece."""
    repo = repos / "acme" / "sweep-scan"
    big = repo / "results" / "big.csv"
    big.write_text("col\n" + "0123456789\n" * 2_000_000)   # about 22 MB
    commit(repo, "big")

    fresh = core.build(cfg)
    e = core.resolve_one(fresh, Spec.parse("sweep-scan:big.csv"))

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    path = core.materialize(e, e.latest)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert path.stat().st_size == big.stat().st_size
    assert path.read_bytes()[:4] == b"col\n"
    # ru_maxrss is bytes on macOS and kilobytes on linux
    unit = 1 if sys.platform == "darwin" else 1024
    assert (after - before) * unit < 8 * 1024 * 1024


def test_same_name_in_two_dirs_gets_its_own_bytes(by_spec):
    """One version key, two files: the cache must not serve one for the other."""
    top = by_spec["acme/sweep-scan:results/stable.csv"]
    sub = by_spec["acme/sweep-scan:results/sub/stable.csv"]
    assert top.latest.sha == sub.latest.sha      # both last changed in one commit
    assert top.latest.blob != sub.latest.blob        # but they are different files

    p_top = core.materialize(top, top.latest)
    p_sub = core.materialize(sub, sub.latest)
    assert p_top != p_sub
    assert p_top.read_text() == "k,v\nx,1\n"
    assert p_sub.read_text() == "k,v\ny,2\n"


def test_a_readable_link_left_by_an_older_layout_is_replaced(by_spec):
    """A link that does not stand for the wanted blob is rebuilt, not trusted."""
    from crossrepo import cache

    sub = by_spec["acme/sweep-scan:results/sub/stable.csv"]
    dest = cache.readable_path(sub.repo_key, sub.latest.sha, sub.path)
    core.materialize(sub, sub.latest)

    dest.unlink()
    dest.write_text("stale bytes from an older cache\n")   # same name, wrong content
    assert core.materialize(sub, sub.latest).read_text() == "k,v\ny,2\n"


def test_each_writer_gets_its_own_temporary_path():
    """Two writers agree on the object but must not share a scratch file."""
    a_tmp, a_final = cache.open_for_write("f" * 40)
    b_tmp, b_final = cache.open_for_write("f" * 40)
    assert a_final == b_final        # one object, keyed by content
    assert a_tmp != b_tmp            # but one scratch file each


def test_concurrent_fetches_do_not_publish_a_corrupt_object(repos, cfg):
    """Fetches racing on one blob must not write over each other."""
    repo = repos / "acme" / "sweep-scan"
    racy = repo / "results" / "racy.csv"
    racy.write_text("col\n" + "0123456789\n" * 500_000)      # about 5.5 MB
    commit(repo, "racy")

    entry = core.resolve_one(core.build(cfg), Spec.parse("sweep-scan:racy.csv"))
    expected = racy.read_bytes()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(core.materialize, entry, entry.latest) for _ in range(8)]
        paths = [f.result() for f in futures]

    assert len({str(p) for p in paths}) == 1        # all agree on the path
    for p in paths:
        assert p.read_bytes() == expected           # and it holds the whole file
    blob = cache.blob_path(entry.latest.blob)
    assert blob.read_bytes() == expected
    assert not list(blob.parent.glob("*.tmp"))      # no scratch files left behind
