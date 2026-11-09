"""Synthetic tests only: never fetch or load proprietary provider content."""

import gzip
import json
from pathlib import Path

import pytest

from scripts import materialize_webfig_fixtures as fetch


def synthetic_corpus():
    blobs, assets = {}, {}
    for number in range(42):
        decoded = f"synthetic fixture {number}".encode()
        sha = fetch.digest(decoded)
        name = f"{sha}.gz"
        blobs[name] = gzip.compress(decoded, mtime=0)
        assets[f"/synthetic/{number}"] = {
            "path": name,
            "sha256": sha,
            "bytes": len(decoded),
            "compression": "gzip",
            "kind": "data",
        }
    raw = json.dumps({"assets": assets, "schemas": list(assets.values())[:9]}).encode()
    return raw, blobs


@pytest.fixture
def synthetic_checkout(tmp_path, monkeypatch):
    raw, blobs = synthetic_corpus()
    monkeypatch.setattr(fetch, "MANIFEST_SHA256", fetch.digest(raw))
    descriptor = {
        "schema": "academy.private-webfig-fixtures.v1",
        "repository": fetch.REPOSITORY,
        "commit": fetch.COMMIT,
        "manifest_sha256": fetch.digest(raw),
        "fixture_root": "webfig",
        "known_hosts_file": ".github/private-fixtures/github_known_hosts",
        "expected_asset_count": 42,
        "expected_schema_count": 9,
    }
    private = tmp_path / ".github/private-fixtures"
    private.mkdir(parents=True)
    (private / "webfig.json").write_text(json.dumps(descriptor))
    (private / "github_known_hosts").write_text("synthetic host metadata")
    destination = tmp_path / "tests/fixtures/webfig"
    destination.mkdir(parents=True)
    (destination / "manifest.json").write_bytes(raw)
    objects = {"README.md": b"synthetic metadata", "webfig/manifest.json": raw}
    objects.update({f"webfig/{name}": blob for name, blob in blobs.items()})
    ids = {name: fetch.digest(blob)[:40] for name, blob in objects.items()}
    by_id = {ids[name]: blob for name, blob in objects.items()}
    calls = []

    def fake_git(repo, env, *args, limit=fetch.LIMIT):
        calls.append((args, env.copy()))
        assert fetch.KEY_ENVIRONMENT_VARIABLE not in env
        assert Path(env["HOME"]).is_relative_to(Path("/tmp"))
        if args[0] == "rev-parse":
            return fetch.COMMIT.encode() + b"\n"
        if args[0] == "ls-tree":
            return f"040000 tree {'a' * 40}\twebfig\0".encode() + b"".join(
                f"100644 blob {oid}\t{name}\0".encode() for name, oid in ids.items()
            )
        if args[0] == "cat-file":
            value = by_id[args[2]]
            return str(len(value)).encode() if args[1] == "-s" else value
        return b""

    monkeypatch.setattr(fetch, "git", fake_git)
    return tmp_path, blobs, calls


def test_acquire_exact_synthetic_tree_and_remove_key_before_materialization(synthetic_checkout):
    root, blobs, calls = synthetic_checkout
    evidence = fetch.acquire(root, "synthetic test identity")
    assert evidence["file_count"] == evidence["asset_count"] == 42
    assert evidence["schema_count"] == 9
    assert set(evidence) == {
        "repository",
        "commit",
        "manifest_sha256",
        "asset_count",
        "schema_count",
        "file_count",
        "compressed_bytes",
        "decoded_bytes",
        "per_file_byte_limit",
    }
    assert {p.name: p.read_bytes() for p in (root / "tests/fixtures/webfig").glob("*.gz")} == blobs
    assert all(not Path(env["HOME"]).exists() for _, env in calls)
    assert not any("checkout" in args or "submodule" in args for args, _ in calls)


@pytest.mark.parametrize("secret", ["", " ", "\n"])
def test_missing_secret_blocks_before_git(synthetic_checkout, secret):
    root, _, calls = synthetic_checkout
    with pytest.raises(fetch.AcquisitionError):
        fetch.acquire(root, secret)
    assert calls == []


@pytest.mark.parametrize("mutation", ["../escape.gz", "/absolute.gz", "nested/file.gz", "wrong.gz"])
def test_manifest_rejects_unsafe_or_non_content_addressed_names(mutation):
    raw, _ = synthetic_corpus()
    manifest = json.loads(raw)
    manifest["assets"]["/synthetic/0"]["path"] = mutation
    with pytest.raises(fetch.AcquisitionError):
        fetch.manifest_entries(json.dumps(manifest).encode())


@pytest.mark.parametrize("mode", ["120000", "160000", "100755"])
def test_tree_rejects_symlinks_submodules_and_executable_modes(mode):
    with pytest.raises(fetch.AcquisitionError):
        fetch.tree_entries(f"{mode} blob {'a' * 40}\tREADME.md\0".encode(), {"README.md"})


