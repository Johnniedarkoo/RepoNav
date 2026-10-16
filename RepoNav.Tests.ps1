param([switch]$Integration)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'TestEnvironment.ps1')
$testEnvironment = Start-RepoNavTestEnvironment
try {
$script:TestStatePython = Join-Path ([string](& uv tool dir)).Trim() 'serena-agent/Scripts/python.exe'
$repoRoot = Split-Path -Parent $PSCommandPath
$sourcePath = Join-Path $repoRoot 'repo-nav.ps1'
$script:RepoNavScriptRoot = $repoRoot
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($sourcePath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw ($parseErrors -join "`n") }
foreach ($functionAst in $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    Invoke-Expression $functionAst.Extent.Text; function Get-StateSafetyPython { $script:TestStatePython }
}
$script:RepoNavGlobalEntries = @('.serena/', 'graphify-out/')

function Assert-Equal {
    param([object]$Expected, [object]$Actual, [string]$Message)
    if ($Expected -cne $Actual) { throw "$Message`nExpected: $Expected`nActual:   $Actual" }
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('reponav-tests-' + [guid]::NewGuid().ToString('N'))
$gitConfig = Join-Path $testRoot 'gitconfig'
$testHome = Join-Path $testRoot 'home'
New-Item -ItemType Directory -Path $testRoot, $testHome -Force | Out-Null
$previousGlobalConfig = $env:GIT_CONFIG_GLOBAL
$previousUserProfile = $env:USERPROFILE
try {
    $env:GIT_CONFIG_GLOBAL = $gitConfig
    $env:USERPROFILE = $testHome

    # Root detection preserves and classifies Git diagnostics without changing trust.
    $script:gitFailureText = ''
    function Invoke-NativeCapture {
        param($FilePath, $Arguments)
        [PSCustomObject]@{ ExitCode = 128; Output = @($script:gitFailureText) }
    }
    foreach ($case in @(
        @('fatal: not a git repository (or any parent)', 'Not inside a Git working tree'),
        @("fatal: detected dubious ownership in repository at '/sample'; safe.directory", 'Git ownership/trust rejection'),
        @("fatal: cannot access '/sample/.git': Permission denied", 'Git permission/access failure'),
        @('fatal: unexpected rev-parse problem', 'Git command failure (exit 128)')
    )) {
        $script:gitFailureText = $case[0]
        $message = ''
        try { Get-RepositoryRoot | Out-Null } catch { $message = $_.Exception.Message }
        if (-not $message.Contains($case[1]) -or -not $message.Contains($case[0]) -or -not $message.Contains('[repository detection]') -or -not $message.Contains((Get-Location).Path)) {
            throw "Git detection lost classification, path or underlying message: $message"
        }
    }
    foreach ($f in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)) { Invoke-Expression $f.Extent.Text; function Get-StateSafetyPython { $script:TestStatePython } }

    # MCP inspection uses the installed Codex parser and never registers/duplicates servers.
    $script:codexPresent = $true
    $script:mcpCalls = @()
    function Get-Command {
        param($Name, $ErrorAction)
        if ($Name -eq 'codex') {
            if ($script:codexPresent) { [PSCustomObject]@{Source='mock-codex'} }
        } else { Microsoft.PowerShell.Core\Get-Command $Name -ErrorAction SilentlyContinue }
    }
    function Invoke-NativeCapture {
        param($FilePath, $Arguments)
        $script:mcpCalls += ,$Arguments
        $script:mcpResult
    }
    $script:mcpResult = [PSCustomObject]@{ ExitCode=1; Output=@("Error: No MCP server named 'serena' found.") }
    Assert-Equal 'MISSING' (Get-SerenaMcpState).State 'Missing Serena MCP not detected.'
    $server = @{name='serena';enabled=$true;transport=@{type='stdio';command='serena';args=@('start-mcp-server','--project-from-cwd','--context=codex')}}
    $script:mcpResult = [PSCustomObject]@{ExitCode=0;Output=@($server | ConvertTo-Json -Depth 6)}
    Assert-Equal 'CONFIGURED' (Get-SerenaMcpState).State 'Enabled Codex-context server not detected.'
    Assert-Equal 'CONFIGURED' (Get-SerenaMcpState).State 'Repeated read changed MCP state.'
    foreach ($call in $script:mcpCalls) { Assert-Equal 'mcp,get,serena,--json' ($call -join ',') 'MCP inspection must be read-only.' }
    $server.transport.type='streamable_http'
    $script:mcpResult.Output=@($server | ConvertTo-Json -Depth 6)
    Assert-Equal 'INVALID' (Get-SerenaMcpState).State 'HTTP server unexpectedly accepted as recommended STDIO setup.'
    $server.transport.type='stdio'
    $server.enabled=$false
    $script:mcpResult.Output=@($server | ConvertTo-Json -Depth 6)
    Assert-Equal 'DISABLED' (Get-SerenaMcpState).State 'Disabled server falsely configured.'
    $server.enabled=$true; $server.transport.args=@('start-mcp-server','--context=claude-code')
    $script:mcpResult.Output=@($server | ConvertTo-Json -Depth 6)
    Assert-Equal 'INVALID' (Get-SerenaMcpState).State 'Wrong launch arguments falsely configured.'
    $script:mcpResult.Output=@('invalid JSON')
    Assert-Equal 'UNAVAILABLE' (Get-SerenaMcpState).State 'Malformed CLI response not handled.'
    $script:codexPresent=$false
    $count=$script:mcpCalls.Count
    Assert-Equal 'UNAVAILABLE' (Get-SerenaMcpState).State 'Missing Codex must not fail local setup.'
    Assert-Equal $count $script:mcpCalls.Count 'Missing Codex still invoked a process.'
    foreach ($f in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)) { Invoke-Expression $f.Extent.Text; function Get-StateSafetyPython { $script:TestStatePython } }
    Remove-Item Function:Get-Command

    $existingExclude = Join-Path $testRoot 'existing-ignore'
    Set-Content -LiteralPath $existingExclude -Value @('*.tmp', '.local-cache/', '.serena/') -Encoding utf8
    & git config --global core.excludesFile $existingExclude
    Ensure-GlobalExcludes | Out-Null
    Ensure-GlobalExcludes | Out-Null
    $existingLines = @(Get-Content -LiteralPath $existingExclude)
    foreach ($entry in @('*.tmp', '.local-cache/', '.serena/', 'graphify-out/')) {
        if (@($existingLines | Where-Object { $_ -ceq $entry }).Count -ne 1) { throw "Expected exactly one preserved/added entry: $entry" }
    }

    Remove-Item -LiteralPath $gitConfig
    $defaultPath = Join-Path $testHome '.config\git\ignore'
    $fresh = Ensure-GlobalExcludes
    Assert-Equal 'OK' $fresh.State 'Missing writable excludes not initialized.'
    Assert-Equal ([IO.Path]::GetFullPath($defaultPath)) $fresh.Path 'Default excludes path was not selected.'
    $configured = (& git config --global --path --get core.excludesFile).Trim()
    Assert-Equal ([IO.Path]::GetFullPath($defaultPath)) ([IO.Path]::GetFullPath($configured)) 'Default path was not configured.'

    Assert-Equal 'OK' (Get-GlobalExcludeState).State 'Accessible exclusions not verified.'
    # Denied reads and writes do not need real ACL changes.
    function Get-ExcludeLines { param($ExcludePath) throw [UnauthorizedAccessException]::new("Access denied: $ExcludePath") }
    $denied=Ensure-GlobalExcludes
    Assert-Equal 'INACCESSIBLE' $denied.State 'Unreadable excludes were not classified.'
    Assert-Equal $false $denied.Ready 'Unreadable excludes falsely ready.'
    Assert-Equal 0 $denied.VerifiedEntries.Count 'Unreadable entries falsely verified.'
    Assert-Equal 'INACCESSIBLE' (Get-GlobalExcludeState).State 'Read-only status threw on denied read.'
    foreach ($f in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)) { Invoke-Expression $f.Extent.Text; function Get-StateSafetyPython { $script:TestStatePython } }
    Set-Content -LiteralPath $defaultPath -Value '.serena/'
    function Set-Content { param($LiteralPath,$Value,$Encoding) throw [UnauthorizedAccessException]::new("Access denied: $LiteralPath") }
    $unwritable=Ensure-GlobalExcludes
    Assert-Equal 'INACCESSIBLE' $unwritable.State 'Unwritable excludes not classified.'
    Assert-Equal $true $unwritable.Readable 'Read permission incorrectly lost.'
    Assert-Equal '.serena/' ($unwritable.VerifiedEntries -join ',') 'Existing verified entries lost.'
    Remove-Item Function:Set-Content
    Ensure-GlobalExcludes | Out-Null
    & git config --global core.excludesFile $testHome
    Assert-Equal 'INVALID' (Ensure-GlobalExcludes).State 'Directory configured as excludes should be invalid.'
    & git config --global core.excludesFile $defaultPath

    function Ensure-LocalExclude { throw 'simulated denied .git write' }
    $warning = Ensure-OptionalLocalExclude -Root $testRoot -ExcludePath (Join-Path $testRoot '.git\info\exclude') -Entry '.gitattributes'
    if ($warning -notmatch '^WARNING:') { throw 'Expected a warning for an unavailable optional local exclusion.' }
    $effective = @(Get-Content -LiteralPath $defaultPath)
    foreach ($entry in @('.serena/', 'graphify-out/')) {
        if (@($effective | Where-Object { $_ -ceq $entry }).Count -ne 1) { throw "Global ignore missing after local write failure: $entry" }
    }
    if (Select-String -LiteralPath $sourcePath -Pattern 'icacls|Set-Acl' -Quiet) { throw 'RepoNav must not attempt ACL repair.' }

    # A denied repository-local metadata write does not fail the navigation stack.
    $script:mockExcludePath = Join-Path $testRoot 'git-info-exclude'
    function Get-ToolInfo { param([string]$Name) [PSCustomObject]@{ Available = $true; Version = 'mock'; Path = 'mock' } }
    function Get-GitPath { param([string]$Root, [string]$PathName) $script:mockExcludePath }
    function Test-TrackedPath { param([string]$Root, [string]$RelativePath) $false }
    function Ensure-Serena { param([string]$Root) 'indexed' }
    function Ensure-Graphify { param([string]$Root) 'graph present' }
    function Ensure-GraphifyHooks { param([string]$Root) 'hooks installed' }
    function Get-GraphifyAttributeState {
        param([string]$Root)
        [PSCustomObject]@{ Exists = $true; Tracked = $false; HasOtherContent = $false; HasGraphifyLine = $true }
    }
    function Ensure-LocalExclude { throw 'simulated denied .git/info/exclude write' }
    function Invoke-GuardedRepositoryStage { param($Root,$Stage,$Action) & $Action }
    $repositoryResult = Invoke-EnsureRepository -Root $testRoot
    Assert-Equal 'indexed' $repositoryResult.SerenaState 'Serena setup did not continue after an optional local exclusion failure.'
    Assert-Equal 'graph present' $repositoryResult.GraphState 'Graphify setup did not continue after an optional local exclusion failure.'
    if (@($repositoryResult.Warnings | Where-Object { $_ -match '^WARNING: optional \.gitattributes exclusion unavailable$' }).Count -ne 1) {
        throw 'Expected the repository-local exclusion failure to be reported as a warning.'
    }

    # Global failures warn and still reach both navigation stages and hooks.
    function Ensure-GlobalExcludes { New-GlobalExcludeState -Path 'mock-denied-ignore' -State 'INACCESSIBLE' -Message 'Access denied' }
    function Get-SerenaMcpState { [PSCustomObject]@{State='MISSING';Path='mock-config';Message='Serena not configured'} }
    $continued = Invoke-EnsureRepository -Root $testRoot -WarningVariable globalWarnings -WarningAction SilentlyContinue
    Assert-Equal 'indexed' $continued.SerenaState 'Denied global excludes blocked Serena.'
    Assert-Equal 'graph present' $continued.GraphState 'Denied global excludes blocked Graphify.'
    Assert-Equal 'hooks installed' $continued.HookState 'Denied global excludes blocked hooks.'
    if (($globalWarnings -join "`n") -notmatch 'global-excludes.*INACCESSIBLE') { throw 'Global warning not emitted.' }
    if (($continued.Warnings -join "`n") -notmatch 'Continuing navigation checks') { throw 'Global warning omitted continuation.' }
    Assert-Equal 'MISSING' $continued.SerenaMcp.State 'Index readiness implied MCP readiness.'

    function Ensure-GlobalExcludes { New-GlobalExcludeState -Path 'mock-unwritable-ignore' -State 'INACCESSIBLE' -Readable $true -VerifiedEntries @('.serena/') -Message 'Access denied writing required entry' }
    $continuedWrite = Invoke-EnsureRepository -Root $testRoot -WarningAction SilentlyContinue
    Assert-Equal 'indexed' $continuedWrite.SerenaState 'Denied global write blocked Serena.'
    Assert-Equal 'graph present' $continuedWrite.GraphState 'Denied global write blocked Graphify.'
    Assert-Equal 'hooks installed' $continuedWrite.HookState 'Denied global write blocked hooks.'
    Assert-Equal $false $continuedWrite.GlobalExclude.Ready 'Incomplete unwritable exclusions claimed ready.'

    # Restore production functions after the exclusion-failure mocks.
    foreach ($functionAst in $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
        Invoke-Expression $functionAst.Extent.Text; function Get-StateSafetyPython { $script:TestStatePython }
    }
    $languageRepo = Join-Path $testRoot 'languages'
    New-Item -ItemType Directory $languageRepo | Out-Null
    & git -C $languageRepo init -q
    Set-Content (Join-Path $languageRepo 'app.py') 'def hello(): return 1'
    & git -C $languageRepo add app.py
    Assert-Equal 'python' ((Get-SerenaLanguages $languageRepo) -join ',') 'Python detection failed.'
    New-Item -ItemType Directory (Join-Path $languageRepo 'vendor'), (Join-Path $languageRepo 'fixtures'), (Join-Path $languageRepo 'results') | Out-Null
    Set-Content (Join-Path $languageRepo 'vendor/incidental.rs') 'fn incidental() {}'
    Set-Content (Join-Path $languageRepo 'fixtures/incidental.go') 'package fixtures'
    Set-Content (Join-Path $languageRepo 'results/incidental.cs') 'class Generated {}'
    Set-Content (Join-Path $languageRepo 'untracked.java') 'class Untracked {}'
    & git -C $languageRepo add vendor fixtures results
    Assert-Equal 'python' ((Get-SerenaLanguages $languageRepo) -join ',') 'Excluded/untracked languages leaked.'
    $script:serenaCalls = @()
    function Invoke-UnattendedCapture {
        param([string]$FilePath, [string[]]$Arguments)
        $script:serenaCalls += ,$Arguments
        [PSCustomObject]@{ ExitCode = 0; Output = @() }
    }
    Ensure-Serena $languageRepo | Out-Null
    Assert-Equal 'project,create' (($script:serenaCalls[0][0..1]) -join ',') 'Expected create.'
    if (($script:serenaCalls[0] -join ' ') -notmatch '--ls python') { throw 'Python creation omitted explicit server.' }
    Set-Content (Join-Path $languageRepo 'app.ts') 'export function hello() {}'
    & git -C $languageRepo add app.ts
    Ensure-Serena $languageRepo | Out-Null
    $polyglotArgs = $script:serenaCalls[1] -join ' '
    if ($polyglotArgs -notmatch '--ls python' -or $polyglotArgs -notmatch '--ls typescript') { throw 'Polyglot creation omitted a server.' }
    New-Item -ItemType Directory (Join-Path $languageRepo '.serena/cache') -Force | Out-Null
    $configPath = Join-Path $languageRepo '.serena/project.yml'
    $template = Join-Path (Split-Path (Split-Path $script:TestStatePython -Parent) -Parent) 'Lib/site-packages/serena/resources/project.template.yml'
    Set-Content $configPath ((Get-Content $template -Raw) -replace 'language_servers:.*', 'language_servers: [python_jedi]')
    Set-Content (Join-Path $languageRepo '.serena/cache/symbols.bin') 'existing'
    $preserved = Get-Content $configPath -Raw
    function Get-SerenaLanguages { throw 'Existing config must not trigger detection.' }
    Assert-Equal 'existing index' (Ensure-Serena $languageRepo) 'Existing index not reused.'
    Assert-Equal $preserved (Get-Content $configPath -Raw) 'Existing config rewritten.'
    Remove-Item (Join-Path $languageRepo '.serena/cache/symbols.bin')
    Ensure-Serena $languageRepo | Out-Null
    Assert-Equal 'project,index' (($script:serenaCalls[2][0..1]) -join ',') 'Existing config should index only.'
    Assert-Equal $preserved (Get-Content $configPath -Raw) 'Index rewrote config.'
    function Get-SerenaLanguages { @() }
    $emptyRepo = Join-Path $testRoot 'empty'
    New-Item -ItemType Directory $emptyRepo | Out-Null
    Assert-Equal 'WARNING — no supported active source language detected' (Ensure-Serena $emptyRepo) 'No-language fallback failed.'
    Assert-Equal 3 $script:serenaCalls.Count 'No-language path invoked Serena.'

    # Real stdin EOF probe uses the exact Serena process wrapper, not a mock.
    $unattendedAst = $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Invoke-UnattendedCapture' }, $true)[0]
    Invoke-Expression $unattendedAst.Extent.Text
    $stdinProbe = Invoke-UnattendedCapture powershell.exe @('-NoProfile', '-Command', 'if ([Console]::ReadLine() -eq $null) { exit 0 } else { exit 1 }')
    Assert-Equal 0 $stdinProbe.ExitCode 'Child stdin was not closed.'

    $script:graphCalls = @()
    function Get-GraphifyState { param($Root) [PSCustomObject]@{state=$script:graphState; semantic=$false} }
    function Invoke-NativeCapture {
        param($FilePath, $Arguments)
        $script:graphCalls += ,$Arguments
        New-Item -ItemType Directory (Join-Path $languageRepo 'graphify-out') -Force | Out-Null
        Set-Content (Join-Path $languageRepo 'graphify-out/graph.json') '{"nodes":[],"links":[]}'
        [PSCustomObject]@{ExitCode=0; Output=@()}
    }
    $script:graphState='missing'
    Assert-Equal 'initialized and built' (Ensure-Graphify $languageRepo) 'Missing graph should build.'
    Assert-Equal 'update,.' ($script:graphCalls[0] -join ',') 'Unexpected initial build command.'
    foreach($state in @('graph current','stale — 3 code files changed','update pending/running','graph invalid')) {
        $script:graphState=$state
        $result=Ensure-Graphify $languageRepo
        if (-not $result.StartsWith($state)) { throw "State lost: $state" }
    }
    Assert-Equal 1 $script:graphCalls.Count 'Existing graph triggered a competing/full build.'
    # Status must not call any ensure, installer, or refresh operation.
    function Get-RepositoryState {
        param($Root)
        [PSCustomObject]@{Root=$Root; SerenaProject=$true; SerenaCache=$true; Hooks=[PSCustomObject]@{Installed=$true}; Attributes=[PSCustomObject]@{HasOtherContent=$false}; GlobalExcludePath='mock'; GlobalExcludeReadable=$true; GlobalExclude=(New-GlobalExcludeState -Path 'mock' -State 'OK' -Readable $true -VerifiedEntries @('.serena/','graphify-out/')); ExcludeState=@{'.serena/'=$true;'graphify-out/'=$true;'.gitattributes'=$true}; ExcludePath='mock'}
    }
    function Get-ToolInfo { param($Name) [PSCustomObject]@{Available=$true; Version='mock'} }
    function Ensure-Graphify { throw 'Status called ensure.' }
    function Ensure-Serena { throw 'Status called ensure.' }
    function Ensure-GraphifyHooks { throw 'Status called installer.' }
    function Get-SerenaMcpState { [PSCustomObject]@{State='CONFIGURED';Path='mock';Message='Enabled'} }
    Write-Status $languageRepo | Out-Null
    function Get-RepositoryState {
        param($Root)
        [PSCustomObject]@{Root=$Root; SerenaProject=$true; SerenaCache=$true; Hooks=[PSCustomObject]@{Installed=$true}; Attributes=[PSCustomObject]@{HasOtherContent=$false}; GlobalExcludePath='denied'; GlobalExcludeReadable=$false; GlobalExclude=(New-GlobalExcludeState -Path 'denied' -State 'INACCESSIBLE' -Message 'Access denied'); ExcludeState=@{'.serena/'=$false;'graphify-out/'=$false;'.gitattributes'=$false}; ExcludePath='mock'}
    }
    $status=Write-Status $languageRepo
    if (($status -join "`n") -notmatch '\.serena/\s+unavailable' -or ($status -join "`n") -notmatch 'global exclusion status unavailable') { throw 'Status claimed or lost unverified exclusions.' }

    Assert-Equal 1 $script:graphCalls.Count 'Status mutated graph.'

    # Tracked-state pollution guard: test through simulated ensure stages in disposable Git repositories.
    foreach ($functionAst in $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)) { Invoke-Expression $functionAst.Extent.Text; function Get-StateSafetyPython { $script:TestStatePython } }
    $script:ProtectedPolicyPaths=@('CLAUDE.md','CLAUDE.local.md','AGENTS.md','AGENTS.override.md','GEMINI.md','.github/copilot-instructions.md')
    function New-GuardRepo {
        param([string]$Path)
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
        & git -C $Path init -q
        & git -C $Path config user.name 'RepoNav Guard Test'
        & git -C $Path config user.email 'reponav-guard@example.invalid'
        Set-Content (Join-Path $Path 'CLAUDE.md') 'project policy'
        Set-Content (Join-Path $Path 'src.py') 'print(1)'
        & git -C $Path add CLAUDE.md src.py
        & git -C $Path commit -qm initial
        return $Path
    }
    function Get-GraphifyPython { (Get-Command python -ErrorAction Stop).Source }
    $guardRepo = New-GuardRepo (Join-Path $testRoot 'guard-clean')
    function Get-ToolInfo { param($Name) [PSCustomObject]@{ Available=$true; Version='mock'; Path='mock' } }
    function Ensure-GlobalExcludes { New-GlobalExcludeState -Path 'mock' -State 'OK' -Readable $true -VerifiedEntries @('.serena/','graphify-out/') }
    function Get-GitPath { param($Root,$PathName) Join-Path $Root '.git\info\exclude' }
    function Ensure-Serena { param($Root) 'indexed' }
    function Ensure-Graphify { param($Root) if($script:injectPolicy){ Add-Content (Join-Path $Root 'CLAUDE.md') '## graphify'; }; 'graph present' }
    function Ensure-GraphifyHooks { param($Root) 'not installed — test' }
    function Get-GraphifyAttributeState { param($Root) [PSCustomObject]@{Exists=$false;Tracked=$false;HasOtherContent=$false;HasGraphifyLine=$false} }
    function Get-SerenaMcpState { [PSCustomObject]@{State='CONFIGURED';Path='mock';Message='ready'} }
    $script:injectPolicy=$false
    $cleanStatusBefore=(& git -C $guardRepo status --porcelain)
    $cleanEnsure=Invoke-EnsureRepository -Root $guardRepo
    Assert-Equal 'graph present' $cleanEnsure.GraphState 'Clean simulated ensure failed.'
    Assert-Equal ($cleanStatusBefore -join "`n") ((& git -C $guardRepo status --porcelain) -join "`n") 'Ensure changed tracked CLAUDE.md or another tracked file.'

    Add-Content (Join-Path $guardRepo 'CLAUDE.md') 'user change'
    Add-Content (Join-Path $guardRepo 'src.py') '# user change'
    $dirtyBefore=(& git -C $guardRepo diff --raw --no-abbrev HEAD)
    $dirtyEnsure=Invoke-EnsureRepository -Root $guardRepo
    Assert-Equal 'graph present' $dirtyEnsure.GraphState 'Pre-existing dirty files caused a false pollution report.'
    Assert-Equal ($dirtyBefore -join "`n") ((& git -C $guardRepo diff --raw --no-abbrev HEAD) -join "`n") 'Pre-existing dirty CLAUDE/source state was not preserved.'
    if ((Get-Content (Join-Path $guardRepo 'CLAUDE.md') -Raw) -notmatch 'user change') { throw 'Dirty CLAUDE.md content was lost.' }

    $pollutedRepo=New-GuardRepo (Join-Path $testRoot 'guard-polluted')
    $script:injectPolicy=$true
    $pollutionMessage=''
    try { Invoke-EnsureRepository -Root $pollutedRepo | Out-Null } catch { $pollutionMessage=$_.Exception.Message }
    $script:injectPolicy=$false
    if ($pollutionMessage -notmatch 'TRACKED WORKFLOW POLLUTION' -or $pollutionMessage -notmatch 'CLAUDE.md' -or $pollutionMessage -notmatch 'Graphify graph') { throw "Policy pollution was not reported with its RepoNav stage: $pollutionMessage" }
    if ((Get-Content (Join-Path $pollutedRepo 'CLAUDE.md') -Raw) -notmatch '## graphify') { throw 'Polluted policy file was automatically reverted.' }

    $newPolicyRepo=New-GuardRepo (Join-Path $testRoot 'guard-new-policy')
    $newPolicyMessage=''
    try {
        Invoke-GuardedRepositoryStage -Root $newPolicyRepo -Stage 'simulated Serena setup' -Action {
            Set-Content (Join-Path $newPolicyRepo 'AGENTS.md') '## graphify'
            & git -C $newPolicyRepo add AGENTS.md
        } | Out-Null
    } catch { $newPolicyMessage=$_.Exception.Message }
    if ($newPolicyMessage -notmatch 'TRACKED WORKFLOW POLLUTION' -or $newPolicyMessage -notmatch 'AGENTS.md') { throw "New staged policy file was not detected: $newPolicyMessage" }
    if (-not (Test-Path (Join-Path $newPolicyRepo 'AGENTS.md'))) { throw 'New policy file was automatically removed.' }

    # A failed polluted repository must not stop bootstrap-all from processing the next repository.
    $bootstrapRoot=Join-Path $testRoot 'guard-bootstrap'
    $bootstrapBad=New-GuardRepo (Join-Path $bootstrapRoot '01-polluted')
    $bootstrapGood=New-GuardRepo (Join-Path $bootstrapRoot '02-clean')
    function Invoke-EnsureRepository {
        param([string]$Root)
        Invoke-GuardedRepositoryStage -Root $Root -Stage 'simulated ensure' -Action {
            if ($Root -like '*01-polluted') { Add-Content (Join-Path $Root 'CLAUDE.md') '## graphify' }
            [PSCustomObject]@{Warnings=@()}
        }
    }
    $bootstrapOutput=@(Invoke-BootstrapAll -BootstrapArguments @($bootstrapRoot) | ForEach-Object { $_.ToString() }) -join "`n"
    if ($bootstrapOutput -notmatch 'FAILED\s+.*01-polluted' -or $bootstrapOutput -notmatch 'READY\s+.*02-clean' -or $bootstrapOutput -notmatch 'Discovered:\s+2') { throw "Bootstrap pollution isolation failed: $bootstrapOutput" }
    Write-Output 'RepoNav focused tests passed.'
}
finally {
    if ($null -eq $previousGlobalConfig) { Remove-Item Env:GIT_CONFIG_GLOBAL -ErrorAction SilentlyContinue } else { $env:GIT_CONFIG_GLOBAL = $previousGlobalConfig }
    if ($null -eq $previousUserProfile) { Remove-Item Env:USERPROFILE -ErrorAction SilentlyContinue } else { $env:USERPROFILE = $previousUserProfile }
    # Cleanup is owned and validated by the outer test environment.

}

if ($Integration) { & (Join-Path $repoRoot 'RepoNav.Integration.Tests.ps1') }

} finally { Stop-RepoNavTestEnvironment $testEnvironment }
