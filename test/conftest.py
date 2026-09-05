"""Shared fixtures for the crossrepo tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crossrepo.config import Config
from crossrepo.core import build

from fixtures import make


@pytest.fixture(scope="module")
def repos(tmp_path_factory):
    """Fixture repositories, with the cache pointed at a temporary directory."""
    base = make(tmp_path_factory.mktemp("repos") / "fx")
    os.environ["CROSSREPO_CACHE"] = str(tmp_path_factory.mktemp("cache"))
    return base


@pytest.fixture(scope="module")
def cfg(repos):
    """Settings covering the fixture repositories.

    The fixtures group repositories by owner, so each owner directory is a root:
    a root is either a repository or a directory holding them.
    """
    return Config(roots=[str(repos / "acme"), str(repos / "other")])


@pytest.fixture(scope="module")
def entries(cfg):
    """The catalog built from the fixture repositories."""
    return build(cfg)


@pytest.fixture(scope="module")
def by_spec(entries):
    """The catalog keyed by ``owner/repo:path``."""
    return {f"{e.repo_key}:{e.path}": e for e in entries}
