"""Public exports keep source while rejecting runtime data and credentials."""
import importlib.util
import binascii
from pathlib import Path
import tempfile
import struct
import unittest
import zlib


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "check_public_release.py"
SPEC = importlib.util.spec_from_file_location("check_public_release", MODULE)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class PublicReleaseTests(unittest.TestCase):
    @staticmethod
    def png_fixture(width=2, height=1):
        def chunk(kind, payload):
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xffffffff)
        header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                + chunk(b"IDAT", zlib.compress(b"\x00" + b"\x00" * 6)) + chunk(b"IEND", b""))

    def test_only_six_reviewed_screenshot_paths_are_allowed(self):
        self.assertEqual(len(release.PUBLIC_IMAGES), 6)
        for name in release.PUBLIC_IMAGES:
            self.assertTrue(release.allowed_source(name), name)
        for name in ("docs/images/07-private.png", "screenshots/01-conversation.png",
                     "docs/images/nested/01-conversation.png", "docs/01-conversation.png",
                     "docs/images/01-conversation.PNG"):
            self.assertFalse(release.allowed_source(name), name)

    def test_png_file_checks_magic_dimensions_and_container(self):
        valid = self.png_fixture()
        self.assertEqual(release.png_issues(valid), [])
        self.assertIn("invalid PNG signature", release.png_issues(b"not an image"))
        for width, height in ((0, 1), (1, 0), (8193, 1), (1, 8193), (8192, 8192)):
            self.assertIn("invalid or excessive PNG dimensions", release.png_issues(self.png_fixture(width, height)))
        self.assertIn("missing PNG image ending", release.png_issues(valid[:-12]))
        self.assertIn("invalid PNG image ending", release.png_issues(valid + b"unexpected trailing data"))
        corrupted = bytearray(valid)
        corrupted[28] ^= 1
        self.assertIn("invalid PNG chunk checksum", release.png_issues(bytes(corrupted)))

    def test_curated_png_is_read_as_binary_and_other_images_stay_excluded(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix="tmp-public-") as folder:
            root = Path(folder)
            image = root / "docs/images/01-conversation.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(self.png_fixture())
            other = image.with_name("07-private.png")
            other.write_bytes(self.png_fixture())
            files, issues = release.inspect_tree(root)
            self.assertEqual(files, ["docs/images/01-conversation.png"])
            self.assertEqual(issues, [])
            image.write_bytes(b"a renamed text file")
            self.assertIn("invalid PNG signature", release.file_issues(image))

    def test_developer_sources_are_included(self):
        for path in ("server.py", "start.ps1", "web/favicon.svg", "android/src/pkg/App.java",
                     "android/tests/pkg/RouteTest.java", "android/res/drawable/ic_moa.xml",
                     "tests/test_trip_ui.cjs", "tests/speech-sdk-probe/prepare.py",
                     "tools/build_poi_index.py", "scripts/android-dependencies.json",
                     "docs/MOA_Proposal_Public.docx"):
            self.assertTrue(release.allowed_source(path), path)

    def test_private_runtime_files_and_original_documents_are_not_included(self):
        for path in (".env", "data/meeting.json", "tests/tmp123/meeting.json",
                     "tests/map-check-avd/userdata.img", "screenshots/desktop.png",
                     "android/deps/navi-helper.jar", "android/build/debug.keystore",
                     "docs/Original_Application.docx", "../README.md", "/README.md"):
            self.assertFalse(release.allowed_source(path), path)

    def test_credential_diagnostics_do_not_echo_secret_values(self):
        token = "ghp_" + "a" * 36
        issues = release.content_issues('TOKEN = "' + token + '"')
        self.assertIn("GitHub or API token", issues)
        self.assertNotIn(token, str(issues))
        private_path = "C:" + chr(92) + "Users" + chr(92) + "private-user" + chr(92) + "data.json"
        self.assertIn("machine-specific user path", release.content_issues(private_path))

    def test_export_omits_data_but_staged_verification_rejects_it(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix="tmp-public-") as folder:
            root = Path(folder)
            (root / "server.py").write_text("print('hello')", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "meeting.json").write_text("{}", encoding="utf-8")
            files, issues = release.inspect_tree(root)
            self.assertEqual(files, ["server.py"])
            self.assertEqual(issues, [])
            _, staged_issues = release.inspect_tree(root, verify=True)
            self.assertEqual(staged_issues[0]["path"], "data/meeting.json")

    def test_verification_rejects_an_empty_or_incomplete_export(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix="tmp-public-") as folder:
            _, issues = release.inspect_tree(Path(folder), verify=True)
            missing = {item["path"] for item in issues if item["issue"] == "missing required source file"}
            self.assertEqual(missing, release.REQUIRED_FILES)

    def test_manifest_comparison_catches_an_optional_source_omitted_during_copy(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix="tmp-public-") as folder:
            root = Path(folder)
            _, issues = release.inspect_tree(root, verify=True, expected_files={"tests/test_trip_ui.cjs"})
            self.assertIn({"path": "tests/test_trip_ui.cjs", "issue": "missing required source file"}, issues)

    def test_complete_staged_source_tree_passes(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix="tmp-public-") as folder:
            root = Path(folder)
            expected = release.REQUIRED_FILES | {"android/src/local/pleos/cabin/AppBranding.java"}
            for relative in expected:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("public source fixture", encoding="utf-8")
            files, issues = release.inspect_tree(root, verify=True, expected_files=expected)
            self.assertEqual(set(files), expected)
            self.assertEqual(issues, [])

    def test_manifest_loader_rejects_private_paths_and_failed_source_checks(self):
        import json
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix="tmp-public-") as folder:
            path = Path(folder) / "manifest.json"
            for manifest in ({"ok": True, "files": ["data/meeting.json"], "issues": []},
                             {"ok": False, "files": ["server.py"], "issues": []}):
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ValueError):
                    release.load_expected_files(path)
            path.write_text(json.dumps({"ok": True, "files": ["server.py"], "issues": []}), encoding="utf-8")
            self.assertEqual(release.load_expected_files(path), {"server.py"})


if __name__ == "__main__":
    unittest.main()
