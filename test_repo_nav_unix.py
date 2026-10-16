"""Regressions for the Unix entry point (repo_nav.py and the repo-nav launcher)."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import repo_nav

HERE = Path(__file__).resolve().parent


@unittest.skipIf(os.name == 'nt', 'Unix entry point')
class UnixTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='reponav-unix-')
        self.addCleanup(temp.cleanup)
        self.base = Path(os.path.realpath(temp.name))

    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True)

    def repository(self, name='repo', files=None):
        root = self.base / name
        root.mkdir(parents=True)
        self.git(root, 'init', '-q')
        self.git(root, 'config', 'user.name', 'Synthetic Test')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        for relative, content in (files or {'README.md': 'synthetic\n'}).items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-qm', 'initial')
        return root


class LanguageDetectionTests(UnixTestCase):
    def test_tracked_source_only_with_exclusions(self):
        root = self.repository(files={
            'app/Model.php': '<?php', 'app/view.phtml': '<?php', 'web/app.ts': 'export {}',
            'vendor/lib/tool.py': 'x = 1', 'web/bundle.min.js': '', 'build/out.go': 'package x',
            'config.json': '{}', 'tests/fixtures/sample.rb': '', 'deleted.rs': 'fn main() {}'})
        (root / 'untracked.go').write_text('package x')
        (root / 'deleted.rs').unlink()
        self.assertEqual(repo_nav.serena_languages(str(root)), ['php', 'typescript'])

    def test_language_override(self):
        self.assertIsNone(repo_nav.parse_languages([]))
        self.assertEqual(repo_nav.parse_languages(['--languages', 'php, typescript']), ['php', 'typescript'])
        with self.assertRaisesRegex(ValueError, 'Unknown language'):
            repo_nav.parse_languages(['--languages', 'cobol'])
        with self.assertRaises(ValueError):
            repo_nav.parse_languages(['--languages'])


class PathSafetyTests(UnixTestCase):
    def test_redirected_ancestor_refused(self):
        target = self.base / 'target' / 'inner'
        target.mkdir(parents=True)
        (self.base / 'alias').symlink_to(self.base / 'target', target_is_directory=True)
        repo_nav.assert_ordinary_path(target)
        with self.assertRaisesRegex(repo_nav.RepoNavError, 'Redirected'):
            repo_nav.assert_ordinary_path(self.base / 'alias' / 'inner')

    def test_redirected_write_target_refused(self):
        (self.base / 'real').mkdir()
        (self.base / 'link').symlink_to(self.base / 'real', target_is_directory=True)
        repo_nav.assert_ordinary_write_target(self.base / 'real' / 'missing' / 'exclude')
        with self.assertRaisesRegex(repo_nav.RepoNavError, 'redirected Git metadata'):
            repo_nav.assert_ordinary_write_target(self.base / 'link' / 'exclude')


class PreservationGuardTests(UnixTestCase):
    def guarded(self, before, after, action=lambda: 'done'):
        with patch.object(repo_nav, 'tracked_snapshot', side_effect=[before, after]):
            return repo_nav.guarded('/repo', 'stage', action)

    def test_unchanged_returns_result(self):
        self.assertEqual(self.guarded({'W:a': '1'}, {'W:a': '1'}), 'done')

    def test_policy_change_reported_first(self):
        with self.assertRaisesRegex(repo_nav.RepoNavError, '(?s)TRACKED WORKFLOW POLLUTION.*CLAUDE[.]md'):
            self.guarded({'W:CLAUDE.md': '1', 'W:a': '1'}, {'W:CLAUDE.md': '2', 'W:a': '2'})

    def test_new_ignored_policy_file_reported(self):
        with self.assertRaisesRegex(repo_nav.RepoNavError, '(?s)WORKFLOW POLLUTION.*[.]claude/settings[.]local[.]json'):
            self.guarded({}, {'P:.claude/settings.local.json': '1'})

    def test_tracked_change_reported(self):
        with self.assertRaisesRegex(repo_nav.RepoNavError, '(?s)TRACKED FILE POLLUTION.*src/a[.]py'):
            self.guarded({'W:src/a.py': '1'}, {'W:src/a.py': '2'})

    def test_failure_propagates_after_check(self):
        def fail():
            raise repo_nav.RepoNavError('tool failed')
        with self.assertRaisesRegex(repo_nav.RepoNavError, 'tool failed'):
            self.guarded({'W:a': '1'}, {'W:a': '1'}, fail)


class ExcludeTests(UnixTestCase):
    def test_add_once_without_breaking_last_line(self):
        root = self.repository()
        exclude = Path(repo_nav.git_path(str(root), 'info/exclude'))
        exclude.write_text('# existing', encoding='utf-8')
        self.assertEqual(repo_nav.ensure_local_exclude(str(root), exclude, '.serena/'), '.serena/ added')
        self.assertEqual(repo_nav.ensure_local_exclude(str(root), exclude, '.serena/'), '.serena/ present')
        self.assertEqual(exclude.read_text(encoding='utf-8'), '# existing\n.serena/\n')
        self.assertTrue(repo_nav.exclude_state(str(root))['entries']['.serena/'])

    def test_tracked_entry_not_excluded(self):
        root = self.repository(files={'graphify-out/graph.json': '{}'})
        exclude = repo_nav.git_path(str(root), 'info/exclude')
        self.assertEqual(repo_nav.ensure_local_exclude(str(root), exclude, 'graphify-out/'), 'graphify-out/ tracked; not excluded')


class GitFailureTests(unittest.TestCase):
    def test_classification(self):
        cases = {'fatal: detected dubious ownership in repository': 'ownership/trust',
                 'error: Permission denied': 'permission/access',
                 'fatal: not a git repository (or any of the parent directories)': 'Not inside a Git working tree',
                 'something else': 'Git command failure (exit 128)'}
        for detail, kind in cases.items():
            self.assertIn(kind, repo_nav.format_git_failure(128, [detail], 'stage', '/repo'))


class DiscoveryTests(UnixTestCase):
    def test_worktrees_bare_pruned_and_redirected(self):
        scan = self.base / 'scan'
        first = self.repository('scan/first')
        subprocess.check_call(['git', 'init', '-q', '--bare', str(scan / 'mirror.git')])
        self.repository('scan/first/node_modules/dependency')
        self.git(first, 'worktree', 'add', '-q', str(scan / 'second'))
        (scan / 'alias').symlink_to(first, target_is_directory=True)
        candidates, issues, pruned = repo_nav.find_candidates(str(scan), set())
        found = {(c['kind'], Path(c['path']).name) for c in candidates}
        self.assertEqual(found, {('Worktree', 'first'), ('Worktree', 'second'), ('Bare', 'mirror.git')})
        self.assertEqual(issues, [])
        reasons = {Path(p['path']).name: p['reason'] for p in pruned}
        self.assertEqual(reasons['node_modules'], 'generated/heavy directory')
        self.assertEqual(reasons['alias'], 'redirected directory')

    def test_dry_run_reports_without_running_tools(self):
        self.repository('scan/one')
        output = io.StringIO()
        with patch.object(repo_nav, 'ensure_repository', side_effect=AssertionError('ran ensure')), \
                contextlib.redirect_stdout(output):
            code = repo_nav.bootstrap_all(['--dry-run', str(self.base / 'scan'), str(self.base / 'missing')])
        text = output.getvalue()
        self.assertEqual(code, 2)
        self.assertIn('FAILED   ' + str(self.base / 'missing'), text)
        self.assertIn('DRY-RUN  ' + str(self.base / 'scan' / 'one') + '  would run ensure', text)
        self.assertIn('Would process: 1', text)

    def test_bootstrap_usage(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(repo_nav.bootstrap_all([]), 2)
            self.assertEqual(repo_nav.bootstrap_all(['--dry-run', '--dry-run', '.']), 2)
            self.assertEqual(repo_nav.bootstrap_all(['--force', '.']), 2)


class CodexMcpTests(unittest.TestCase):
    def state(self, code, payload):
        output = [json.dumps(payload)] if isinstance(payload, dict) else [payload]
        with patch.object(repo_nav.shutil, 'which', return_value='/bin/codex'), \
                patch.object(repo_nav, 'run', return_value=(code, output)):
            return repo_nav.serena_mcp_state()['state']

    def test_states(self):
        good = {'enabled': True, 'transport': {'type': 'stdio', 'args': ['start-mcp-server', '--project-from-cwd', '--context=codex']}}
        self.assertEqual(self.state(0, good), 'CONFIGURED')
        self.assertEqual(self.state(0, dict(good, enabled=False)), 'DISABLED')
        self.assertEqual(self.state(0, dict(good, transport={'type': 'stdio', 'args': ['start-mcp-server']})), 'INVALID')
        self.assertEqual(self.state(1, "Error: No MCP server named 'serena' found."), 'MISSING')


class EntryPointTests(UnixTestCase):
    def test_usage_errors(self):
        with patch.object(repo_nav, 'assert_supported_dependencies'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(repo_nav.main(['bogus']), 2)
            self.assertEqual(repo_nav.main(['status', 'extra']), 2)
            self.assertEqual(repo_nav.main(['ensure', '--languages', 'cobol']), 2)

    def test_missing_prerequisite_is_reported(self):
        output = io.StringIO()
        with patch.object(repo_nav.shutil, 'which', return_value=None), contextlib.redirect_stdout(output):
            self.assertEqual(repo_nav.main(['status']), 1)
        self.assertIn('repo-nav error: Missing prerequisite: git', output.getvalue())

    def test_launcher_follows_symlink_and_keeps_directory(self):
        tools, bin_dir, work = self.base / 'tools', self.base / 'bin', self.base / 'work'
        python = tools / 'graphifyy' / 'bin' / 'python'
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n')
        bin_dir.mkdir()
        (bin_dir / 'uv').write_text(f'#!/bin/sh\necho "{tools}"\n')
        for script in (python, bin_dir / 'uv'):
            script.chmod(0o755)
        (bin_dir / 'repo-nav').symlink_to(HERE / 'repo-nav')
        work.mkdir()
        env = dict(os.environ, PATH=f'{bin_dir}:/usr/bin:/bin')
        lines = subprocess.check_output([str(bin_dir / 'repo-nav'), 'status'], cwd=work, env=env, text=True).splitlines()
        self.assertEqual(lines, [str(work), '-B', str(HERE / 'repo_nav.py'), 'status'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
