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
        result = self.run_installer("codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = os.path.join(self.temp_home, ".codex/skills/ynab-agent-skill")
        pref_file = os.path.join(dest, "references", "local-preferences.md")
        with open(pref_file, "w") as f:
            f.write("custom existing preference")
            
        result = self.run_installer("codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(pref_file, "r") as f:
            self.assertEqual(f.read(), "custom existing preference")

    def test_unsafe_symlinks(self):
        dest_parent = os.path.join(self.temp_home, ".codex/skills")
        os.makedirs(dest_parent)
        dest = os.path.join(dest_parent, "ynab-agent-skill")
        os.symlink("/tmp", dest)
        
        result = self.run_installer("codex")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsafe to proceed", result.stdout + result.stderr)


class InstallerSafetyTests(unittest.TestCase):
    def setUp(self):
        from tools import install_skill
        from unittest.mock import patch
        self.module = install_skill
        self.tmp = tempfile.TemporaryDirectory(prefix='test_home ')
        self.addCleanup(self.tmp.cleanup)
        self.home = __import__('pathlib').Path(self.tmp.name)
        self.root = __import__('pathlib').Path(__file__).absolute().parents[1]
        self.mount_patch = patch.object(install_skill.subprocess, 'run', return_value=
            subprocess.CompletedProcess([], 0, 'disk on / (apfs, local)\n', ''))
        self.mount_patch.start()
        self.addCleanup(self.mount_patch.stop)

    def seed(self, host, name='ynab-agent-skill', preference=b'fictional private preference'):
        dest = self.home / self.module.HOSTS[host] / name
        (dest / 'references').mkdir(parents=True)
        (dest / self.module.PREFERENCE).write_bytes(preference)
        (dest / 'custom.txt').write_bytes(b'custom file retained in backup')
        return dest

    def test_both_hosts_backup_isolation_and_repeat_install(self):
        first = self.seed('codex', preference=b'fictional codex preference')
        second = self.seed('antigravity', preference=b'fictional antigravity preference')
        for _ in range(2):
            self.module.install(self.root, self.home, 'both')
            self.assertEqual((first / self.module.PREFERENCE).read_bytes(), b'fictional codex preference')
            self.assertEqual((second / self.module.PREFERENCE).read_bytes(), b'fictional antigravity preference')
        backups = list((self.home / '.ynab-agent-skill-backups').glob('*/codex/ynab-agent-skill'))
        self.assertEqual(len(backups), 2)
        self.assertTrue(any((p / 'custom.txt').exists() for p in backups))
        for dest in (first, second):
            self.assertEqual(dest.stat().st_mode & 0o777, 0o700)
            self.assertEqual((dest / self.module.PREFERENCE).stat().st_mode & 0o777, 0o600)
            for name in self.module.SKILL_FILES:
                self.assertEqual((dest / name).read_bytes(),
                                 (self.root / 'skills/ynab-agent-skill' / name).read_bytes())

    def test_current_preferences_win_over_legacy(self):
        dest = self.seed('codex', preference=b'current')
        legacy = self.seed('codex', 'ynab-codex-skill', b'legacy')
        self.module.install(self.root, self.home, 'codex')
        self.assertEqual((dest / self.module.PREFERENCE).read_bytes(), b'current')
        self.assertFalse(legacy.exists())
        self.assertEqual(len(list((self.home / '.ynab-agent-skill-backups').glob('*/codex/ynab-codex-skill'))), 1)

    def test_dry_run_has_no_mutating_operations(self):
        from unittest.mock import patch
        from contextlib import ExitStack
        self.seed('codex')
        with ExitStack() as stack:
            for name in ('mkdir', 'rename', 'chmod', 'unlink', 'write_bytes'):
                stack.enter_context(patch.object(self.module.Path, name, side_effect=AssertionError('dry-run write')))
            stack.enter_context(patch.object(self.module.os, 'open', side_effect=AssertionError('dry-run lock')))
            self.module.install(self.root, self.home, 'both', dry_run=True)
        self.assertFalse((self.home / '.ynab-agent-skill-backups').exists())

    def test_ancestor_symlink_rejected_before_any_install(self):
        outside = self.home / 'outside'
        outside.mkdir()
        (self.home / '.gemini').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(self.module.InstallError):
            self.module.install(self.root, self.home, 'both')
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.home / '.codex').exists())

    def test_unknown_mount_rejected_without_path_inspection(self):
        from unittest.mock import patch
        with patch.object(self.module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            with patch.object(self.module.Path, 'lstat', side_effect=AssertionError('unverified access')):
                with self.assertRaises(self.module.InstallError):
                    self.module.install(self.root, self.home, 'codex')

    def test_source_allowlist_does_not_copy_private_or_extra_files(self):
        source_root = self.home / 'checkout'
        source = source_root / 'skills/ynab-agent-skill'
        shutil.copytree(self.root / 'skills/ynab-agent-skill', source)
        (source_root / 'bin').mkdir()
        (source_root / 'bin/ynab-agent-skill').write_text('synthetic launcher')
        (source / self.module.PREFERENCE).write_bytes(b'never distribute')
        (source / 'unexpected.txt').write_bytes(b'never distribute')
        self.module.install(source_root, self.home, 'codex')
        dest = self.home / '.codex/skills/ynab-agent-skill'
        self.assertFalse((dest / 'unexpected.txt').exists())
        self.assertNotEqual((dest / self.module.PREFERENCE).read_bytes(), b'never distribute')

    def test_hardlinked_preferences_rejected(self):
        dest = self.seed('codex')
        os.link(dest / self.module.PREFERENCE, self.home / 'other-name')
        with self.assertRaises(self.module.InstallError):
            self.module.install(self.root, self.home, 'codex')
        self.assertTrue((dest / 'custom.txt').exists())

    def test_second_host_failure_restores_both_and_legacy(self):
        from unittest.mock import patch
        first = self.seed('codex', preference=b'first')
        legacy = self.seed('codex', 'ynab-codex-skill', b'legacy')
        second = self.seed('antigravity', preference=b'second')
        verify = self.module._verify
        def fail(directory, expected, safe):
            if directory == second:
                raise self.module.InstallError('injected readback failure')
            return verify(directory, expected, safe)
        with patch.object(self.module, '_verify', side_effect=fail):
            with self.assertRaisesRegex(self.module.InstallError, 'injected'):
                self.module.install(self.root, self.home, 'both')
        for dest, expected in ((first, b'first'), (second, b'second'), (legacy, b'legacy')):
            self.assertEqual((dest / self.module.PREFERENCE).read_bytes(), expected)
            self.assertTrue((dest / 'custom.txt').exists())
        self.assertEqual(list(self.home.rglob('.ynab-install-*')), [])

    def test_staging_corruption_does_not_displace_existing_skill(self):
        from unittest.mock import patch
        dest = self.seed('codex')
        write = self.module._write
        def corrupt(path, data):
            write(path, b'corrupted' if path.name == 'SKILL.md' else data)
        with patch.object(self.module, '_write', side_effect=corrupt):
            with self.assertRaisesRegex(self.module.InstallError, 'verification'):
                self.module.install(self.root, self.home, 'codex')
        self.assertTrue((dest / 'custom.txt').exists())
