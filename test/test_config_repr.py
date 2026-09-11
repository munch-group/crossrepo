"""Tests that a Config shows itself the way pprint would."""

import random
import string
from dataclasses import dataclass, fields, make_dataclass
from pprint import pformat

from crossrepo.config import Config


def reference(cfg: Config) -> str:
    """What pprint makes of the same settings, via a plain clone of the class.

    The clone keeps the generated repr, which is what pprint recognises: a class
    with a repr of its own is printed by calling it, so pprint applied to
    `Config` can no longer be the yardstick for `Config`.
    """
    plain = make_dataclass("Config", [(f.name, f.type) for f in fields(Config)])
    return pformat(plain(**{f.name: getattr(cfg, f.name) for f in fields(Config)}))


def configs():
    """Settings to show, from the plain ones to a few that need wrapping."""
    yield Config()
    yield Config(repos=["munch-group/relate1Kgenomes", "munch-group/atlas-variant-ages"])
    yield Config(
        roots=["~/projects", "kmt@login.genome.au.dk:xy-drive/people/kmt"],
        owners=["munch-group", "erikfogh"],
        min_bytes=1024,
        max_bytes=10**9,
    )
    yield Config(asset_dirs=["results", "steps/data", "analysis/step3/results"])
    yield Config(roots=["a" * 200])
    yield Config(include=["*.csv"] * 40)
    rand = random.Random(0)
    for _ in range(50):
        def words(n, w):
            alphabet = string.ascii_lowercase + "/.*-"
            return [
                "".join(rand.choices(alphabet, k=rand.randint(1, w)))
                for _ in range(rand.randint(0, n))
            ]
        yield Config(
            roots=words(4, 30), owners=words(3, 12), repos=words(5, 25),
            asset_dirs=words(4, 20), include=words(25, 10), exclude=words(25, 10),
            min_bytes=rand.randint(0, 10**12), max_bytes=rand.randint(0, 10**12),
        )


def test_it_is_what_pprint_would_have_printed():
    for cfg in configs():
        assert repr(cfg) == reference(cfg)


def test_pprint_still_agrees_now_that_there_is_a_repr():
    for cfg in configs():
        assert pformat(cfg) == repr(cfg)


def test_one_setting_to_a_line_within_eighty_columns():
    cfg = Config(
        owners=["munch-group"],
        asset_dirs=["results", "data"],
        include=["*.csv", "*.tsv", "*.parquet", "*.h5", "*.hdf5", "*.json", "*.zarr"],
    )
    lines = repr(cfg).splitlines()
    assert len(lines) >= len(fields(Config))              # one setting to a line
    assert max(len(line) for line in lines) <= 80
    assert lines[0] == "Config(roots=[],"
    assert lines[1] == "       owners=['munch-group'],"   # aligned under the first
    assert lines[-1] == "       max_bytes=0)"


def test_a_short_enough_object_stays_on_one_line():
    @dataclass
    class Tiny:
        a: int = 1
        b: str = "x"

        __repr__ = Config.__repr__

    assert repr(Tiny()) == "Tiny(a=1, b='x')" == pformat(Tiny())


def test_it_still_says_how_to_build_the_same_settings():
    for cfg in configs():
        assert eval(repr(cfg)) == cfg
