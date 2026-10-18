"""Synthetic preservation regressions; run through the isolated PowerShell runner."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('preservation', Path(__file__).with_name('preservation-state.py'))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def directory_link(link, target):
    """Windows junction or, elsewhere, a directory symlink; False when unavailable."""
    if os.name == 'nt':
        return subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],capture_output=True).returncode == 0
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        return False
    return True


def remove_directory_link(link):
    link.rmdir() if os.name == 'nt' else link.unlink()


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='preservation-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.write('file.txt', 'initial\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args])

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode('utf-8'))

    def state(self):
        return guard.snapshot(self.root)['records']

    def verify(self, change, detected=True):
        before = self.state()
        change()
        self.assertEqual(before != self.state(), detected)

    def test_01_clean_unchanged(self): self.verify(lambda: None, False)
    def test_02_clean_modified(self): self.verify(lambda: self.write('file.txt', 'new'))
    def test_03_dirty_unchanged(self):
        self.write('file.txt', 'dirty'); self.verify(lambda: None, False)
    def test_04_dirty_modified(self):
        self.write('file.txt', 'dirty'); self.verify(lambda: self.write('file.txt', 'again'))
    def test_05_staged_unchanged(self):
        self.write('file.txt', 'staged'); self.git('add', '.'); self.verify(lambda: None, False)
    def test_06_staged_modified(self):
        self.write('file.txt', 'staged'); self.git('add', '.'); self.verify(lambda: self.write('file.txt', 'again'))
    def test_07_both(self):
        self.write('file.txt', 'staged'); self.git('add', '.'); self.write('file.txt', 'dirty')
        self.verify(lambda: self.write('file.txt', 'again'))
    def test_08_deletion(self): self.verify(lambda: (self.root/'file.txt').unlink())
    def test_09_new_agents(self): self.verify(lambda: self.write('AGENTS.md', 'policy'))
    def test_10_claude_modified(self):
        self.write('CLAUDE.md', 'baseline'); self.verify(lambda: self.write('CLAUDE.md', 'new'))
    def test_11_policy_deleted(self):
        self.write('.claude/rules.md', 'baseline'); self.verify(lambda: (self.root/'.claude/rules.md').unlink())
    def test_12_nested_codex(self): self.verify(lambda: self.write('src/.codex/rules/policy.md', 'new'))
    def test_13_policy_unchanged(self):
        self.write('AGENTS.md', 'baseline'); self.verify(lambda: None, False)
    def test_14_generated(self):
        self.verify(lambda: (self.write('.serena/project.yml', 'generated'), self.write('graphify-out/graph.json', '{}')), False)
    def test_15_unicode(self):
        self.write('space caf\u00e9.txt', 'baseline'); self.git('add', '.'); self.git('commit', '-qm', 'unicode')
        self.verify(lambda: self.write('space caf\u00e9.txt', 'again'))
    def test_16_failure(self):
        before=self.state()
        result=subprocess.run([sys.executable, '-B', '-c', 'raise SystemExit(7)'])
        self.assertEqual(result.returncode,7); self.assertEqual(before,self.state())
    def test_17_separate_repositories(self):
        before=self.state(); other=self.root/'second'; other.mkdir()
        subprocess.check_call(['git','-C',str(other),'init','-q'])
        guard.snapshot(other); self.assertEqual(before,self.state())
    def test_18_global_boundary(self):
        config=Path(os.environ['GIT_CONFIG_GLOBAL'])
        before=config.read_bytes()
        self.git('status','--porcelain'); self.state()
        self.assertEqual(before,config.read_bytes())
        self.assertEqual(os.environ['GIT_CONFIG_NOSYSTEM'],'1')
    def test_index_change(self):
        self.write('file.txt','dirty'); self.verify(lambda: self.git('add','.'))
    def test_staged_policy(self): self.verify(lambda: (self.write('AGENTS.md','new'),self.git('add','AGENTS.md')))
    def test_baseline_deletion(self):
        (self.root/'file.txt').unlink(); self.verify(lambda: None,False)
    def test_ignored_policy(self):
        self.write('.git/info/exclude','AGENTS.md\n'); self.verify(lambda: self.write('AGENTS.md','new'))
    def test_case_policy(self): self.verify(lambda: self.write('src/aGeNtS.Md','new'))
    def test_clean_line_endings(self):
        self.git('config','core.autocrlf','true')
        self.write('file.txt','initial\r\n')
        self.assertEqual(self.git('diff','--raw'),b'')
        self.verify(lambda: self.write('file.txt','initial\n'))
        self.assertEqual(self.git('diff','--raw'),b'')
    def test_link_no_traversal(self):
        outside=self.root/'outside'; outside.mkdir(); (outside/'AGENTS.md').write_text('baseline')
        link=self.root/'.codex'
        if not directory_link(link,outside): self.skipTest('Junction capability unavailable')
        try:
            before=self.state()
            self.assertTrue(before['P:.codex'].startswith('link:'))
            self.assertNotIn('P:.codex/AGENTS.md',before)
        finally: remove_directory_link(link)

    def test_raw_diff_reproduction(self):
        self.write('file.txt','dirty one\n')
        before=self.git('diff','--raw','--no-abbrev','--no-renames')
        baseline=self.state()
        subprocess.check_call([sys.executable,'-B','-c','from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b"dirty two longer\\n")',str(self.root/'file.txt')])
        after=self.git('diff','--raw','--no-abbrev','--no-renames')
        self.assertEqual(before,after)  # Original guard compares only these rows.
        self.assertNotEqual(baseline,self.state())
        print('Original raw-diff guard missed second dirty edit; byte guard detected it.')


if __name__=='__main__': unittest.main(verbosity=2)