@pytest.mark.parametrize("names", [[], ["README.md", "extra"], ["../README.md"], ["README.md", "README.md"]])
def test_tree_rejects_missing_extra_traversal_duplicate(names):
    raw = b"".join(f"100644 blob {'a' * 40}\t{name}\0".encode() for name in names)
    with pytest.raises(fetch.AcquisitionError):
        fetch.tree_entries(raw, {"README.md"})


@pytest.mark.parametrize("mutation", ["hash", "size", "compressed", "decoded"])
def test_blob_rejects_hash_size_and_both_bounds(mutation):
    decoded = b"synthetic content"
    raw = gzip.compress(decoded)
    entry = {"sha256": fetch.digest(decoded), "bytes": len(decoded)}
    if mutation == "hash":
        entry["sha256"] = "0" * 64
    elif mutation == "size":
        entry["bytes"] += 1
    elif mutation == "compressed":
        raw = b"x" * (fetch.LIMIT + 1)
    else:
        raw = gzip.compress(b"x" * (fetch.LIMIT + 1))
    with pytest.raises(fetch.AcquisitionError):
        fetch.validate_blob(raw, entry)


def test_wrong_commit_materializes_nothing(synthetic_checkout, monkeypatch):
    root, _, _ = synthetic_checkout
    original = fetch.git

    def wrong_commit(repo, env, *args, **kwargs):
        if args[0] == "rev-parse":
            return b"0" * 40
        return original(repo, env, *args, **kwargs)

    monkeypatch.setattr(fetch, "git", wrong_commit)
    with pytest.raises(fetch.AcquisitionError):
        fetch.acquire(root, "synthetic identity")
    assert not list((root / "tests/fixtures/webfig").glob("*.gz"))


def test_public_manifest_hash_mismatch_blocks_before_git(synthetic_checkout):
    root, _, calls = synthetic_checkout
    (root / "tests/fixtures/webfig/manifest.json").write_bytes(b"{}")
    with pytest.raises(fetch.AcquisitionError):
        fetch.acquire(root, "synthetic identity")
    assert calls == []


def test_main_redacts_all_exception_content(monkeypatch, capsys, tmp_path):
    marker = "synthetic-sensitive-provider-and-key-marker"
    monkeypatch.setenv(fetch.KEY_ENVIRONMENT_VARIABLE, marker)
    monkeypatch.setattr(fetch, "ROOT", tmp_path)

    def failed_acquire(root, secret):
        assert secret == marker
        raise RuntimeError(marker)

    monkeypatch.setattr(fetch, "acquire", failed_acquire)
    assert fetch.main() == 1
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err
    assert "qualification is blocked" in captured.err
    assert not (tmp_path / "artifacts/fixture-evidence.json").exists()


def test_private_manifest_mismatch_never_materializes(synthetic_checkout, monkeypatch):
    root, _, _ = synthetic_checkout
    original = fetch.git
    manifest_id = fetch.digest((root / "tests/fixtures/webfig/manifest.json").read_bytes())[:40]

    def corrupted_manifest(repo, env, *args, **kwargs):
        if args[0] == "cat-file" and args[2] == manifest_id:
            return b"2" if args[1] == "-s" else b"{}"
        return original(repo, env, *args, **kwargs)

    monkeypatch.setattr(fetch, "git", corrupted_manifest)
    with pytest.raises(fetch.AcquisitionError):
        fetch.acquire(root, "synthetic identity")
    assert not list((root / "tests/fixtures/webfig").glob("*.gz"))


def test_invalid_last_blob_materializes_nothing_and_cleans_temporary_identity(synthetic_checkout, monkeypatch):
    root, blobs, calls = synthetic_checkout
    original = fetch.git
    last_blob = list(blobs.values())[-1]
    last_id = fetch.digest(last_blob)[:40]

    def corrupted_blob(repo, env, *args, **kwargs):
        raw = original(repo, env, *args, **kwargs)
        if args[:2] == ("cat-file", "blob") and args[2] == last_id:
            return b"x" * len(raw)
        return raw

    monkeypatch.setattr(fetch, "git", corrupted_blob)
    with pytest.raises((fetch.AcquisitionError, gzip.BadGzipFile)):
        fetch.acquire(root, "synthetic identity")
    assert not list((root / "tests/fixtures/webfig").glob("*.gz"))
    assert all(not Path(env["HOME"]).exists() for _, env in calls)


def test_destination_symlink_is_rejected(synthetic_checkout, tmp_path):
    root, _, _ = synthetic_checkout
    destination = root / "tests/fixtures/webfig"
    replacement = tmp_path / "other"
    destination.rename(replacement)
    destination.symlink_to(replacement, target_is_directory=True)
    with pytest.raises(fetch.AcquisitionError):
        fetch.acquire(root, "synthetic identity")
    assert not list(replacement.glob("*.gz"))
