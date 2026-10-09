import os
import subprocess
import tempfile
import shutil
import stat
import unittest

class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.temp_home = tempfile.mkdtemp(prefix="test_home ") # Includes space
        self.install_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "install.sh"))
        self.env = os.environ.copy()
        self.env["HOME"] = self.temp_home

    def tearDown(self):
        shutil.rmtree(self.temp_home)

    def run_installer(self, *args):
        # run installer from a different cwd to ensure it resolves paths correctly
        cwd = tempfile.mkdtemp()
        try:
            return subprocess.run(
                [self.install_script, *args],
                env=self.env,
                cwd=cwd,
                capture_output=True,
                text=True
            )
        finally:
            shutil.rmtree(cwd)

    def test_codex_target(self):
        result = self.run_installer("codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = os.path.join(self.temp_home, ".codex/skills/ynab-agent-skill")
        self.assertTrue(os.path.isdir(dest))
        self.assertTrue(os.path.isfile(os.path.join(dest, "SKILL.md")))

    def test_antigravity_target(self):
        result = self.run_installer("antigravity")
        self.assertEqual(result.returncode, 0)
        dest = os.path.join(self.temp_home, ".gemini/config/skills/ynab-agent-skill")
        self.assertTrue(os.path.isdir(dest))
        self.assertTrue(os.path.isfile(os.path.join(dest, "SKILL.md")))

    def test_both_targets(self):
        result = self.run_installer("both")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.temp_home, ".codex/skills/ynab-agent-skill")))
        self.assertTrue(os.path.isdir(os.path.join(self.temp_home, ".gemini/config/skills/ynab-agent-skill")))

    def test_dry_run(self):
        result = self.run_installer("both", "--dry-run")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(os.path.isdir(os.path.join(self.temp_home, ".codex")))
        self.assertFalse(os.path.isdir(os.path.join(self.temp_home, ".gemini")))

    def test_legacy_migration_and_preference_preservation(self):
        legacy_dir = os.path.join(self.temp_home, ".codex/skills/ynab-codex-skill")
        os.makedirs(os.path.join(legacy_dir, "references"))
        pref_file = os.path.join(legacy_dir, "references", "local-preferences.md")
        with open(pref_file, "w") as f:
            f.write("legacy private preference content")
        
        result = self.run_installer("codex")
        self.assertEqual(result.returncode, 0)
        
        # Legacy dir should be gone
        self.assertFalse(os.path.isdir(legacy_dir))
        
        dest = os.path.join(self.temp_home, ".codex/skills/ynab-agent-skill")
        self.assertTrue(os.path.isdir(dest))
        
        new_pref = os.path.join(dest, "references", "local-preferences.md")
        self.assertTrue(os.path.isfile(new_pref))
        
        with open(new_pref, "r") as f:
            self.assertEqual(f.read(), "legacy private preference content")
            
        # Check permissions
        st = os.stat(new_pref)
        self.assertEqual(st.st_mode & 0o777, 0o600)

    def test_reinstallation_preserves_preferences(self):
        self.run_installer("codex")
        dest = os.path.join(self.temp_home, ".codex/skills/ynab-agent-skill")
        pref_file = os.path.join(dest, "references", "local-preferences.md")
        with open(pref_file, "w") as f:
            f.write("custom existing preference")
            
        self.run_installer("codex")
        with open(pref_file, "r") as f:
            self.assertEqual(f.read(), "custom existing preference")

    def test_unsafe_symlinks(self):
        dest_parent = os.path.join(self.temp_home, ".codex/skills")
        os.makedirs(dest_parent)
        dest = os.path.join(dest_parent, "ynab-agent-skill")
        os.symlink("/tmp", dest)
        
        result = self.run_installer("codex")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsafe to proceed", result.stdout)
