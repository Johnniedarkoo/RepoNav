. (Join-Path $PSScriptRoot 'TestEnvironment.ps1')
$testEnvironment = Start-RepoNavTestEnvironment
try {
$ErrorActionPreference = 'Stop'
function Invoke-BoundedEnsure($Source) {
    $info=New-Object Diagnostics.ProcessStartInfo
    $info.FileName='powershell.exe'
    $info.WorkingDirectory=(Get-Location).Path
    $info.Arguments='-NoProfile -ExecutionPolicy Bypass -File "' + $Source + '" ensure'
    $info.UseShellExecute=$false
    $info.CreateNoWindow=$true
    $info.RedirectStandardInput=$true
    $info.RedirectStandardOutput=$true
    $info.RedirectStandardError=$true
    $process=New-Object Diagnostics.Process
    $process.StartInfo=$info
    try {
        [void]$process.Start()
        $process.StandardInput.Close()
        $stdout=$process.StandardOutput.ReadToEndAsync()
        $stderr=$process.StandardError.ReadToEndAsync()
        if(-not $process.WaitForExit(120000)) {
            & taskkill.exe /PID $process.Id /T /F | Out-Null
            throw 'Disposable ensure exceeded 120 seconds'
        }
        Write-Host $stdout.Result
        if($process.ExitCode -ne 0) { throw $stderr.Result }
    } finally { $process.Dispose() }
}

$source = Join-Path $PSScriptRoot 'repo-nav.ps1'
$script:RepoNavScriptRoot = $PSScriptRoot
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
foreach($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)){ Invoke-Expression $f.Extent.Text }
$script:RepoNavGlobalEntries=@('.serena/','graphify-out/')
$root=Join-Path $env:TEMP ('reponav-integration-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $root | Out-Null

try {
foreach($case in @('python','polyglot','unsupported','combined')) {
    $repo=Join-Path $root $case
    New-Item -ItemType Directory $repo | Out-Null
    Push-Location $repo
    try {
    git init -q
    git config user.name 'RepoNav Test'
    git config user.email 'reponav-test@example.invalid'
    if($case -ne 'unsupported') { Set-Content main.py 'def hello(): return 42' }
    if($case -eq 'polyglot') { Set-Content shared.py 'def shared(): return 7' }
    if($case -in @('polyglot','combined')) { Set-Content main.ts 'export function greet(): number { return 42; }' }
    if($case -eq 'unsupported') { Set-Content README.txt 'No supported source.' }
    if($case -eq 'combined') {
        Set-Content CLAUDE.md 'Project Claude policy.'
        Set-Content AGENTS.md 'Project agent policy.'
        New-Item -ItemType Directory src -Force | Out-Null
        Set-Content src/example.py 'def example(): return 5'
    }
    git add .
    git commit -qm initial
    if($case -eq 'combined') {
        $trackedBefore=(& git status --porcelain) -join [Environment]::NewLine
        foreach($run in 1..2) {
            $watch=[Diagnostics.Stopwatch]::StartNew()
            Invoke-BoundedEnsure $source
            Write-Host "Combined run $run seconds: $($watch.Elapsed.TotalSeconds)"
        }
        if($trackedBefore -cne ((& git status --porcelain) -join [Environment]::NewLine)) { throw 'Real ensure changed tracked status, including protected policy files.' }
        if(-not (Test-Path .serena/project.yml) -or -not (Test-Path .serena/cache) -or -not (Test-Path graphify-out/graph.json)) { throw 'Real ensure did not initialize Serena and Graphify state.' }
        $before=(Get-FileHash graphify-out/graph.json).Hash
        Add-Content main.py 'def changed(): return hello()'
        Invoke-BoundedEnsure $source
        if($before -ne (Get-FileHash graphify-out/graph.json).Hash) { throw 'Stale combined ensure rewrote graph' }
        continue
    }
    Write-Host "CASE $case languages: $(Get-SerenaLanguages $repo)"
    $watch=[Diagnostics.Stopwatch]::StartNew()
    Write-Host (Ensure-Serena $repo)
    Write-Host "Serena seconds: $($watch.Elapsed.TotalSeconds)"
    if($case -ne 'unsupported') {
        $config=Get-Content .serena/project.yml -Raw
        $watch.Restart()
        Write-Host (Ensure-Serena $repo)
        if($config -cne (Get-Content .serena/project.yml -Raw)) { throw 'Serena config changed' }
        Write-Host "Serena reuse seconds: $($watch.Elapsed.TotalSeconds)"
    }
    if($case -eq 'polyglot') {
        $watch.Restart(); Write-Host (Ensure-Graphify $repo); Write-Host "Graph initial seconds: $($watch.Elapsed.TotalSeconds)"
        $hash=(Get-FileHash graphify-out/graph.json).Hash
        $watch.Restart(); Write-Host (Ensure-Graphify $repo); Write-Host "Graph reuse seconds: $($watch.Elapsed.TotalSeconds)"
        if($hash -ne (Get-FileHash graphify-out/graph.json).Hash) { throw 'No-change graph rewritten' }
        Set-Content .git/hooks/post-commit "#!/bin/sh`necho project-hook-preserved"
        $attrsBefore=Test-Path .gitattributes
        $hookResult=Ensure-GraphifyHooks $repo
        if($hookResult -notmatch 'no Git-local-only installer') { throw 'RepoNav did not report the unsupported local-only Graphify hook mode.' }
        if((Test-Path .gitattributes) -ne $attrsBefore) { throw 'Hook status created .gitattributes.' }
        if((Get-Content .git/hooks/post-commit -Raw) -notmatch 'project-hook-preserved') { throw 'Existing project hook was changed.' }
        $snapshot=@(Get-ChildItem -Recurse -File -Force | ForEach-Object { $_.FullName + ':' + $_.Length + ':' + $_.LastWriteTimeUtc.Ticks }) -join "`n"
        Write-Status $repo | Out-Null
        $afterStatus=@(Get-ChildItem -Recurse -File -Force | ForEach-Object { $_.FullName + ':' + $_.Length + ':' + $_.LastWriteTimeUtc.Ticks }) -join "`n"
        if($snapshot -cne $afterStatus) { throw 'Real status modified repo' }
        Set-Content graphify-out/.rebuild.lock 'test pending marker'
        if((Ensure-Graphify $repo) -ne 'update pending/running') { throw 'Pending state missed' }
        Invoke-GraphifyRefresh $repo | Out-Null
        if($hash -ne (Get-FileHash graphify-out/graph.json).Hash) { throw 'Pending refresh raced update' }
        Remove-Item graphify-out/.rebuild.lock
        Add-Content main.py @('from shared import shared', 'def caller(): return hello() + shared()')
        $watch.Restart(); Write-Host (Ensure-Graphify $repo); Write-Host "Stale ensure seconds: $($watch.Elapsed.TotalSeconds)"
        $watch.Restart(); Invoke-GraphifyRefresh $repo | Write-Host; Write-Host "Incremental seconds: $($watch.Elapsed.TotalSeconds)"
        graphify explain hello
        $sharedQuery=(& graphify explain shared) -join "`n"
        if($sharedQuery -notmatch 'caller.*calls') { throw 'Changed-to-unchanged cross-file call lost' }
        Remove-Item shared.py
        $deleted=Get-GraphifyState $repo
        if($deleted.state -notlike 'stale*') { throw 'Deleted source not detected' }
        Invoke-GraphifyRefresh $repo | Out-Null
        $removed=(& graphify explain shared) -join "`n"
        if($removed -notmatch 'No node matching') { throw 'Deleted source left stale nodes' }
        Set-Content shared.py 'def shared(): return 7'
        Invoke-GraphifyRefresh $repo | Out-Null
        $watch.Restart(); & powershell -NoProfile -ExecutionPolicy Bypass -File $source ensure
        if($LASTEXITCODE -ne 0) { throw 'Combined ensure failed' }
        Write-Host "Combined ensure seconds: $($watch.Elapsed.TotalSeconds)"
        # Hooks exercise normal commits and actual branch switches; background
        # completion is bounded, while Git operations must return promptly.
        $watch.Restart(); git add main.py; git commit -qm changed
        Write-Host "Commit seconds: $($watch.Elapsed.TotalSeconds)"
        Start-Sleep -Seconds 3
        git switch -qc alternate
        Set-Content main.ts 'export function greet(): number { return 43; }'
        git add main.ts; git commit -qm alternate
        Start-Sleep -Seconds 3
        $watch.Restart(); git switch -q master
        Write-Host "Checkout seconds: $($watch.Elapsed.TotalSeconds)"
        Start-Sleep -Seconds 6
        graphify hook status
        graphify explain greet
    }
    } finally { Pop-Location }
}
} finally {
    # Outer owned environment performs checked cleanup.
}
Write-Host 'RepoNav real integration tests passed.'

} finally { Stop-RepoNavTestEnvironment $testEnvironment }
