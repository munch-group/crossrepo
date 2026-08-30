"""Tests for materialising content into the cache."""

import os
import resource
import subprocess
import sys

import pytest

from labdata import core
from labdata.model import Spec

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
