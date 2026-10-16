"""Synthetic state checks and adapter parity; no third-party consumer is executed."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import state_safety as safety

spec = importlib.util.spec_from_file_location('graph_adapter', Path(__file__).with_name('graphify-state.py'))
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='state-safety-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def test_absent(self):
        state=safety.inspect_state(self.root)
        self.assertEqual(state['serena']['category'],'absent')
        self.assertEqual(state['metrics']['hashes'],0)
        self.assertEqual(state['metrics']['full_repository_scans'],0)

    def test_serialized_markers_never_read(self):
        for name in ('raw_document_symbols.pkl','document_symbols.pkl','document_symbols_cache_v23-06-25.pkl','other.PICKLE'):
            self.write('.serena/cache/python/'+name,'inert dummy bytes')
        with patch.object(Path,'read_text',side_effect=AssertionError('Must not read cache bytes')):
            state=safety.inspect_state(self.root,'serena')
        self.assertTrue(state['serena']['serialized'])

    def test_repository_marker_does_not_trust_pickle(self):
        self.write('.serena/.owner','claimed RepoNav ownership')
        self.write('.serena/cache/raw_document_symbols.pkl','inert')
        state=safety.inspect_state(self.root,'serena')
        self.assertEqual(state['serena']['category'],'unverified-executable-cache')

    def test_safe_yaml_malformed(self):
        self.write('.serena/project.yml','[not, a, mapping]')
        with self.assertRaises(safety.StateSafetyError): safety.inspect_state(self.root,'serena')

    def test_configuration_directory(self):
        (self.root/'.serena/project.yml').mkdir(parents=True)
        with self.assertRaises(safety.StateSafetyError): safety.inspect_state(self.root,'serena')

    def test_cache_file_is_malformed(self):
        self.write('.serena/cache','not a directory')
        with self.assertRaises(safety.StateSafetyError): safety.inspect_state(self.root,'serena')

    def test_inaccessible_state(self):
        (self.root/'.serena').mkdir()
        with patch.object(os,'scandir',side_effect=PermissionError()):
            with self.assertRaisesRegex(safety.StateSafetyError,'inaccessible'): safety.inspect_state(self.root,'serena')

    def test_unclear_serena_state(self):
        self.write('.serena/user.txt','preserve')
        self.assertEqual(safety.inspect_state(self.root,'serena')['serena']['category'],'unclear-ownership')
        with self.assertRaises(safety.StateSafetyError): safety.inspect_state(self.root,'serena','ensure')

    def test_unclear_graph_does_not_invoke_native_detection(self):
        self.write('graphify-out/user.txt','preserve')
        with self.assertRaises(safety.StateSafetyError): adapter.inspect(self.root,read_only=False)

    def test_invalid_graph_preserved(self):
        path=self.write('graphify-out/graph.json','invalid json')
        before=path.read_bytes()
        state=adapter.inspect(self.root)
        self.assertEqual(state['state'],'graph invalid')
        self.assertEqual(before,path.read_bytes())

    def test_empty_graph_directory_is_initializable(self):
        (self.root/'graphify-out').mkdir()
        state=safety.inspect_state(self.root,'graphify','ensure')
        self.assertEqual(state['graphify']['category'],'ordinary')
        self.assertEqual(adapter.inspect(self.root)['state'],'missing')

    def modules(self, changed):
        detect=types.ModuleType('graphify.detect')
        detect.detect_incremental=lambda *a,**kw: {'new_files':{'code':changed},'deleted_files':[]}
        detect.classify_file=lambda path:'code'
        watch=types.ModuleType('graphify.watch')
        watch._read_build_excludes=lambda out:[]
        watch._read_build_gitignore=lambda out:True
        self.rebuild_calls=[]
        watch._rebuild_code=lambda root,changed_paths:(self.rebuild_calls.append((root,changed_paths)) or True)
        return {'graphify':types.ModuleType('graphify'),'graphify.detect':detect,'graphify.watch':watch}

    def test_refresh_native_incremental_contract(self):
        self.write('graphify-out/graph.json','{"nodes":[],"links":[]}')
        self.write('source.py','def source(): return 1')
        modules=self.modules(['source.py'])
        with patch.dict(sys.modules,modules), patch.object(sys,'argv',['adapter','refresh',str(self.root)]), patch.object(adapter.importlib.metadata,'version',return_value='0.9.72'):
            self.assertEqual(adapter.main(),0)
        self.assertEqual(self.rebuild_calls,[(self.root,[Path('source.py')])])

    def test_refresh_unchanged_does_not_rebuild(self):
        self.write('graphify-out/graph.json','{"nodes":[],"links":[]}')
        modules=self.modules([])
        with patch.dict(sys.modules,modules), patch.object(sys,'argv',['adapter','refresh',str(self.root)]), patch.object(adapter.importlib.metadata,'version',return_value='0.9.72'):
            self.assertEqual(adapter.main(),0)
        self.assertEqual(self.rebuild_calls,[])

    def test_status_preserves_bytes_and_timestamps(self):
        self.write('graphify-out/graph.json','{"nodes":[],"links":[]}')
        self.write('graphify-out/cache/stat-index.json','{}')
        def before(): return {p.relative_to(self.root).as_posix():(p.read_bytes(),p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        baseline=before()
        with patch.dict(sys.modules,self.modules([])):
            self.assertEqual(adapter.inspect(self.root)['state'],'graph current')
        self.assertEqual(baseline,before())

    def test_nested_junction_adapter_refuses_before_import(self):
        outside=self.root/'outside'; outside.mkdir(); sentinel=outside/'sentinel'; sentinel.write_bytes(b'untouched')
        self.write('graphify-out/graph.json','{"nodes":[],"links":[]}')
        link=self.root/'graphify-out/cache'
        result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],capture_output=True)
        if result.returncode: self.skipTest('Junction creation unavailable')
        try:
            with self.assertRaises(safety.StateSafetyError): adapter.inspect(self.root,read_only=False)
            self.assertEqual(sentinel.read_bytes(),b'untouched')
        finally: link.rmdir()

    def test_nested_file_symlink(self):
        target=self.write('outside/sentinel','untouched')
        self.write('graphify-out/graph.json','{"nodes":[],"links":[]}')
        link=self.root/'graphify-out/manifest.json'
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest('File symlink privilege unavailable')
        try:
            with self.assertRaises(safety.StateSafetyError): adapter.inspect(self.root,read_only=False)
            self.assertEqual(target.read_text(),'untouched')
        finally: link.unlink()

    def test_network_root_refused(self):
        with self.assertRaises(safety.StateSafetyError): safety.inspect_state(chr(92) * 2 + "invalid-host" + chr(92) + "share")

    def test_no_repository_wide_scan(self):
        self.write('source/nested/ordinary.txt','ordinary')
        with patch.object(os,'scandir',side_effect=AssertionError('Unexpected source scan')):
            safety.inspect_state(self.root)

    def test_root_alias_refused(self):
        target=self.root/'target'; target.mkdir(); link=self.root/'alias'
        result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],capture_output=True)
        if result.returncode: self.skipTest('Junction creation unavailable')
        try:
            with self.assertRaises(safety.StateSafetyError): safety.inspect_state(link)
        finally: link.rmdir()


if __name__=='__main__': unittest.main(verbosity=2)
