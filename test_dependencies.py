import json
from pathlib import Path
import tempfile
import unittest
from check_dependencies import check_launcher, check_symbols, check_version


class DependencyTests(unittest.TestCase):
    def test_snapshot_launcher_exception_is_narrow(self):
        from prepare_snapshot import relative_name, ValidationError
        self.assertEqual(relative_name('repo-nav.cmd'), 'repo-nav.cmd')
        for name in ('other.cmd', 'nested/repo-nav.cmd', 'payload.exe'):
            with self.assertRaises(ValidationError):
                relative_name(name)

    def test_exact_versions(self):
        check_version('1.7.0', '1.7.0', 'Serena')
        for version in ('1.6.0', '2.0.0', '1.7.1'):
            with self.assertRaisesRegex(ValueError, 'supported version'):
                check_version(version, '1.7.0', 'Serena')

    def test_launcher_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = str(root / 'graphify.exe')
            (root / 'uv-receipt.toml').write_text(
                '[tool]\nentrypoints = [{name="graphify", install-path=' + json.dumps(executable) + '}]\n', encoding='utf-8')
            check_launcher(root, 'graphify', executable)
            with self.assertRaisesRegex(ValueError, 'PATH launcher differs'):
                check_launcher(root, 'graphify', str(root / 'other.exe'))

    def test_same_version_incompatible_interface(self):
        check_symbols('def rebuild(root, *, changed_paths): pass', {'rebuild': ['changed_paths']})
        for source in ('def other(): pass', 'def rebuild(root): pass'):
            with self.assertRaisesRegex(ValueError, 'Unsupported tool interface'):
                check_symbols(source, {'rebuild': ['changed_paths']})


if __name__ == '__main__':
    unittest.main()
