$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'TestEnvironment.ps1')
$context=Start-RepoNavTestEnvironment
try {
    $source=$PSScriptRoot
    $a=$null; $b=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $source 'repo-nav.ps1'),[ref]$a,[ref]$b)
    if($b.Count) { throw 'Parse failed' }
    foreach($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) {Invoke-Expression $f.Extent.Text}
    $script:RepoNavScriptRoot=$source
    $times=@(1..3 | ForEach-Object { (Measure-Command {Assert-SupportedDependencies}).TotalSeconds })
    Write-Output ('Dependency preflight seconds: ' + ($times -join ', '))
    $python=Get-StateSafetyPython
    & $python -B -m unittest -v test_dependencies
    if($LASTEXITCODE) { throw 'Dependency unit tests failed' }
    foreach($name in @('normal','path with spaces',('unicode-'+[char]0x00e9))) {
        $install=Join-Path $context.Root $name
        New-Item -ItemType Directory -Path $install | Out-Null
        foreach($file in @('repo-nav.ps1','repo-nav.cmd','check_dependencies.py','state_safety.py','graphify-state.py','preservation-state.py')) {Copy-Item -LiteralPath (Join-Path $source $file) -Destination $install}
        $repo=Join-Path $context.Root ($name+' repo')
        New-Item -ItemType Directory -Path $repo | Out-Null
        Push-Location -LiteralPath $repo
        try {
            & git init -q
            if($LASTEXITCODE) { throw 'git init failed' }
            $output=@(& (Join-Path $install 'repo-nav.cmd') status)
            if($LASTEXITCODE -ne 0 -or ($output -join "`n") -notmatch 'not initialized') { throw ($output -join "`n") }
            Write-Output "PASS launcher: $name; outside installation; disposable Git repository"
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $install 'repo-nav.ps1') status | Out-Null
            if($LASTEXITCODE) { throw 'Direct script invocation failed' }
        } finally {Pop-Location}
    }
    # Useful error before any repository state operations.
    $oldPath=$env:PATH
    try {
        $env:PATH=Join-Path $context.Root 'empty-path'
        $message=''
        try {Assert-SupportedDependencies} catch {$message=$_.Exception.Message}
        if($message -notmatch 'Missing prerequisite: git') { throw "Unexpected missing dependency message: $message" }
    } finally {$env:PATH=$oldPath}
    Write-Output 'B3.1 launch and dependency checks passed. No indexing or installers invoked.'
} finally {Stop-RepoNavTestEnvironment $context}
