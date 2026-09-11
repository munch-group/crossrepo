"""Tests for cataloguing GitHub repositories without cloning them.

The API is stubbed rather than called: the suite must not need a network or a
token. What is not stubbed is the logic under test — manifests, datasets, LFS
pointers and version resolution all run for real against canned responses.
"""

import pytest

from crossrepo import remote
from crossrepo.config import Config, SourceWarning
from crossrepo.github import GitHubError, blob_url

MANIFEST = b"files:\n  hits.csv: Association hits\n  big.h5: Kept in LFS\n"
POINTER = (
    b"version https://git-lfs.github.com/spec/v1\n"
    b"oid sha256:" + b"ab" * 32 + b"\nsize 512189753\n"
)


class FakeClient:
    """A GitHub client that answers from canned data and counts its calls."""

    def __init__(self, tree, commits, blobs, files_by_commit=None, head=None,
                 missing=()):
        self._tree = tree
        self._commits = commits
        self._blobs = blobs
        self._files = files_by_commit or {}
        self._head = head or (commits[0] if commits else commit("0" * 40, "head"))
        self._missing = set(missing)
        self.calls = []

    def commit(self, owner, repo, ref):
        self.calls.append("commit")
        return self._head

    def default_branch(self, owner, repo):
        self.calls.append("default_branch")
        return "main"

    def repositories(self, owner):
        self.calls.append("repositories")
        return [("demo", "main")]

    def tree(self, owner, repo, ref):
        self.calls.append("tree")
        return self._tree

    def read_blob(self, owner, repo, sha):
        self.calls.append(f"blob:{sha}")
        return self._blobs[sha]

    def commits(self, owner, repo, path, limit=100):
        self.calls.append(f"commits:{path}")
        return self._commits

    def get(self, endpoint, **params):
        self.calls.append(endpoint)
        if "/commits/" in endpoint:
            sha = endpoint.rsplit("/", 1)[-1]
            return {"files": [{"filename": f} for f in self._files.get(sha, [])]}
        if endpoint.startswith("repos/"):          # does this repository exist
            slug = endpoint[len("repos/"):]
            if slug in self._missing:
                raise GitHubError(f"GitHub returned 404 for {endpoint}.")
            return {"full_name": slug}
        raise AssertionError(f"unexpected call {endpoint}")


def commit(sha, subject, date="2026-01-01T00:00:00Z"):
    return {"sha": sha, "commit": {"committer": {"date": date}, "message": subject}}


def test_a_repo_without_a_manifest_costs_one_tree_call():
    client = FakeClient(
        tree=[{"path": "results/hits.csv", "type": "blob", "sha": "aaa", "size": 10}],
        commits=[], blobs={},
    )
    assert remote.scan_repo(client, "o", "r", Config(), "main") == []
    assert client.calls == ["tree"]          # nothing else was asked for


def test_published_files_carry_their_description_and_version():
    client = FakeClient(
        tree=[
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 60},
            {"path": "results/hits.csv", "type": "blob", "sha": "aaa", "size": 10},
            {"path": "results/scratch.csv", "type": "blob", "sha": "bbb", "size": 10},
        ],
        commits=[commit("c" * 40, "add hits")],
        blobs={"man": MANIFEST},
        files_by_commit={"c" * 40: ["results/hits.csv"]},
    )
    entries = remote.scan_repo(client, "munch-group", "demo", Config(), "main")
    assert [e.path for e in entries] == ["results/hits.csv"]   # scratch is unpublished
    entry = entries[0]
    assert entry.description == "Association hits"
    assert entry.latest.sha == "c" * 40
    assert entry.source == "github"
    assert entry.remote == "munch-group/demo"
    assert str(entry.root) in ("", ".")                        # nothing was cloned


