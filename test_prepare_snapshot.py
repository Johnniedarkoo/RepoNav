"""Synthetic fixtures only. No Git commands, installed tools or real configuration."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import prepare_snapshot as snapshot


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='reponav-publication-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        self.root.mkdir()
        self.dest = self.base / 'export'
        self.put('README.md', b'# Synthetic project\r\n')
        self.manifest(['README.md'])

    def put(self, name, data):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def manifest(self, names):
        self.put('release-files.txt', ('\n'.join(names) + '\n').encode())

    def run_export(self):
        return snapshot.export(self.root, self.dest)

    def test_valid_hashes_bytes_and_source_preservation(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.run_export()
        self.assertEqual(result['verification'], 'passed')
        self.assertEqual(result['files'], [{'path': 'README.md', 'sha256': hashlib.sha256(before[Path('README.md')]).hexdigest()}])
        self.assertEqual((self.dest / 'README.md').read_bytes(), before[Path('README.md')])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_unlisted_tracked_untracked_state_audit_and_custom_ref(self):
        # Synthetic index/ref bytes: exporter cannot and does not interpret Git.
        for n in ('untracked.txt', 'tracked.txt', '.serena/cache/local.txt',
                  'graphify-out/graph.json', 'workflow-pollution-audit.md',
                  '.git/index', '.git/refs/codex/private'):
            self.put(n, b'Excluded synthetic private data')
        self.run_export()
        self.assertEqual([p.name for p in self.dest.iterdir()], ['README.md'])
        self.assertEqual((self.root / '.git/refs/codex/private').read_bytes(), b'Excluded synthetic private data')

    def test_missing_required(self):
        self.manifest(['missing.md'])
        with self.assertRaises(OSError):
            self.run_export()
        self.assertFalse(self.dest.exists())

    def test_duplicate_case_collision_and_unsafe_paths(self):
        for names in (['README.md', 'README.md'], ['README.md', 'readme.md'],
                      ['../outside.md'], ['/absolute.md'], ['C:' + '/outside.md'],
                      ['folder\\outside.md'], ['folder/../outside.md'], ['*.md'],
                      ['CON.txt'], ['bad./x.md'], [' README.md'], ['.git/config.txt'],
                      ['graphify-out/graph.json'], ['tool.exe'], ['.serena/cache.py']):
            with self.subTest(names=names):
                self.manifest(names)
                with self.assertRaises(snapshot.ValidationError):
                    self.run_export()
                self.assertFalse(self.dest.exists())

    def test_license_exact_filename_and_required_export(self):
        self.assertEqual(snapshot.relative_name('LICENSE'), 'LICENSE')
        for name in ('LICENSE.other', 'OTHER', 'nested/LICENSE', 'license'):
            with self.assertRaises(snapshot.ValidationError):
                snapshot.relative_name(name)
        self.manifest(['README.md', 'LICENSE'])
        with self.assertRaises(OSError):
            self.run_export()
        self.put('LICENSE', b'MIT License\nSynthetic license fixture\n')
        result = self.run_export()
        self.assertEqual([item['path'] for item in result['files']], ['LICENSE', 'README.md'])
        self.assertEqual((self.dest / 'LICENSE').read_bytes(), (self.root / 'LICENSE').read_bytes())

    def test_existing_destination_preserved(self):
        self.dest.mkdir()
        p = self.dest / 'keep.txt'
        p.write_bytes(b'Keep')
        with self.assertRaises(snapshot.ValidationError):
            self.run_export()
        self.assertEqual(p.read_bytes(), b'Keep')

    def test_case_colliding_directories_and_file_directory_collision(self):
        for names in (['Folder/a.txt', 'folder/b.txt'], ['a.txt', 'a.txt/b.txt']):
            self.manifest(names)
            with self.assertRaises(snapshot.ValidationError):
                self.run_export()

    def test_nonordinary_file_and_nonutf8(self):
        (self.root / 'README.md').unlink()
        (self.root / 'README.md').mkdir()
        with self.assertRaises(snapshot.ValidationError):
            self.run_export()
        (self.root / 'README.md').rmdir()
        self.put('README.md', bytes([255, 254]))
        with self.assertRaises(snapshot.ValidationError):
            self.run_export()

    def test_reproducible_summary(self):
        first = self.run_export()
        self.dest = self.base / 'second export'
        self.assertEqual(first, self.run_export())

    def test_url_and_regex_are_not_absolute_paths(self):
        self.put('README.md', b'https://example.org/docs\npost-commit:' + bytes([92]) + b's+installed')
        self.run_export()

    def test_destination_inside_source_rejected(self):
        with self.assertRaises(snapshot.ValidationError):
            snapshot.export(self.root, self.root / 'output')

    def test_spaces_unicode_and_nested_paths(self):
        name = 'source examples/caf\u00e9 \u03b1.txt'
        self.put(name, 'Synthetic Unicode\n'.encode())
        self.manifest(['README.md', name])
        self.run_export()
        self.assertEqual((self.dest / name).read_bytes(), (self.root / name).read_bytes())

    def test_privacy_patterns_fail_before_output(self):
        examples = [b'gh' + b'p_' + b'X' * 32,
                    ('C:' + chr(92) + 'Users' + chr(92) + 'Synthetic').encode(),
                    b'password' + b' = ' + bytes([34]) + b'synthetic-value' + bytes([34]),
                    b'person' + b'@' + b'synthetic.test', b'\x00binary']
        for data in examples:
            with self.subTest(length=len(data)):
                self.put('README.md', data)
                with self.assertRaises(snapshot.ValidationError):
                    self.run_export()
                self.assertFalse(self.dest.exists())

    def test_private_fingerprint_substrings(self):
        from unittest.mock import patch
        import hashlib
        marker = 'private-fixture'
        rules = ((len(marker), hashlib.sha256(marker.encode()).hexdigest()),)
        with patch.object(snapshot, 'PRIVATE_FINGERPRINTS', rules):
            for text in ('prefix PRIVATE-fixture suffix', 'private-fixture'):
                with self.assertRaises(snapshot.ValidationError):
                    snapshot.scan('README.md', text.encode())
            with self.assertRaises(snapshot.ValidationError):
                snapshot.scan('prefix-private-fixture.md', b'ordinary')
            snapshot.scan('README.md', b'ordinary public example')

    def test_unexpected_export_file_or_directory(self):
        result = self.run_export()
        hashes = {x['path']: x['sha256'] for x in result['files']}
        (self.dest / 'extra.txt').write_bytes(b'Unexpected')
        with self.assertRaises(snapshot.ValidationError):
            snapshot.verify(self.root, self.dest, ['README.md'], hashes)
        (self.dest / 'extra.txt').unlink()
        (self.dest / '.git').mkdir()
        with self.assertRaises(snapshot.ValidationError):
            snapshot.verify(self.root, self.dest, ['README.md'], hashes)

    def test_modified_export_and_source(self):
        result = self.run_export()
        hashes = {x['path']: x['sha256'] for x in result['files']}
        self.put('README.md', b'Changed source')
        with self.assertRaises(snapshot.ValidationError):
            snapshot.verify(self.root, self.dest, ['README.md'], hashes)
        (self.dest / 'README.md').write_bytes(b'Changed output')
        with self.assertRaises(snapshot.ValidationError):
            snapshot.verify(self.root, self.dest, ['README.md'], hashes)

    def test_source_link_outside_root(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'file.txt').write_bytes(b'Outside bytes')
        link = self.root / 'linked'
        if os.name == 'nt':
            # Native directory junction requires no symlink privilege. Both targets
            # are disposable children of this test's uniquely owned temp root.
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
            if result.returncode:
                self.skipTest('Disposable junction creation unavailable')
            self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)
        else:
            link.symlink_to(outside, target_is_directory=True)
        self.manifest(['linked/file.txt'])
        with self.assertRaises(snapshot.ValidationError):
            self.run_export()
        self.assertEqual((outside / 'file.txt').read_bytes(), b'Outside bytes')


if __name__ == '__main__':
    unittest.main()
