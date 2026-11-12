"""List source files for a clean public export, or verify an already exported tree.

This intentionally does not copy files or publish anything. The allowlist excludes
runtime data even when a new generated file is not covered by .gitignore. Secret
checks are a final guard for recognizable credentials, not a privacy guarantee.
"""
from __future__ import annotations

import argparse
import binascii
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import sys
import zipfile


SOURCE_PRUNE = {
    ".git", ".venv", "models", "data", "__pycache__", ".pytest_cache",
    "build", "deps", "android-sdk", "screenshots", ".public-place-check",
    "fixtures", "map-check-avd", "map-diagnostics", "sdk-research", "live-travel",
}
ROOT_FILES = {
    ".gitignore", ".gitattributes", "README.md", "LICENSE", "NOTICE",
    "requirements.txt", "requirements-poi.txt",
}
REQUIRED_FILES = {
    ".gitignore", "README.md", "requirements.txt", "server.py", "start.ps1",
    "web/index.html", "web/app.js", "web/app.css", "web/favicon.svg",
    "android/AndroidManifest.xml", "android/build.ps1", "android/assets/schema.json",
    "android/src/local/pleos/cabin/MainActivity.java",
    "android/src/local/pleos/cabin/NativeNavigation.java",
    "scripts/check_public_release.py", "scripts/restore_android_deps.py",
    "scripts/android-dependencies.json", "tools/build_poi_index.py",
}
PROBE_FILES = {
    ".gitignore", "AndroidManifest.xml", "build.ps1", "prepare.py", "README.md",
    "SpeechSdkProbe.java",
}
MAX_FILE_BYTES = 12 * 1024 * 1024
PUBLIC_IMAGES = {
    "docs/images/01-conversation.png",
    "docs/images/02-recommendations.png",
    "docs/images/03-place-detail.png",
    "docs/images/04-native-guidance.png",
    "docs/images/05-save-decision.png",
    "docs/images/06-saved-places.png",
}
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 32 * 1024 * 1024
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub or API token": re.compile(r"\b(?:ghp_|github_pat_|sk-proj-)[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    "embedded bearer credential": re.compile(
        r"(?i)authorization\s*[:=]\s*[\"']Bearer\s+[A-Za-z0-9_./+=-]{20,}"),
}
LOCAL_USER_PATH = re.compile(
    r"(?i)\b[A-Z]:[\\/]+Users[\\/]+(?!Public(?:[\\/]|$)|<|%|\$)[A-Za-z0-9_.-]+[\\/]"
)


def allowed_source(relative: str) -> bool:
    """Keep only application source, developer tests, and curated public docs."""
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return False
    parts = path.parts
    suffix = path.suffix.lower()
    if len(parts) == 1:
        return relative in ROOT_FILES or suffix in {".py", ".ps1"}
    if parts[0] == "web":
        return suffix in {".html", ".css", ".js", ".svg"}
    if relative in {"android/AndroidManifest.xml", "android/build.ps1", "android/assets/schema.json"}:
        return True
    if parts[:2] in {("android", "src"), ("android", "tests")}:
        return suffix == ".java"
    if parts[:2] == ("android", "res"):
        return suffix == ".xml"
    if parts[0] == "tests" and len(parts) == 2:
        return suffix in {".py", ".cjs", ".ps1"}
    if parts[:2] == ("tests", "speech-sdk-probe") and len(parts) == 3:
        return parts[2] in PROBE_FILES
    if parts[0] == "scripts" and len(parts) == 2:
        return suffix == ".py" or parts[1] == "android-dependencies.json"
    if relative == "tools/build_poi_index.py":
        return True
    if parts[0] == "docs":
        return suffix == ".md" or relative == "docs/MOA_Proposal_Public.docx" or relative in PUBLIC_IMAGES
    return False


def content_issues(text: str) -> list[str]:
    issues = [label for label, pattern in SECRET_PATTERNS.items() if pattern.search(text)]
    if LOCAL_USER_PATH.search(text):
        issues.append("machine-specific user path")
    return issues


