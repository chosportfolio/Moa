#!/usr/bin/env python3
"""Restore the exact public Maven artifacts used by MOA's manual Android build.

Uses only Python's standard library. No access token or repository credentials
are needed. Artifact bytes and extracted classes.jar files are SHA-256 checked
against android-dependencies.json. This reproduces the existing build inputs;
it is not a general Gradle transitive-dependency resolver.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
LOCK = Path(__file__).with_name("android-dependencies.json")
ALLOWED_HOSTS = {
    "nexus-playground.pleos.ai", "repo.maven.apache.org", "dl.google.com",
}
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment):
        raise ValueError("Only the pinned public Maven repository URLs are allowed")
    return url


class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return super().redirect_request(
            request, fp, code, message, headers, safe_url(new_url))


def filename(value: str) -> str:
    if not value or value in (".", "..") or Path(value).name != value or "/" in value or "\\" in value:
        raise ValueError("Dependency output names must be plain filenames")
    return value


def load_lock() -> list[dict]:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    if lock.get("schema_version") != 1:
        raise ValueError("Unsupported dependency lock version")
    dependencies = lock["dependencies"]
    names: set[str] = set()
    for item in dependencies:
        safe_url(item["url"])
        outputs = [(item["filename"], item["sha256"])]
        if item.get("classes_jar"):
            outputs.append((item["classes_jar"], item["classes_sha256"]))
        for name, expected in outputs:
            filename(name)
            if name in names or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
                raise ValueError("Invalid or duplicated dependency lock entry")
            names.add(name)
    return dependencies


def verify(path: Path, expected: str) -> None:
    if not path.is_file():
        raise ValueError(f"Missing dependency: {path.name}")
    if digest(path) != expected:
        raise ValueError(f"SHA-256 mismatch: {path.name}")


def outputs(item: dict) -> list[tuple[str, str]]:
    result = [(item["filename"], item["sha256"])]
    if item.get("classes_jar"):
        result.append((item["classes_jar"], item["classes_sha256"]))
    return result


def restore(item: dict, destination: Path, check_only: bool, timeout: float) -> str:
    expected_outputs = outputs(item)
    missing = []
    for name, expected in expected_outputs:
        target = destination / name
        if target.exists() or check_only:
            verify(target, expected)
        else:
            missing.append(name)
    if not missing:
        return f"OK {item['coordinate']}"

    # Download and verify in a private temporary directory before installing any
    # output for this artifact. Existing mismatched files are never overwritten.
    with tempfile.TemporaryDirectory(prefix=".moa-dependency-", dir=destination) as temporary:
        staging = Path(temporary)
        archive = staging / item["filename"]
        existing_archive = destination / item["filename"]
        if existing_archive.is_file():
            archive = existing_archive
        else:
            opener = urllib.request.build_opener(OfficialRedirects())
            request = urllib.request.Request(item["url"], headers={"User-Agent": "MOA-dependency-restore/1"})
            with opener.open(request, timeout=timeout) as response, archive.open("wb") as target:
                safe_url(response.url)
                count = 0
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_ARCHIVE_BYTES:
                        raise ValueError(f"Unexpected artifact size: {item['filename']}")
                    target.write(chunk)
            verify(archive, item["sha256"])

        if item.get("classes_jar"):
            extracted = staging / item["classes_jar"]
            with zipfile.ZipFile(archive) as aar:
                entry = aar.getinfo("classes.jar")
                if entry.file_size > MAX_ARCHIVE_BYTES:
                    raise ValueError("Unexpected classes.jar size")
                extracted.write_bytes(aar.read(entry))
            verify(extracted, item["classes_sha256"])
        for name, expected in expected_outputs:
            target = destination / name
            if target.exists():
                verify(target, expected)
                continue
            (staging / name).replace(target)
    return f"RESTORED {item['coordinate']}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "android" / "deps",
                        help="dependency folder (default: android/deps)")
    parser.add_argument("--check", action="store_true",
                        help="verify local files only; never download or change files")
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=4,
                        metavar="1..8", help="parallel downloads (default: 4)")
    parser.add_argument("--timeout", type=float, default=30,
                        help="per-network-operation timeout in seconds (default: 30)")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    try:
        dependencies = load_lock()
        destination = args.output_dir.resolve()
        if not args.check:
            destination.mkdir(parents=True, exist_ok=True)
        failures = []
        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            tasks = {executor.submit(restore, item, destination, args.check, args.timeout): item
                     for item in dependencies}
            for task in as_completed(tasks):
                try:
                    print(task.result(), flush=True)
                except Exception as error:
                    item = tasks[task]
                    failures.append(item["coordinate"])
                    print(f"FAILED {item['coordinate']}: {error}", file=sys.stderr, flush=True)
        if failures:
            print("No credentials are required. Check internet access and the reported files; "
                  "rerun after fixing the problem. Existing mismatched files are not overwritten.", file=sys.stderr)
            return 1
        count = sum(len(outputs(item)) for item in dependencies)
        print(f"Verified {len(dependencies)} Maven artifacts / {count} build files in {destination}")
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"Dependency restore failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
