"""Acquire pinned private compatibility bytes without executing vendor content.

Only the acquisition step receives the read-only deploy key. Git has no checkout,
user configuration, hooks, filters, submodules, or credential persistence.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 2 * 1024 * 1024
REPOSITORY = "michaelayoade/dotmac_academy_console_fixtures"
COMMIT = "013278c9398be5023e67a07ced081fc8c6367ba4"
MANIFEST_SHA256 = "c197c982d9f78fe59bd4930f63095f34caa622642b00e96d7dc0dc27e6003d7b"
KEY_ENVIRONMENT_VARIABLE = "ACADEMY_WEBFIG_FIXTURE_SSH_KEY"


class AcquisitionError(Exception):
    """Deliberately content-free acquisition failure."""


def require(condition: bool) -> None:
    if not condition:
        raise AcquisitionError("private fixture verification failed")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_entries(raw: bytes, asset_count: int = 42, schema_count: int = 9) -> dict[str, dict]:
    manifest = json.loads(raw)
    assets, schemas = manifest["assets"], manifest["schemas"]
    require(isinstance(assets, dict) and isinstance(schemas, list))
    require(len(assets) == asset_count and len(schemas) == schema_count)
    result = {}
    for entry in [*assets.values(), *schemas]:
        sha, path, size = entry["sha256"], entry["path"], entry["bytes"]
        require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) is not None)
        require(path == f"{sha}.gz" and entry["compression"] == "gzip")
        require(type(size) is int and 0 < size <= LIMIT)
        require(path not in result or result[path] == entry)
        result[path] = entry
    require(len(result) == asset_count)
    return result


def validate_blob(raw: bytes, entry: dict) -> None:
    require(0 < len(raw) <= LIMIT)
    with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as stream:
        decoded = stream.read(LIMIT + 1)
    require(len(decoded) <= LIMIT and len(decoded) == entry["bytes"])
    require(digest(decoded) == entry["sha256"])


def git(repo: Path, env: dict[str, str], *args: str, limit: int = LIMIT) -> bytes:
    """Bound captured stdout; never expose Git diagnostics or remote content."""
    with subprocess.Popen(  # noqa: S603 - fixed executable, argument vector, isolated environment
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never", "-C", str(repo), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        timer = threading.Timer(180, process.kill)
        timer.start()
        try:
            assert process.stdout is not None
            output = process.stdout.read(limit + 1)
            if len(output) > limit:
                process.kill()
                raise AcquisitionError("private fixture command exceeded bound")
            require(process.wait(timeout=180) == 0)
            return output
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()


def tree_entries(raw: bytes, expected: set[str]) -> dict[str, str]:
    result = {}
    directories = set()
    expected_directories = {"webfig"} if any(name.startswith("webfig/") for name in expected) else set()
    for line in raw.split(b"\0"):
        if not line:
            continue
        metadata, name = line.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split(" ")
        path = name.decode("ascii")
        if mode == "040000" and kind == "tree":
            require(path in expected_directories and path not in directories)
            directories.add(path)
            continue
        require(mode == "100644" and kind == "blob")
        require(path in expected and path not in result)
        require(re.fullmatch(r"[0-9a-f]{40}", oid) is not None)
        result[path] = oid
    require(set(result) == expected and directories == expected_directories)
    return result


def read_blob(repo: Path, env: dict[str, str], oid: str) -> bytes:
    size = int(git(repo, env, "cat-file", "-s", oid, limit=32))
    require(0 < size <= LIMIT)
    raw = git(repo, env, "cat-file", "blob", oid)
    require(len(raw) == size)
    return raw


def acquire(root: Path, secret: str) -> dict:
    require(bool(secret.strip()))
    descriptor = json.loads((root / ".github/private-fixtures/webfig.json").read_bytes())
    require(
        descriptor
        == {
            "schema": "academy.private-webfig-fixtures.v1",
            "repository": REPOSITORY,
            "commit": COMMIT,
            "manifest_sha256": MANIFEST_SHA256,
            "fixture_root": "webfig",
            "known_hosts_file": ".github/private-fixtures/github_known_hosts",
            "expected_asset_count": 42,
            "expected_schema_count": 9,
        }
    )
    public = (root / "tests/fixtures/webfig/manifest.json").read_bytes()
    require(digest(public) == MANIFEST_SHA256)
    entries = manifest_entries(public)
    expected = {"README.md", "webfig/manifest.json", *(f"webfig/{name}" for name in entries)}
    # Explicit /tmp keeps key and object database outside the checkout, even if TMPDIR is changed.
    with tempfile.TemporaryDirectory(prefix="academy-fixtures-", dir="/tmp") as directory:
        work = Path(directory)
        key = work / "identity"
        key.touch(mode=0o600)
        key.write_text(secret.rstrip() + "\n")
        host = work / "known_hosts"
        host.write_bytes((root / descriptor["known_hosts_file"]).read_bytes())
        ssh = shlex.join(
            [
                "/usr/bin/ssh",
                "-F",
                "/dev/null",
                "-i",
                str(key),
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                "-o",
                "ForwardAgent=no",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={host}",
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-o",
                "HostKeyAlgorithms=ssh-ed25519",
                "-o",
                "ConnectTimeout=30",
            ]
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(work),
            "XDG_CONFIG_HOME": str(work),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": ssh,
            "GIT_SSH_VARIANT": "ssh",
            "GIT_ALLOW_PROTOCOL": "ssh",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "LC_ALL": "C",
        }
        repo = work / "repo.git"
        repo.mkdir()
        git(repo, env, "init", "--bare", "--template=")
        git(
            repo,
            env,
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--depth=1",
            f"git@github.com:{REPOSITORY}.git",
            COMMIT,
        )
        require(git(repo, env, "rev-parse", "FETCH_HEAD").decode("ascii").strip() == COMMIT)
        tree = tree_entries(git(repo, env, "ls-tree", "-rtz", "--full-tree", COMMIT), expected)
        require(digest(read_blob(repo, env, tree["webfig/manifest.json"])) == MANIFEST_SHA256)
        read_blob(repo, env, tree["README.md"])
        blobs = {}
        for name, entry in entries.items():
            raw = read_blob(repo, env, tree[f"webfig/{name}"])
            validate_blob(raw, entry)
            blobs[name] = raw
    # Temporary key and Git database are already deleted before any materialization.
    destination = root / "tests/fixtures/webfig"
    require(not destination.is_symlink() and destination.resolve() == root.resolve() / "tests/fixtures/webfig")
    require(not any(destination.glob("*.gz")))
    for name, raw in blobs.items():
        with (destination / name).open("xb") as output:
            output.write(raw)
    return {
        "repository": REPOSITORY,
        "commit": COMMIT,
        "manifest_sha256": MANIFEST_SHA256,
        "asset_count": 42,
        "schema_count": 9,
        "file_count": len(blobs),
        "compressed_bytes": sum(map(len, blobs.values())),
        "decoded_bytes": sum(entry["bytes"] for entry in entries.values()),
        "per_file_byte_limit": LIMIT,
    }


def main() -> int:
    secret = os.environ.pop(KEY_ENVIRONMENT_VARIABLE, "")
    try:
        evidence = acquire(ROOT, secret)
        evidence_path = ROOT / "artifacts/fixture-evidence.json"
        evidence_path.parent.mkdir(exist_ok=True)
        evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")
    except Exception:
        print("Private fixture acquisition failed; qualification is blocked.", file=sys.stderr)
        return 1
    print("Private fixture acquisition verified: 42 assets, 9 schemas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