def test_the_url_names_the_commit_not_the_branch():
    client = FakeClient(
        tree=[
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 60},
            {"path": "results/hits.csv", "type": "blob", "sha": "aaa", "size": 10},
        ],
        commits=[commit("d" * 40, "add hits")],
        blobs={"man": MANIFEST},
        files_by_commit={"d" * 40: ["results/hits.csv"]},
    )
    entry = remote.scan_repo(client, "munch-group", "demo", Config(), "main")[0]
    assert entry.url() == blob_url(
        "munch-group", "demo", "d" * 40, "results/hits.csv"
    )
    assert "d" * 40 in entry.url()
    assert "/main/" not in entry.url()


def test_an_lfs_pointer_reports_the_real_size():
    client = FakeClient(
        tree=[
            {"path": ".gitattributes", "type": "blob", "sha": "att", "size": 40},
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 60},
            {"path": "results/big.h5", "type": "blob", "sha": "ptr", "size": 130},
        ],
        commits=[commit("e" * 40, "add big")],
        blobs={
            "att": b"results/*.h5 filter=lfs diff=lfs merge=lfs -text\n",
            "man": MANIFEST,
            "ptr": POINTER,
        },
        files_by_commit={"e" * 40: ["results/big.h5"]},
    )
    entry = remote.scan_repo(client, "o", "demo", Config(), "main")[0]
    assert entry.latest.size == 512189753
    assert entry.latest.lfs_oid == "ab" * 32


def test_small_files_are_not_probed_when_nothing_is_in_lfs():
    """Reading every small blob to look for a pointer would cost a request each."""
    client = FakeClient(
        tree=[
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 60},
            {"path": "results/hits.csv", "type": "blob", "sha": "aaa", "size": 10},
        ],
        commits=[commit("f" * 40, "add hits")],
        blobs={"man": MANIFEST},
        files_by_commit={"f" * 40: ["results/hits.csv"]},
    )
    remote.scan_repo(client, "o", "demo", Config(), "main")
    assert client.calls.count("blob:aaa") == 0       # only the manifest was read


def test_a_directory_named_in_the_manifest_is_one_dataset():
    manifest = b"files:\n  table.parquet: A partitioned table\n"
    client = FakeClient(
        tree=[
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 40},
            {"path": "results/table.parquet", "type": "tree", "sha": "tre"},
            {"path": "results/table.parquet/p0.parquet", "type": "blob",
             "sha": "p0", "size": 100},
            {"path": "results/table.parquet/p1.parquet", "type": "blob",
             "sha": "p1", "size": 150},
        ],
        commits=[commit("a" * 40, "repartition")],
        blobs={"man": manifest},
        files_by_commit={"a" * 40: ["results/table.parquet/p1.parquet"]},
    )
    entries = remote.scan_repo(client, "o", "demo", Config(), "main")
    assert [e.path for e in entries] == ["results/table.parquet"]
    entry = entries[0]
    assert entry.latest.parts == 2
    assert entry.latest.size == 250
    assert entry.latest.blob == "tre"                # the tree sha keys the dataset
    assert "/tree/" in entry.url()                   # a directory has a page, not a file


def test_build_needs_no_configuration_to_do_nothing():
    assert remote.build(Config()) == []              # and makes no request


def test_build_enumerates_owners():
    client = FakeClient(
        tree=[
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 60},
            {"path": "results/hits.csv", "type": "blob", "sha": "aaa", "size": 10},
        ],
        commits=[commit("b" * 40, "add hits")],
        blobs={"man": MANIFEST},
        files_by_commit={"b" * 40: ["results/hits.csv"]},
    )
    entries = remote.build(Config(owners=["munch-group"]), client=client)
    assert [e.repo_key for e in entries] == ["munch-group/demo"]


def test_a_manifest_deeper_in_the_tree_is_ignored_remotely():
    client = FakeClient(
        tree=[
            {"path": "results/sub/crossrepo.yml", "type": "blob", "sha": "man",
             "size": 40},
            {"path": "results/sub/x.csv", "type": "blob", "sha": "aaa", "size": 10},
        ],
        commits=[], blobs={"man": b"files:\n  x.csv: too deep\n"},
    )
    assert remote.scan_repo(client, "o", "r", Config(), "main") == []
    assert client.calls == ["tree"]        # the deep manifest was not even read


