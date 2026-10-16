"""Regressions for the Unix entry point (repo_nav.py and the repo-nav launcher)."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
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


class SerenaTemporaryTests(UnixTestCase):
    def temporary(self, token='a' * 32):
        temporary = self.base / f'reponav-serena-{token}'
        (temporary / 'home' / 'node_modules' / '.bin').mkdir(parents=True)
        (temporary / '.owner').write_text(token)
        return temporary, token

    def test_npm_links_removed_without_following(self):
        temporary, token = self.temporary()
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_text('keep')
        (temporary / 'home' / 'node_modules' / '.bin' / 'tool').symlink_to(outside / 'keep.txt')
        (temporary / 'home' / 'linked-dir').symlink_to(outside, target_is_directory=True)
        repo_nav.remove_serena_temporary(self.base, token)
        self.assertFalse(temporary.exists())
        self.assertEqual((outside / 'keep.txt').read_text(), 'keep')

    def test_links_refused_without_safe_rmtree(self):
        temporary, token = self.temporary()
        (temporary / 'home' / 'node_modules' / '.bin' / 'tool').symlink_to(self.base)
        with patch.object(repo_nav.shutil.rmtree, 'avoids_symlink_attacks', False):
            with self.assertRaisesRegex(repo_nav.RepoNavError, 'cleanup refused'):
                repo_nav.remove_serena_temporary(self.base, token)
        self.assertTrue(temporary.exists())

    def test_foreign_marker_refused(self):
        temporary, token = self.temporary()
        (temporary / '.owner').write_text('someone else')
        with self.assertRaisesRegex(repo_nav.RepoNavError, 'ownership marker'):
            repo_nav.remove_serena_temporary(self.base, token)
        self.assertTrue(temporary.exists())

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


class VersionListTests(unittest.TestCase):
    def test_dependency_check_accepts_tested_versions(self):
        from check_dependencies import check_version
        check_version('0.9.91', '0.9.72|0.9.91', 'graphifyy')
        with self.assertRaisesRegex(ValueError, 'supported version is 0.9.72 or 0.9.91; found 0.9.90'):
            check_version('0.9.90', '0.9.72|0.9.91', 'graphifyy')

    def test_adapter_and_launcher_read_the_same_list(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('graphify_state', HERE / 'graphify-state.py')
        adapter = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'state_safety': type(sys)('state_safety')}):
            sys.modules['state_safety'].inspect_state = None
            spec.loader.exec_module(adapter)
        tested = repo_nav.supported_versions()['tested']
        self.assertEqual(adapter.supported_versions(), sorted({pair['graphifyy'] for pair in tested}))
        self.assertTrue(all(pair['serena-agent'].startswith('1.') for pair in tested), 'Serena 2.x is GPL; not listed')


class HookTests(UnixTestCase):
    def hooks(self, root):
        return Path(repo_nav.git_path(str(root), 'hooks'))

    def test_install_status_uninstall_round_trip(self):
        root = self.repository()
        hooks = self.hooks(root)
        hooks.mkdir(exist_ok=True)
        original = '#!/usr/bin/env bash\necho existing\nexit 0\n'
        (hooks / 'post-commit').write_text(original)
        (hooks / 'post-commit').chmod(0o755)
        lines = repo_nav.hooks_install(str(root))
        self.assertIn('post-commit: added to existing hook', lines)
        self.assertIn('post-merge: installed', lines)
        text = (hooks / 'post-commit').read_text()
        # The block runs before an existing "exit 0" could skip it.
        self.assertLess(text.index(repo_nav.HOOK_BEGIN), text.index('echo existing'))
        self.assertIn('${3:-0}', (hooks / 'post-checkout').read_text())
        self.assertNotIn('${3:-0}', text)
        self.assertTrue(repo_nav.hooks_state(str(root))['installed'])
        self.assertEqual(repo_nav.hooks_install(str(root)).count('post-commit: already installed'), 1)
        repo_nav.hooks_uninstall(str(root))
        self.assertEqual((hooks / 'post-commit').read_text(), original)
        self.assertFalse((hooks / 'post-merge').exists())

    def test_foreign_hook_left_alone(self):
        root = self.repository()
        hooks = self.hooks(root)
        hooks.mkdir(exist_ok=True)
        (hooks / 'post-merge').write_text('#!/usr/bin/env python3\nprint(1)\n')
        lines = repo_nav.hooks_install(str(root))
        self.assertTrue(any(line.startswith('post-merge: skipped') for line in lines))
        self.assertEqual((hooks / 'post-merge').read_text(), '#!/usr/bin/env python3\nprint(1)\n')
        self.assertEqual(repo_nav.hooks_state(str(root))['hooks']['post-merge'], 'foreign hook (not a shell script)')

    def test_hooks_path_inside_worktree_refused(self):
        root = self.repository()
        (root / '.husky').mkdir()
        self.git(root, 'config', 'core.hooksPath', '.husky')
        with self.assertRaisesRegex(repo_nav.RepoNavError, 'inside the work tree'):
            repo_nav.hooks_install(str(root))
        self.assertEqual(list((root / '.husky').iterdir()), [])

    def test_hook_is_inert_without_a_graph(self):
        root = self.repository()
        repo_nav.hooks_install(str(root))
        marker = self.base / 'launched'
        hook = (self.hooks(root) / 'post-commit').read_text()
        hook = hook.replace(shlex_quote(HERE / 'repo-nav'), f'touch {marker};')
        script = self.base / 'hook.sh'
        script.write_text(hook)
        subprocess.check_call(['sh', str(script)], cwd=root)
        self.assertFalse(marker.exists())
        (root / 'graphify-out').mkdir()
        (root / 'graphify-out' / 'graph.json').write_text('{}')
        subprocess.check_call(['sh', str(script)], cwd=root)
        self.assertTrue(marker.exists())


def shlex_quote(path):
    import shlex
    return shlex.quote(str(path))


class RefreshLockTests(UnixTestCase):
    def test_second_refresh_queues_one_more_pass(self):
        root = self.repository()
        passes = []

        def fake_refresh(path, quiet):
            passes.append(quiet)
            if len(passes) == 1:
                # Another trigger arrives while the first pass runs.
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    repo_nav.locked_refresh(path)
                self.assertIn('one more pass queued', output.getvalue())
        with patch.object(repo_nav, 'graphify_refresh', side_effect=fake_refresh), \
                patch.object(repo_nav, 'guarded', side_effect=lambda root, name, action: action()):
            repo_nav.locked_refresh(str(root), quiet=True)
        self.assertEqual(passes, [True, True])
        self.assertFalse(Path(repo_nav.git_path(str(root), 'repo-nav-refresh.lock')).exists())

    def test_stale_lock_taken_over(self):
        root = self.repository()
        lock = Path(repo_nav.git_path(str(root), 'repo-nav-refresh.lock'))
        lock.mkdir()
        (lock / 'pid').write_text('999999999')
        self.assertEqual(repo_nav.acquire_refresh_lock(str(root)), lock)
        repo_nav.release_refresh_lock(lock)

    def test_background_refresh_detaches_from_hook_environment(self):
        root = self.repository()
        log = Path(repo_nav.git_path(str(root), 'repo-nav.log'))
        log.write_bytes(b'x' * (repo_nav.LOG_LIMIT + 10))
        env = dict(os.environ, GIT_INDEX_FILE='/tmp/index.lock', GIT_DIR='/elsewhere')
        with patch.dict(os.environ, env), patch.object(repo_nav, 'git_path', return_value=log), \
                patch.object(repo_nav.subprocess, 'Popen') as popen:
            repo_nav.start_background_refresh(str(root))
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs['start_new_session'])
        self.assertNotIn('GIT_INDEX_FILE', kwargs['env'])
        self.assertNotIn('GIT_DIR', kwargs['env'])
        self.assertEqual(popen.call_args.args[0][-2:], ['refresh', '--quiet'])
        self.assertLess(log.stat().st_size, repo_nav.LOG_LIMIT)


class AgentTests(UnixTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(repo_nav, 'claude_serena', return_value=(None, 'Claude Code CLI unavailable'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_install_merges_and_uninstall_restores(self):
        root = self.repository()
        settings = root / '.claude' / 'settings.local.json'
        settings.parent.mkdir()
        existing = {'permissions': {'allow': ['Bash(ls)']},
                    'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo mine'}]}]}}
        settings.write_text(json.dumps(existing))
        lines = repo_nav.agent_install(str(root))
        self.assertTrue(any('SessionStart hook added' in line for line in lines))
        self.assertIn('Serena MCP: skipped — Claude Code CLI unavailable', lines)
        data = json.loads(settings.read_text())
        self.assertEqual(data['permissions'], existing['permissions'])
        self.assertEqual(len(data['hooks']['SessionStart']), 2)
        self.assertTrue(repo_nav.agent_settings_state(str(root))['installed'])
        self.assertIn('already installed', repo_nav.agent_install(str(root))[0])
        self.assertIn('.claude/settings.local.json', Path(repo_nav.git_path(str(root), 'info/exclude')).read_text())
        repo_nav.agent_uninstall(str(root))
        self.assertEqual(json.loads(settings.read_text()), existing)

    def test_uninstall_deletes_file_it_filled_alone(self):
        root = self.repository()
        repo_nav.agent_install(str(root))
        repo_nav.agent_uninstall(str(root))
        self.assertFalse((root / '.claude' / 'settings.local.json').exists())

    def test_tracked_settings_refused(self):
        root = self.repository(files={'.claude/settings.local.json': '{}'})
        with self.assertRaisesRegex(repo_nav.RepoNavError, 'tracked'):
            repo_nav.agent_install(str(root))

    def context(self, root):
        output = io.StringIO()
        with patch.object(repo_nav, 'start_background_refresh') as background, contextlib.redirect_stdout(output):
            repo_nav.agent_context(str(root))
        return output.getvalue(), background.called

    def test_context_lines(self):
        root = self.repository()
        text, background = self.context(root)
        self.assertIn('repo-nav setup', text)
        self.assertFalse(background)
        (root / 'graphify-out').mkdir()
        (root / 'graphify-out' / 'graph.json').write_text('{}')
        text, background = self.context(root)
        self.assertIn('local code graph', text)
        self.assertIn('graphify query', text)
        self.assertTrue(background)


class SkillTests(UnixTestCase):
    def run_skill(self, *arguments):
        output = io.StringIO()
        with patch.dict(os.environ, {'HOME': str(self.base)}), contextlib.redirect_stdout(output):
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
            os.environ.pop('CODEX_HOME', None)
            self.assertEqual(repo_nav.skill_command(list(arguments)), 0)
        return output.getvalue()

    def test_install_update_and_uninstall(self):
        (self.base / '.openclaw').mkdir()
        text = self.run_skill('install')
        self.assertEqual(text.count(': installed'), 4)
        for folder in ('.agents/skills', '.claude/skills', '.codex/skills', '.openclaw/skills'):
            self.assertEqual((self.base / folder / 'repo-nav' / 'SKILL.md').read_text(),
                             (HERE / 'repo-nav-skill.md').read_text())
        self.assertEqual(self.run_skill('install').count('already installed'), 4)
        (self.base / '.codex/skills/repo-nav/SKILL.md').write_text(repo_nav.SKILL_MARKER + ' old -->')
        self.assertIn('Codex: updated', self.run_skill('install'))
        self.run_skill('uninstall')
        self.assertFalse((self.base / '.claude/skills/repo-nav').exists())

    def test_foreign_skill_kept_and_openclaw_only_when_installed(self):
        foreign = self.base / '.claude/skills/repo-nav/SKILL.md'
        foreign.parent.mkdir(parents=True)
        foreign.write_text('my own skill')
        text = self.run_skill('install')
        self.assertIn('Claude Code: skipped', text)
        self.assertNotIn('OpenClaw', text)
        self.run_skill('uninstall')
        self.assertEqual(foreign.read_text(), 'my own skill')

    def test_skill_frontmatter(self):
        head = (HERE / 'repo-nav-skill.md').read_text().split('---')[1]
        self.assertIn('name: repo-nav', head)
        self.assertIn('description:', head)


class UpgradeTests(unittest.TestCase):
    def upgrade(self, current, check_codes=(0, 0), dry_run=False):
        installs = []
        output = io.StringIO()
        with patch.object(repo_nav.shutil, 'which', return_value='/bin/uv'), \
                patch.object(repo_nav, 'installed_version', side_effect=lambda d: current[d]), \
                patch.object(repo_nav, 'install_tool', side_effect=lambda d, v, p: installs.append((d, v))), \
                patch.object(repo_nav, 'check_dependency', side_effect=[(code, ['check failed']) for code in check_codes]), \
                contextlib.redirect_stdout(output):
            try:
                code = repo_nav.upgrade_tools(dry_run)
            except repo_nav.RepoNavError as error:
                code = str(error)
        return code, installs, output.getvalue()

    def test_moves_to_newest_tested_pair(self):
        target = repo_nav.supported_versions()['tested'][-1]
        code, installs, _ = self.upgrade({'serena-agent': target['serena-agent'], 'graphifyy': '0.0.1'})
        self.assertEqual(code, 0)
        self.assertEqual(installs, [('graphifyy', target['graphifyy'])])

    def test_failed_check_restores_previous_version(self):
        target = repo_nav.supported_versions()['tested'][-1]
        code, installs, _ = self.upgrade({'serena-agent': target['serena-agent'], 'graphifyy': '0.0.1'}, check_codes=(0, 1))
        self.assertIn('previous versions restored', code)
        self.assertEqual(installs, [('graphifyy', target['graphifyy']), ('graphifyy', '0.0.1')])

    def test_dry_run_and_nothing_to_do(self):
        target = repo_nav.supported_versions()['tested'][-1]
        code, installs, text = self.upgrade({'serena-agent': target['serena-agent'], 'graphifyy': '0.0.1'}, dry_run=True)
        self.assertEqual((code, installs), (0, []))
        self.assertIn('Dry run', text)
        code, installs, text = self.upgrade(dict(target))
        self.assertEqual((code, installs), (0, []))
        self.assertIn('nothing to do', text)


class WatchTests(UnixTestCase):
    def test_options(self):
        self.assertEqual(repo_nav.parse_debounce([]), 3.0)
        self.assertEqual(repo_nav.parse_debounce(['--debounce', '1.5']), 1.5)
        for bad in (['--debounce', '0'], ['--debounce', 'x'], ['--fast']):
            with self.assertRaises(ValueError):
                repo_nav.parse_debounce(bad)

    def test_event_source_preference(self):
        with patch.object(repo_nav.shutil, 'which', side_effect=lambda name: '/bin/' + name if name == 'inotifywait' else None):
            self.assertEqual(repo_nav.watch_source('/repo')[0], 'inotifywait')
        with patch.object(repo_nav.shutil, 'which', return_value=None):
            self.assertEqual(repo_nav.watch_source('/repo'), ('polling', None))

    def test_polling_signature_sees_repeated_edits(self):
        root = self.repository(files={'a.py': 'x = 1\n'})
        first = repo_nav.worktree_signature(str(root))
        (root / 'a.py').write_text('x = 2\n')
        second = repo_nav.worktree_signature(str(root))
        time.sleep(0.01)
        (root / 'a.py').write_text('x = 33\n')
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, repo_nav.worktree_signature(str(root)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
