"""Measure B2.2 alone on disposable fixtures; no indexer or installer runs."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from state_safety import inspect_state, StateSafetyError

source = Path(__file__).resolve().parent
package = Path(next(iter(importlib.util.find_spec('serena').submodule_search_locations)))
template = (package / 'resources/project.template.yml').read_bytes()
results=[]
with tempfile.TemporaryDirectory(prefix='state-benchmark-') as temporary:
    for name,tracked,state_files,unsafe in [('small',10,0,False),('medium',1000,20,False),('many-tracked',10000,20,False),('populated-state',1000,1000,False),('unsafe-early-refusal',1000,0,True)]:
        root=Path(temporary)/name; root.mkdir()
        def git(*args): return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL)
        git('init','-q'); git('config','user.name','Synthetic Test'); git('config','user.email','test@example.invalid')
        for number in range(tracked):
            directory=root/('sources'+str(number//1000)); directory.mkdir(exist_ok=True)
            (directory/('file'+str(number)+'.txt')).write_bytes(b'x'*4096)
        git('add','.'); git('commit','-qm','benchmark')
        if state_files:
            (root/'.serena/cache/python').mkdir(parents=True)
            (root/'.serena/project.yml').write_bytes(template)
            (root/'graphify-out/cache').mkdir(parents=True)
            (root/'graphify-out/graph.json').write_text('{"nodes":[],"links":[]}',encoding='utf-8')
            for number in range(state_files):
                (root/'.serena/cache/python'/('data'+str(number)+'.bin')).write_bytes(b'ordinary')
                (root/'graphify-out/cache'/('data'+str(number)+'.json')).write_text('{}',encoding='utf-8')
        if unsafe: (root/'.serena').write_text('malformed state')
        core=[]; metrics={}
        for _ in range(7):
            start=time.perf_counter()
            try: metrics=inspect_state(root,'all','ensure')['metrics']
            except StateSafetyError as error: metrics={'inspections':error.inspections,'hashes':0,'full_repository_scans':0}
            core.append(time.perf_counter()-start)
        counts={'stat':0,'lstat':0,'scandir':0,'file_opens':0}
        original_stat=os.stat; original_lstat=os.lstat; original_scandir=os.scandir; original_open=Path.open
        def counted_stat(*args,**kwargs):
            counts['stat']+=1; return original_stat(*args,**kwargs)
        def counted_lstat(*args,**kwargs):
            counts['lstat']+=1; return original_lstat(*args,**kwargs)
        def counted_scandir(*args,**kwargs):
            counts['scandir']+=1; return original_scandir(*args,**kwargs)
        def counted_open(path,*args,**kwargs):
            counts['file_opens']+=1; return original_open(path,*args,**kwargs)
        with patch.object(os,'stat',counted_stat), patch.object(os,'lstat',counted_lstat), patch.object(os,'scandir',counted_scandir), patch.object(Path,'open',counted_open):
            try: inspect_state(root,'all','ensure')
            except StateSafetyError: pass
        cli=[]
        for _ in range(5):
            start=time.perf_counter()
            result=subprocess.run([sys.executable,'-B',str(source/'state_safety.py'),str(root),'all','ensure'],capture_output=True)
            if result.returncode != (1 if unsafe else 0): raise AssertionError('Unexpected helper result')
            cli.append(time.perf_counter()-start)
        command="""$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; $script:RepoNavScriptRoot='SOURCE';
$t=$null; $e=$null; $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $script:RepoNavScriptRoot 'repo-nav.ps1'),[ref]$t,[ref]$e);
foreach($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)){Invoke-Expression $f.Extent.Text};
$times=@(); foreach($run in 1..5) { $watch=[Diagnostics.Stopwatch]::StartNew(); try { $state=Get-RepositoryStateSafety -Root 'REPO' -Operation ensure } catch { if($_ -notmatch 'STATE SAFETY') { throw } }; $times+=$watch.Elapsed.TotalSeconds }; $cold=[Diagnostics.Stopwatch]::StartNew(); $key=Get-PhysicalRepositoryKey -Path 'REPO'; $coldSeconds=$cold.Elapsed.TotalSeconds; $identityTimes=@(); foreach($run in 1..5) { $watch=[Diagnostics.Stopwatch]::StartNew(); $key=Get-PhysicalRepositoryKey -Path 'REPO'; $identityTimes+=$watch.Elapsed.TotalSeconds }; @{validation=$times;identityCold=$coldSeconds;identityWarm=$identityTimes} | ConvertTo-Json -Compress
""".replace('SOURCE',str(source).replace("'","''")).replace('REPO',str(root).replace("'","''"))
        encoded=base64.b64encode(command.encode('utf-16-le')).decode('ascii')
        result=subprocess.run(['powershell','-NoProfile','-EncodedCommand',encoded],capture_output=True,check=True)
        actual=json.loads(result.stdout)
        results.append({'fixture':name,'tracked_files':tracked,'state_files':state_files*2+(2 if state_files else (1 if unsafe else 0)),
                        'core_median_seconds':statistics.median(core),'helper_process_median_seconds':statistics.median(cli),
                        'powershell_validation_median_seconds':statistics.median(actual['validation']),'physical_identity_cold_seconds':actual['identityCold'],'physical_identity_warm_median_seconds':statistics.median(actual['identityWarm']),'metrics':metrics,'instrumented_filesystem_api_calls':counts})
print(json.dumps({'core_runs':7,'process_runs':5,'b21_comparison':'1000 tracked files of 4096 bytes; 1.8157922 seconds for a full guarded no-op, including two preservation snapshots and uv discovery; not included in these B2.2 timings', 'results':results},indent=2))