def test_cataloguing_costs_the_same_however_many_files_there_are():
    """Two requests per repo, plus the manifest: flat in the number of files."""
    tree = [{"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 60}]
    tree += [
        {"path": f"results/part-{i}.csv", "type": "blob", "sha": f"s{i}", "size": 10}
        for i in range(300)
    ]
    client = FakeClient(
        tree=tree,
        commits=[commit("9" * 40, "bulk")],
        blobs={"man": b'files:\n  "*.csv": one of many\n'},
    )
    entries = remote.scan_repo(client, "o", "demo", Config(), "main")
    assert len(entries) == 300
    assert client.calls == ["tree", "blob:man", "commit"]


def test_every_file_carries_the_repository_head():
    client = FakeClient(
        tree=[
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 60},
            {"path": "results/hits.csv", "type": "blob", "sha": "aaa", "size": 10},
            {"path": "results/big.h5", "type": "blob", "sha": "bbb", "size": 10},
        ],
        commits=[],
        blobs={"man": MANIFEST},
        head=commit("7" * 40, "the tip"),
    )
    entries = remote.scan_repo(client, "o", "demo", Config(), "main")
    assert {e.latest.sha for e in entries} == {"7" * 40}
    assert {e.latest.subject for e in entries} == {"the tip"}


def test_gitattributes_is_not_read_when_nothing_could_be_a_pointer():
    """A repo whose published files are all large needs no LFS lookup at all."""
    client = FakeClient(
        tree=[
            {"path": ".gitattributes", "type": "blob", "sha": "att", "size": 40},
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 60},
            {"path": "results/hits.csv", "type": "blob", "sha": "aaa", "size": 5_000_000},
        ],
        commits=[commit("1" * 40, "big only")],
        blobs={"man": MANIFEST, "att": b"*.h5 filter=lfs\n"},
    )
    remote.scan_repo(client, "o", "demo", Config(), "main")
    assert "blob:att" not in client.calls


def test_the_default_branch_costs_no_request():
    client = FakeClient(
        tree=[
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 60},
            {"path": "results/hits.csv", "type": "blob", "sha": "aaa", "size": 10},
        ],
        commits=[commit("2" * 40, "tip")],
        blobs={"man": MANIFEST},
    )
    remote.scan_repo(client, "o", "demo", Config())      # no ref given
    assert "default_branch" not in client.calls


def test_a_nested_results_dir_is_read_over_the_api_too():
    """asset_dirs are paths from the repo root, however deep, on both backends."""
    deep = "some_dir/some_sub_dir/some_sub_sub_dir"
    client = FakeClient(
        tree=[
            {"path": f"{deep}/crossrepo.yml", "type": "blob", "sha": "man", "size": 40},
            {"path": f"{deep}/deep.csv", "type": "blob", "sha": "aaa", "size": 10},
            {"path": "some_dir/elsewhere.csv", "type": "blob", "sha": "bbb", "size": 10},
        ],
        commits=[commit("3" * 40, "publish")],
        blobs={"man": b"files:\n  deep.csv: a buried result\n"},
    )
    entries = remote.scan_repo(client, "o", "demo", Config(asset_dirs=[deep]), "main")
    assert [e.path for e in entries] == [f"{deep}/deep.csv"]
    assert entries[0].description == "a buried result"


def test_a_blob_sharing_only_a_first_component_is_not_under_results():
    """`some_dir/x.csv` must not count as being under `some_dir/results`."""
    client = FakeClient(
        tree=[{"path": "some_dir/x.csv", "type": "blob", "sha": "aaa", "size": 10}],
        commits=[], blobs={},
    )
    assert remote.scan_repo(
        client, "o", "demo", Config(asset_dirs=["some_dir/results"]), "main"
    ) == []


