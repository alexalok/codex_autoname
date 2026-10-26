import importlib.util
import json
from pathlib import Path
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_global", ROOT / "scripts/install_global.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_preserves_settings_and_leaves_plugin_disabled(self):
        before = '[features]\nhooks = true\n[plugins."existing@source"]\nenabled = true\n'
        after = installer.system_config(before)
        parsed = tomllib.loads(after)
        self.assertTrue(parsed["features"]["hooks"])
        self.assertTrue(parsed["plugins"]["existing@source"]["enabled"])
        self.assertFalse(parsed["plugins"][installer.PLUGIN]["enabled"])
        self.assertEqual(after, installer.system_config(after))

    def test_refuses_to_overwrite_unmanaged_settings(self):
        before = '[marketplaces.codex-autoname]\nsource_type="git"\nsource="example/repo"\n'
        with self.assertRaises(ValueError):
            installer.system_config(before)

    def test_staged_install_creates_shared_files_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer.install(ROOT, root)
            installer.install(ROOT, root)
            prefix = root / "opt/codex-autoname"
            self.assertEqual((ROOT / "scripts/auto_title.py").read_bytes(),
                             (prefix / "plugin/scripts/auto_title.py").read_bytes())
            catalog = json.loads((prefix / ".agents/plugins/marketplace.json").read_text())
            self.assertEqual("./plugin", catalog["plugins"][0]["source"]["path"])
            self.assertIn("ExecStart=/opt/codex-autoname/.venv/bin/python",
                          (root / "etc/systemd/user/codex-auto-title.service").read_text())
            self.assertFalse((root / "home").exists())
            self.assertFalse((root / "etc/systemd/user/default.target.wants").exists())
            self.assertFalse((prefix / "plugin/plugin.json").exists())
            self.assertEqual("./hooks/hooks.json",
                json.loads((prefix / "plugin/.codex-plugin/plugin.json").read_text())["hooks"])

    def test_upgrade_removes_only_the_old_managed_portable_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer.install(ROOT, root)
            old = root / "opt/codex-autoname/plugin/plugin.json"
            old.write_text(json.dumps({"name": "codex-auto-title", "version": "0.2.0"}))
            installer.install(ROOT, root)
            self.assertFalse(old.exists())

    def test_refuses_an_unmanaged_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "opt/codex-autoname").mkdir(parents=True)
            with self.assertRaises(ValueError):
                installer.install(ROOT, root)


if __name__ == "__main__":
    unittest.main()
