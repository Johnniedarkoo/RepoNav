"""Disposable byte-level guard cost comparison; no external indexing tools."""
import base64
import json
from pathlib import Path
import statistics
import subprocess
import tempfile
import time
from test_preservation import guard

with tempfile.TemporaryDirectory(prefix='preservation-benchmark-') as directory:
    root=Path(directory)
    def git(*args): return subprocess.check_output(['git','-C',str(root),*args])
    git('init','-q'); git('config','user.name','Synthetic Test'); git('config','user.email','test@example.invalid')
    for number in range(1000):
        folder=root/('folder'+str(number//100)); folder.mkdir(exist_ok=True)
        (folder/('source'+str(number)+'.txt')).write_bytes(b'x'*4096)
    git('add','.'); git('commit','-qm','benchmark')
    old=[]; new=[]; metrics=[]
    for _ in range(5):
        start=time.perf_counter()
        for _ in range(2):
            git('diff','--cached','--raw','--no-abbrev','--no-renames','--')
            git('diff','--raw','--no-abbrev','--no-renames','--')
        old.append(time.perf_counter()-start)
        start=time.perf_counter()
        for _ in range(2):
            output=subprocess.check_output(['python','-B',str(Path(__file__).with_name('preservation-state.py')),str(root)])
            metrics.append(json.loads(output)['metrics'])
        new.append(time.perf_counter()-start)
    source = str(Path(__file__).resolve().parent).replace("'", "''")
    repo = str(root).replace("'", "''")
    command = """$ErrorActionPreference='Stop'; $script:RepoNavScriptRoot='SOURCE';
$t=$null; $e=$null;
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $script:RepoNavScriptRoot 'repo-nav.ps1'),[ref]$t,[ref]$e);
foreach($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) { Invoke-Expression $f.Extent.Text };
$times=@(); foreach($run in 1..5) { $watch=[Diagnostics.Stopwatch]::StartNew(); Invoke-GuardedRepositoryStage -Root 'REPO' -Stage benchmark -Action {}; $times += $watch.Elapsed.TotalSeconds };
$times | ConvertTo-Json -Compress
""".replace('SOURCE', source).replace('REPO', repo)
    encoded = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
    actual = json.loads(subprocess.check_output(['powershell','-NoProfile','-EncodedCommand',encoded]))
    print(json.dumps({'files':1000,'per_snapshot':metrics[-1], 'old_pair_seconds_median':statistics.median(old),
                     'new_pair_seconds_median':statistics.median(new),'actual_guard_seconds_median':statistics.median(actual),'runs':5},indent=2))