def test_a_key_from_the_root_is_read_over_the_api_too():
    """A manifest publishes files outside its directory on both backends."""
    client = FakeClient(
        tree=[
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 60},
            {"path": "results/hits.csv", "type": "blob", "sha": "aaa", "size": 10},
            {"path": "data/reference/samples.csv", "type": "blob",
             "sha": "bbb", "size": 20},
            {"path": "data/reference/private.csv", "type": "blob",
             "sha": "ccc", "size": 20},
            {"path": "work/scratch.csv", "type": "blob", "sha": "ddd", "size": 20},
        ],
        commits=[commit("4" * 40, "publish")],
        blobs={"man": b"files:\n"
                      b"  hits.csv: Association hits\n"
                      b"  /data/reference/samples.csv: Kept beside the raw data\n"},
    )
    entries = remote.scan_repo(client, "o", "demo", Config(), "main")
    assert sorted(e.path for e in entries) == [
        "data/reference/samples.csv",
        "results/hits.csv",
    ]
    assert {e.description for e in entries} == {
        "Association hits", "Kept beside the raw data",
    }


def test_the_tree_is_read_once_however_far_a_manifest_reaches():
    """Files outside the results directory cost no extra request."""
    client = FakeClient(
        tree=[
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 60},
            {"path": "data/x.csv", "type": "blob", "sha": "aaa", "size": 10},
        ],
        commits=[commit("5" * 40, "publish")],
        blobs={"man": b'files:\n  "/data/*.csv": from the root\n'},
    )
    entries = remote.scan_repo(client, "o", "demo", Config(), "main")
    assert [e.path for e in entries] == ["data/x.csv"]
    assert client.calls.count("tree") == 1


# --------------------------------- sources the settings name but GitHub has not

def test_a_repo_named_in_the_settings_that_is_not_there_is_reported():
    """An empty answer from a repository that does not exist must not pass for
    a repository that publishes nothing."""
    client = FakeClient(tree=[], commits=[], blobs={}, missing={"o/nope"})
    with pytest.warns(SourceWarning, match="o/nope: GitHub returned 404"):
        assert remote.build(Config(repos=["o/nope"]), client) == []
    assert "repos/o/nope" in client.calls


def test_a_repo_that_merely_publishes_nothing_is_not_reported(recwarn):
    client = FakeClient(
        tree=[{"path": "src/main.py", "type": "blob", "sha": "aaa", "size": 10}],
        commits=[], blobs={},
    )
    assert remote.build(Config(repos=["o/quiet"]), client) == []
    assert not [w for w in recwarn if issubclass(w.category, SourceWarning)]
    assert client.calls.count("repos/o/quiet") == 1      # one extra request, once


def test_a_repo_that_publishes_is_never_asked_about_twice():
    client = FakeClient(
        tree=[
            {"path": "results/crossrepo.yml", "type": "blob", "sha": "man", "size": 60},
            {"path": "results/hits.csv", "type": "blob", "sha": "aaa", "size": 10},
        ],
        commits=[commit("6" * 40, "publish")],
        blobs={"man": MANIFEST},
    )
    assert remote.build(Config(repos=["o/demo"]), client)
    assert "repos/o/demo" not in client.calls           # it plainly exists


def test_an_owner_that_cannot_be_listed_is_reported():
    class Refusing(FakeClient):
        def repositories(self, owner):
            raise GitHubError(f"GitHub returned 404 for users/{owner}.")

    client = Refusing(tree=[], commits=[], blobs={})
    with pytest.warns(SourceWarning, match="nosuchorg: GitHub returned 404"):
        assert remote.build(Config(owners=["nosuchorg"]), client) == []


def test_a_repo_written_without_an_owner_is_reported():
    client = FakeClient(tree=[], commits=[], blobs={})
    with pytest.warns(SourceWarning, match="should be written owner/repo"):
        assert remote.build(Config(repos=["justaname"]), client) == []
