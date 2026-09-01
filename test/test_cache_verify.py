"""Tests for checking that the cache still holds what it claims."""

from labdata import cache, cli, core


def test_a_sound_cache_verifies_clean(by_spec):
    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    core.materialize(entry, entry.latest)
    stored = cache.objects()
    assert stored
    assert [p for p in stored if cache.check_object(p)] == []


def test_corruption_is_detected(by_spec):
    entry = by_spec["acme/sweep-scan:results/stable.csv"]
    core.materialize(entry, entry.latest)
    blob = cache.blob_path(entry.latest.blob)
    assert cache.check_object(blob) is None
    original = blob.read_bytes()
    try:
        blob.write_bytes(b"X" * len(original))          # same size, wrong bytes
        assert cache.check_object(blob) == "content does not hash to its key"
    finally:
        blob.write_bytes(original)


def test_an_object_that_is_not_named_for_a_hash_is_reported(tmp_path):
    odd = tmp_path / "not-a-hash"
    odd.write_bytes(b"x")
    assert cache.check_object(odd) == "not named for a content hash"
    short = tmp_path / "abcdef"
    short.write_bytes(b"x")
    assert cache.check_object(short) == "not named for a git blob id or a Git LFS object id"


def test_repairing_removes_the_object_and_its_readable_links(by_spec):
    """A same-size corruption must not survive as a readable hard link."""
    entry = by_spec["acme/sweep-scan:results/sub/stable.csv"]
    readable = core.materialize(entry, entry.latest)
    good = readable.read_bytes()
    blob = cache.blob_path(entry.latest.blob)

    blob.write_bytes(b"X" * len(good))       # writes through the hard link too
    assert readable.read_bytes() != good
    assert cache.check_object(blob) == "content does not hash to its key"

    removed = cache.discard(blob)
    assert blob not in cache.objects()
    assert readable in removed               # the link went too, not just the object

    again = core.materialize(entry, entry.latest)
    assert again.read_bytes() == good        # self-healed on the next fetch
    assert cache.check_object(cache.blob_path(entry.latest.blob)) is None


def test_cache_command_summarises(by_spec, capsys):
    entry = by_spec["acme/sweep-scan:results/stable.csv"]
    core.materialize(entry, entry.latest)
    assert cli.main(["cache"]) == 0
    assert "objects" in capsys.readouterr().out


def test_cache_verify_passes_on_a_sound_cache(by_spec, capsys):
    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    core.materialize(entry, entry.latest)
    assert cli.main(["cache", "--verify"]) == 0
    assert "all sound" in capsys.readouterr().out


def test_cache_verify_fails_and_repair_fixes_it(by_spec, capsys):
    entry = by_spec["acme/sweep-scan:results/candidates.csv"]
    readable = core.materialize(entry, entry.latest)
    good = readable.read_bytes()
    blob = cache.blob_path(entry.latest.blob)
    blob.write_bytes(b"X" * len(good))

    assert cli.main(["cache", "--verify"]) == 1
    err = capsys.readouterr().err
    assert "do not hold what their key promises" in err
    assert "--repair" in err

    assert cli.main(["cache", "--repair"]) == 0
    assert "removed 1 objects" in capsys.readouterr().out
    assert core.materialize(entry, entry.latest).read_bytes() == good