def png_issues(data: bytes) -> list[str]:
    """Check the container of a separately reviewed screenshot, not its visual privacy."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ["invalid PNG signature"]
    if len(data) < 33 or data[8:16] != b"\x00\x00\x00\rIHDR":
        return ["missing PNG image header"]
    width, height = struct.unpack(">II", data[16:24])
    if not (0 < width <= MAX_IMAGE_DIMENSION and 0 < height <= MAX_IMAGE_DIMENSION
            and width * height <= MAX_IMAGE_PIXELS):
        return ["invalid or excessive PNG dimensions"]
    offset, image_data = 8, False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        end = offset + length + 12
        if end > len(data):
            return ["truncated PNG chunk"]
        kind = data[offset + 4:offset + 8]
        checksum = struct.unpack(">I", data[end - 4:end])[0]
        if binascii.crc32(data[offset + 4:end - 4]) & 0xffffffff != checksum:
            return ["invalid PNG chunk checksum"]
        if kind == b"IDAT":
            image_data = True
        if kind == b"IEND":
            if length != 0 or end != len(data) or not image_data:
                return ["invalid PNG image ending"]
            return []
        offset = end
    return ["missing PNG image ending"]


def file_issues(path: Path) -> list[str]:
    if path.is_symlink():
        return ["symbolic links are not exported"]
    if path.stat().st_size > MAX_FILE_BYTES:
        return ["unexpectedly large public source file"]
    try:
        if path.suffix.lower() == ".png":
            return png_issues(path.read_bytes())
        if path.suffix.lower() == ".docx":
            with zipfile.ZipFile(path) as archive:
                content = "\n".join(
                    archive.read(item).decode("utf-8", errors="replace")
                    for item in archive.infolist()
                    if item.filename.endswith((".xml", ".rels")) and item.file_size <= MAX_FILE_BYTES
                )
        else:
            content = path.read_text(encoding="utf-8-sig")
        return content_issues(content)
    except (OSError, UnicodeError, zipfile.BadZipFile) as error:
        return [f"unreadable source: {type(error).__name__}"]


def inspect_tree(root: Path, verify: bool = False,
                 expected_files: set[str] | None = None) -> tuple[list[str], list[dict[str, str]]]:
    files, issues = [], []
    for current, directories, names in os.walk(root, followlinks=False):
        for name in list(directories):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if name == ".git":
                directories.remove(name)
            elif path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                directories.remove(name)
                if verify:
                    issues.append({"path": relative, "issue": "symbolic link or junction directory"})
            elif not verify and (name in SOURCE_PRUNE or name.startswith(("tmp", "ui-"))):
                directories.remove(name)
        for name in names:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if not allowed_source(relative):
                if verify:
                    issues.append({"path": relative, "issue": "not in public source allowlist"})
                continue
            files.append(relative)
            issues.extend({"path": relative, "issue": issue} for issue in file_issues(path))
    if verify:
        required = REQUIRED_FILES | (expected_files or set())
        for relative in sorted(required - set(files)):
            issues.append({"path": relative, "issue": "missing required source file"})
    return sorted(files), issues


def load_expected_files(path: Path) -> set[str]:
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if (not isinstance(manifest, dict) or manifest.get("ok") is not True
            or manifest.get("issues") != [] or not isinstance(manifest.get("files"), list)
            or not manifest["files"]):
        raise ValueError("The export manifest must be a successful --source result")
    if any(not isinstance(name, str) or not allowed_source(name) for name in manifest["files"]):
        raise ValueError("The export manifest contains a non-public path")
    return set(manifest["files"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source", type=Path, help="Print an allowlisted source export manifest as JSON")
    mode.add_argument("--verify", type=Path, help="Reject unexpected files in an already exported tree")
    parser.add_argument("--manifest", type=Path,
                        help="With --verify, also require every file from a successful --source JSON result")
    args = parser.parse_args()
    if args.manifest and not args.verify:
        parser.error("--manifest can only be used with --verify")
    root = (args.source or args.verify).resolve()
    if not root.is_dir():
        parser.error("The supplied directory does not exist")
    expected = None
    if args.manifest:
        try:
            expected = load_expected_files(args.manifest)
        except (OSError, UnicodeError, ValueError, TypeError):
            parser.error("Cannot read a valid successful public export manifest")
    files, issues = inspect_tree(root, verify=args.verify is not None, expected_files=expected)
    print(json.dumps({"ok": not issues, "files": files, "issues": issues}, ensure_ascii=False, indent=2))
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
