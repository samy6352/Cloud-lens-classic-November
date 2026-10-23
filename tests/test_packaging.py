"""Exercise customer packaging from a source directory without Git or Azure access."""
# cloudlens-test: requires-offline
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(POWERSHELL, "PowerShell is required for deployment packaging")
class Packaging(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".pytest_cache", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.archive = Path(self.temp.name) / "application.zip"
        for name, content in {
            "requirements.txt": "fastapi>=0.115.0\n",
            "app/__init__.py": "",
            "app/main.py": "from . import billing\n",
            "app/billing.py": "VALUE = 1\n",
            "web/index.html": "<html></html>",
            "web/login.html": "<html></html>",
            "web/assets/app.js": "export const value = 1;",
            "web/assets/app.css": "body { color: inherit; }",
            "web/assets/dashboard.js": 'import {value} from "./app.js"; import("./dashboard-billing.js");',
            "web/assets/dashboard-billing.js": "export const ready = true;",
            ".env": "AUTH_PASSWORD=must-not-ship",
            ".localdata/.session_secret": "must-not-ship",
            "data/users.json": '{"must-not-ship":true}',
            "sample.csv": "must-not-ship",
            "pyproject.toml": "[project]",
            "tests/test_example.py": "must-not-ship",
            "app/__pycache__/main.py": "must-not-ship",
            "web/assets/.local/private.js": "must-not-ship",
            ".git/config": "must-not-ship",
            "logs/token.log": "must-not-ship",
            ".cache/state.json": "must-not-ship",
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def package(self):
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-File", str(ROOT / "tools" / "package.ps1"),
             "-SourceRoot", str(self.root), "-Destination", str(self.archive),
             "-Python", sys.executable],
            capture_output=True, text=True, timeout=60,
        )

    def test_includes_untracked_modules_excludes_state_and_records_hashes(self):
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(self.archive) as archive:
            names = set(archive.namelist())
            self.assertIn("app/billing.py", names)
            self.assertIn("web/assets/dashboard-billing.js", names)
            self.assertIn("release-manifest.json", names)
            self.assertEqual(len(names), 11)
            for name in names:
                self.assertNotIn(b"must-not-ship", archive.read(name))
            manifest = json.loads(archive.read("release-manifest.json"))
            for entry in manifest["files"]:
                content = archive.read(entry["path"])
                self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
                self.assertEqual(entry["bytes"], len(content))
            identity = "".join(
                f"{entry['path']}\t{entry['bytes']}\t{entry['sha256']}\n"
                for entry in manifest["files"])
            self.assertEqual(manifest["sourceContentSha256"],
                             hashlib.sha256(identity.encode()).hexdigest())

    def test_identical_snapshot_is_byte_reproducible(self):
        import os

        first = self.package()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        before = self.archive.read_bytes()
        self.archive.unlink()
        for path in self.root.rglob("*"):
            if path.is_file():
                os.utime(path, (1800000000, 1800000000))
        second = self.package()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(before, self.archive.read_bytes())

    def test_unreferenced_or_private_source_cannot_enter_the_release(self):
        for name, content, reason in (
            ("app/dump.py", "not valid must-not-ship", "Unexpected unreferenced Python source"),
            ("app/credentials.py", "must-not-ship", "Refusing a private/state file"),
            ("web/assets/tokens.js", "must-not-ship", "Refusing a private/state file"),
        ):
            path = self.root / name
            path.write_text(content, encoding="utf-8")
            with self.subTest(name=name):
                result = self.package()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason, result.stdout + result.stderr)
                self.assertFalse(self.archive.exists())
            path.unlink()

    def test_missing_lazy_module_refuses_package(self):
        (self.root / "web/assets/dashboard-billing.js").unlink()
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing imported module", result.stdout + result.stderr)
        self.assertFalse(self.archive.exists())

    def test_import_case_is_checked_for_linux(self):
        (self.root / "web/assets/dashboard.js").write_text('import("./Dashboard-billing.js");', encoding="utf-8")
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing imported module", result.stdout + result.stderr)

    def test_existing_destination_is_not_overwritten(self):
        self.archive.write_bytes(b"keep existing artifact")
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.archive.read_bytes(), b"keep existing artifact")

    def test_missing_python_module_refuses_package_without_importing_app(self):
        (self.root / "app/billing.py").unlink()
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing Python module", result.stdout + result.stderr)
        self.assertFalse(self.archive.exists())

    def test_invalid_python_syntax_refuses_package(self):
        (self.root / "app/main.py").write_text("def invalid(\n", encoding="utf-8")
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Package source validation failed", result.stdout + result.stderr)
        self.assertFalse(self.archive.exists())

    def test_missing_html_asset_and_css_font_refuse_package(self):
        for name, content in (
            ("web/index.html", '<script type="module" src="/assets/missing.js"></script>'),
            ("web/assets/app.css", '@font-face { src: url("missing.woff2"); }'),
        ):
            path = self.root / name
            original = path.read_text(encoding="utf-8")
            path.write_text(content, encoding="utf-8")
            with self.subTest(name=name):
                result = self.package()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Missing web asset", result.stdout + result.stderr)
                self.assertFalse(self.archive.exists())
            path.write_text(original, encoding="utf-8")

    def test_javascript_import_fragments_do_not_change_the_file_name(self):
        (self.root / "web/assets/dashboard.js").write_text(
            'import("./dashboard-billing.js#version");', encoding="utf-8")
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
