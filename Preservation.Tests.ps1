param([switch]$ReproduceOriginal)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'TestEnvironment.ps1')
$context=Start-RepoNavTestEnvironment
try {
    $script:RepoNavScriptRoot=$PSScriptRoot
    $t=$null; $e=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'repo-nav.ps1'),[ref]$t,[ref]$e)
    foreach($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) { Invoke-Expression $f.Extent.Text }
    $root=Join-Path $context.Root 'repository'; New-Item -ItemType Directory $root | Out-Null
    & git -C $root init -q; & git -C $root config user.name 'Synthetic Test'; & git -C $root config user.email 'test@example.invalid'
    $path=Join-Path $root 'file.txt'; Set-Content $path 'baseline'; & git -C $root add .; & git -C $root commit -qm initial
    $installedSnapshot=Get-TrackedStateSnapshot $root
    if(-not $installedSnapshot.ContainsKey('W:file.txt')) { throw 'Installed Python adapter failed' }
    function Get-GraphifyPython { (Get-Command python -ErrorAction Stop).Source }
    Set-Content $path 'dirty'
    if($ReproduceOriginal) {
        # Self-contained audited raw-diff snapshot: release snapshots have no
        # Git history, and HEAD advances after B2.1 is committed.
        function Get-TrackedStateSnapshot {
            param([string]$Root)
            $records=@{}
            foreach($mode in @('I','W')) {
                $arguments=@('diff','--raw','--no-abbrev','--no-renames','--')
                if($mode -eq 'I') { $arguments=@('diff','--cached','--raw','--no-abbrev','--no-renames','--') }
                $result=Invoke-InRepository -Root $Root -Action { Invoke-NativeCapture -FilePath git -Arguments $arguments }
                if($result.ExitCode -ne 0) { throw 'Raw-diff reproduction failed' }
                foreach($lineObject in $result.Output) {
                    $line=$lineObject.ToString(); $tab=$line.IndexOf("`t")
                    if($tab -ge 0) { $records[$mode+':'+$line.Substring($tab+1)]=$line.Substring(0,$tab) }
                }
            }
            return $records
        }
        $rawBefore=(& git -C $root diff --raw --no-abbrev) -join "`n"
        Invoke-GuardedRepositoryStage $root original-mock {
            & python -B -c 'from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(bytes([115,101,99,111,110,100,32,100,105,114,116,121,32,101,100,105,116]))' $path
            if($LASTEXITCODE -ne 0) { throw 'Mock failed' }
        }
        $rawAfter=(& git -C $root diff --raw --no-abbrev) -join "`n"
        if($rawBefore -cne $rawAfter -or (Get-Content $path -Raw) -ne 'second dirty edit') { throw 'Reproduction failed' }
        Write-Output 'Original guard accepted additional dirty-file modification; raw records identical.'
        return
    }
    Invoke-GuardedRepositoryStage $root unchanged { 'unchanged baseline accepted' }
    $message=''; try { Invoke-GuardedRepositoryStage $root dirty { Set-Content $path 'second dirty content' } } catch { $message=$_.Exception.Message }
    if($message -notmatch 'TRACKED FILE POLLUTION' -or $message -notmatch 'file.txt') { throw 'Dirty violation lost' }
    $message=''; try { Invoke-GuardedRepositoryStage $root policy { Set-Content (Join-Path $root 'AGENTS.md') 'new policy'; throw 'tool failed' } } catch { $message=$_.Exception.Message }
    if($message -notmatch 'TRACKED WORKFLOW POLLUTION' -or $message -notmatch 'AGENTS.md') { throw 'Policy violation after tool failure lost' }
    $before=(Get-FileHash $path).Hash
    $message=''; try { Invoke-GuardedRepositoryStage $root failure { throw 'controlled tool failure' } } catch { $message=$_.Exception.Message }
    if($message -notmatch 'controlled tool failure' -or $before -ne (Get-FileHash $path).Hash) { throw 'Failure handling changed files or lost error' }
    Write-Output 'PowerShell guard checks passed: 5'
} finally { Stop-RepoNavTestEnvironment $context }

