[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = 'ensure',

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Arguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:RepoNavScriptRoot = $PSScriptRoot

function Get-StateSafetyPython {
    $location = Invoke-NativeCapture -FilePath uv -Arguments @('tool', 'dir')
    if ($location.ExitCode -ne 0) { throw 'Cannot locate installed Serena Python for read-only safety inspection.' }
    $python = Join-Path ([string]$location.Output[0]).Trim() 'serena-agent\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Installed Serena Python is required for safe configuration inspection.' }
    return $python
}

function Get-RepositoryStateSafety {
    param([string]$Root, [ValidateSet('all','serena','graphify')][string]$Scope='all', [ValidateSet('inspect','ensure','refresh')][string]$Operation='inspect')
    # A dedicated process boundary keeps tool mocks distinct from validation and
    # imports only the helper plus an installed safe YAML reader when necessary.
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = Get-StateSafetyPython
    $arguments = @('-B', (Join-Path $script:RepoNavScriptRoot 'state_safety.py'), $Root, $Scope, $Operation)
    $info.Arguments = (@($arguments | ForEach-Object { '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"' }) -join ' ')
    $info.UseShellExecute=$false; $info.CreateNoWindow=$true
    $info.RedirectStandardInput=$true; $info.RedirectStandardOutput=$true; $info.RedirectStandardError=$true
    $process=New-Object Diagnostics.Process
    $process.StartInfo=$info
    try {
        [void]$process.Start(); $process.StandardInput.Close()
        $stdout=$process.StandardOutput.ReadToEndAsync(); $stderr=$process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        if($process.ExitCode -ne 0) { throw $stderr.Result.Trim() }
        return ($stdout.Result | ConvertFrom-Json)
    } finally { $process.Dispose() }
}

function Assert-OrdinaryRepositoryPath {
    param([string]$Path)
    # Check every ancestor before Resolve-Path can erase evidence of indirection.
    $candidate=[IO.Path]::GetFullPath($Path)
    if($candidate.StartsWith('\\')) { throw 'Network roots are not supported by containment validation.' }
    while($candidate) {
        $item=Get-Item -LiteralPath $candidate -Force -ErrorAction Stop
        if($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Redirected repository/scan root is unsafe.' }
        $parent=[IO.Directory]::GetParent($candidate)
        if($null -eq $parent) { break }
        $candidate=$parent.FullName
    }
}

function Assert-OrdinaryWriteTarget {
    param([string]$Path)
    $full=[IO.Path]::GetFullPath($Path)
    if($full.StartsWith('\\')) { throw 'STATE SAFETY: network metadata target refused.' }
    $chain=New-Object 'Collections.Generic.Stack[string]'
    $candidate=$full
    while($candidate) {
        $chain.Push($candidate); $parent=[IO.Directory]::GetParent($candidate)
        if($null -eq $parent) { break }; $candidate=$parent.FullName
    }
    while($chain.Count) {
        try { $attributes=[IO.File]::GetAttributes($chain.Pop()) }
        catch [IO.FileNotFoundException] { break }
        catch [IO.DirectoryNotFoundException] { break }
        if($attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'STATE SAFETY: redirected Git metadata target refused.' }
    }
}

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $false)][string[]]$Arguments = @()
    )

    $oldAutoRefresh = $env:GRAPHIFY_NO_AUTO_REFRESH
    $env:GRAPHIFY_NO_AUTO_REFRESH = '1'
    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $FilePath @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldErrorActionPreference
        if ($null -eq $oldAutoRefresh) { Remove-Item Env:GRAPHIFY_NO_AUTO_REFRESH -ErrorAction SilentlyContinue } else { $env:GRAPHIFY_NO_AUTO_REFRESH = $oldAutoRefresh }
    }
    [PSCustomObject]@{
        Output   = $output
        ExitCode = $exitCode
    }
}

function Invoke-InRepository {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    $oldHashSeed = $env:PYTHONHASHSEED
    $env:PYTHONHASHSEED = '0'
    Push-Location -LiteralPath $Root
    try {
        & $Action
    }
    finally {
        Pop-Location
        if ($null -eq $oldHashSeed) {
            Remove-Item Env:PYTHONHASHSEED -ErrorAction SilentlyContinue
        }
        else {
            $env:PYTHONHASHSEED = $oldHashSeed
        }
    }
}

function Get-RepositoryRoot {
    Assert-OrdinaryRepositoryPath -Path (Get-Location).Path
    $result = Invoke-NativeCapture -FilePath 'git' -Arguments @('rev-parse', '--show-toplevel')
    if ($result.ExitCode -ne 0 -or $result.Output.Count -eq 0) {
        throw (Format-GitFailure -Result $result -Stage 'repository detection' -Path (Get-Location).Path)
    }

    return (Resolve-Path -LiteralPath ([string]$result.Output[0].ToString().Trim())).Path
}

function Format-GitFailure {
    param($Result, [string]$Stage, [string]$Path)
    $detail = ($Result.Output | ForEach-Object { $_.ToString() }) -join "`n"
    $kind = if ($detail -match 'detected dubious ownership|safe\.directory') { 'Git ownership/trust rejection' }
        elseif ($detail -match 'permission denied|access (is )?denied|operation not permitted|Toegang geweigerd') { 'Git permission/access failure' }
        elseif ($detail -match 'not a git repository|must be run in a work tree') { 'Not inside a Git working tree' }
        else { "Git command failure (exit $($Result.ExitCode))" }
    return "[$Stage] $kind at $Path. Stopped.`n$detail"
}

function Invoke-NavigationStage {
    param([string]$Stage, [string]$Path, [scriptblock]$Action)
    try { & $Action }
    catch { throw "[$Stage] Path: $Path. Stopped.`n$($_.Exception.Message)" }
}

function Get-GitPath {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$PathName)

    $result = Invoke-InRepository -Root $Root -Action {
        Invoke-NativeCapture -FilePath 'git' -Arguments @('rev-parse', '--git-path', $PathName)
    }
    if ($result.ExitCode -ne 0 -or $result.Output.Count -eq 0) {
        throw (Format-GitFailure -Result $result -Stage "repository detection: git-path $PathName" -Path $Root)
    }

    $candidate = ([string]$result.Output[0].ToString().Trim())
    if ([IO.Path]::IsPathRooted($candidate)) {
        Assert-OrdinaryWriteTarget -Path $candidate
        return $candidate
    }
    $target=Join-Path $Root $candidate
    Assert-OrdinaryWriteTarget -Path $target
    return $target
}

function Test-TrackedPath {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$RelativePath)

    $result = Invoke-InRepository -Root $Root -Action {
        Invoke-NativeCapture -FilePath 'git' -Arguments @('ls-files', '--', $RelativePath)
    }
    return ($result.ExitCode -eq 0 -and $result.Output.Count -gt 0)
}

$script:ProtectedPolicyPaths = @(
    'CLAUDE.md', 'CLAUDE.local.md', 'AGENTS.md', 'AGENTS.override.md', 'GEMINI.md',
    '.github/copilot-instructions.md'
)

function Test-ProtectedPolicyPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $normalized = $Path.Replace('\', '/')
    $parts = $normalized.Split('/')
    if ($parts[-1] -in @('CLAUDE.md','CLAUDE.local.md','AGENTS.md','AGENTS.override.md','GEMINI.md')) { return $true }
    if ($normalized -match '(^|/)(\.claude|\.codex|\.cursor)(/|$)|(^|/)\.github/(instructions(/|$)|copilot-instructions\.md$)') { return $true }
    return $false
}

function Get-TrackedStateSnapshot {
    param([Parameter(Mandatory = $true)][string]$Root)
    $helper = Join-Path $script:RepoNavScriptRoot 'preservation-state.py'
    $result = Invoke-NativeCapture -FilePath (Get-GraphifyPython) -Arguments @('-B', $helper, $Root)
    if ($result.ExitCode -ne 0) { throw "Preservation snapshot failed at $Root. $($result.Output -join ' ')" }
    $snapshot = ($result.Output -join "`n") | ConvertFrom-Json
    $records = @{}
    foreach ($property in $snapshot.records.PSObject.Properties) {
        if ($records.ContainsKey($property.Name)) { throw 'Ambiguous case-insensitive preservation path' }
        $records[$property.Name] = $property.Value
    }
    return $records
}

function Invoke-GuardedRepositoryStage {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Stage,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    $before = Get-TrackedStateSnapshot -Root $Root
    $result = $null; $failure = $null
    try { $result = & $Action } catch { $failure = $_ }
    $after = Get-TrackedStateSnapshot -Root $Root
    $keys = @($after.Keys) + @($before.Keys) | Select-Object -Unique
    $changed = @(); $changedTracked = @()
    foreach ($key in $keys) {
        $oldValue = if ($before.ContainsKey($key)) { [string]$before[$key] } else { $null }
        $newValue = if ($after.ContainsKey($key)) { [string]$after[$key] } else { $null }
        if ($oldValue -cne $newValue) {
            $relative = $key.Substring(2)
            $changedTracked += $relative
            if (Test-ProtectedPolicyPath -Path $relative) { $changed += $relative }
        }
    }
    if ($changed.Count -gt 0) {
        throw ("TRACKED WORKFLOW POLLUTION`nRepoNav stage: {0}`nRepository: {1}`nAffected path(s): {2}`nNo files were reverted; inspect and repair the change before continuing." -f $Stage, $Root, (@($changed | Sort-Object -Unique) -join ', '))
    }
    if ($changedTracked.Count -gt 0) {
        throw ("TRACKED FILE POLLUTION`nRepoNav stage: {0}`nRepository: {1}`nAffected tracked path(s): {2}`nNo files were reverted; inspect and repair the change before continuing." -f $Stage, $Root, (@($changedTracked | Sort-Object -Unique) -join ', '))
    }
    if ($null -ne $failure) { throw $failure }
    return $result
}
function Get-ExcludeLines {
    param([Parameter(Mandatory = $true)][string]$ExcludePath)

    if (-not (Test-Path -LiteralPath $ExcludePath)) {
        return @()
    }
    return @(Get-Content -LiteralPath $ExcludePath -ErrorAction Stop)
}

$script:RepoNavGlobalEntries = @('.serena/', 'graphify-out/')

function Get-GlobalExcludePath {
    $configured = Invoke-NativeCapture -FilePath 'git' -Arguments @('config', '--global', '--path', '--get', 'core.excludesFile')
    if ($configured.ExitCode -notin @(0, 1)) {
        throw "Cannot read Git global core.excludesFile: $($configured.Output -join "`n")"
    }
    if ($configured.ExitCode -eq 0 -and $configured.Output.Count -gt 0) {
        $candidate = ([string]$configured.Output[0]).Trim()
        if (-not [IO.Path]::IsPathRooted($candidate)) {
            $candidate = Join-Path $HOME $candidate
        }
        return [IO.Path]::GetFullPath($candidate)
    }

    $homePath = $env:USERPROFILE
    if ([string]::IsNullOrWhiteSpace($homePath)) { $homePath = $HOME }
    if ([string]::IsNullOrWhiteSpace($homePath)) { $homePath = [Environment]::GetFolderPath('UserProfile') }
    return [IO.Path]::GetFullPath((Join-Path $homePath '.config\git\ignore'))
}

function Get-GlobalExcludeState {
    $path = '(unresolved core.excludesFile)'
    try {
        $path = Get-GlobalExcludePath
        if (-not (Test-Path -LiteralPath $path -ErrorAction Stop)) {
            return New-GlobalExcludeState -Path $path -State 'MISSING' -Message 'File does not exist.'
        }
        if (-not (Test-Path -LiteralPath $path -PathType Leaf -ErrorAction Stop)) {
            return New-GlobalExcludeState -Path $path -State 'INVALID' -Message 'Configured path is not a file.'
        }
        $lines = @(Get-ExcludeLines -ExcludePath $path)
        $verified = @($script:RepoNavGlobalEntries | Where-Object { $entry = $_; @($lines | Where-Object { $_.Trim() -eq $entry }).Count -gt 0 })
        $state = if ($verified.Count -eq $script:RepoNavGlobalEntries.Count) { 'OK' } else { 'MISSING' }
        return New-GlobalExcludeState -Path $path -State $state -Readable $true -VerifiedEntries $verified -Message 'Required entries checked.'
    }
    catch { return New-GlobalExcludeFailure -Path $path -ErrorRecord $_ }
}

function New-GlobalExcludeState {
    param([string]$Path, [string]$State, [bool]$Readable = $false, [string[]]$VerifiedEntries = @(), [string]$Message = '')
    [PSCustomObject]@{ Path = $Path; State = $State; Readable = $Readable; VerifiedEntries = @($VerifiedEntries); Entries = $script:RepoNavGlobalEntries; Ready = ($State -eq 'OK'); Message = $Message }
}

function New-GlobalExcludeFailure {
    param([string]$Path, $ErrorRecord)
    $message = $ErrorRecord.Exception.Message
    $state = if ($ErrorRecord.Exception -is [UnauthorizedAccessException] -or $message -match 'denied|geweigerd|not permitted') { 'INACCESSIBLE' } else { 'INVALID' }
    New-GlobalExcludeState -Path $Path -State $state -Message $message
}

function Format-GlobalExcludeWarning {
    param($State)
    "[global-excludes] $($State.State): $($State.Path)`n$($State.Message)`nGlobal exclusion status unavailable or incomplete; .serena/ and graphify-out/ may appear untracked in this sandbox. Continuing navigation checks."
}

function Ensure-GlobalExcludes {
    $state = Get-GlobalExcludeState
    if ($state.State -in @('INACCESSIBLE', 'INVALID')) { return $state }
    $path = $state.Path
    try {
        $configured = Invoke-NativeCapture -FilePath 'git' -Arguments @('config', '--global', '--get', 'core.excludesFile')
        if ($configured.ExitCode -notin @(0, 1)) { throw ($configured.Output -join "`n") }
        if ($configured.ExitCode -eq 1) {
            $configResult = Invoke-NativeCapture -FilePath 'git' -Arguments @('config', '--global', 'core.excludesFile', $path)
            if ($configResult.ExitCode -ne 0) { throw "Could not configure Git global core.excludesFile: $($configResult.Output -join "`n")" }
        }

        $lines = @(Get-ExcludeLines -ExcludePath $path)
        $updated = New-Object 'System.Collections.Generic.List[string]'
        $seen = @{}
        foreach ($line in $lines) {
            $trimmed = $line.Trim()
            if ($trimmed -in $script:RepoNavGlobalEntries) {
                if ($seen.ContainsKey($trimmed)) { continue }
                $seen[$trimmed] = $true
            }
            [void]$updated.Add([string]$line)
        }
        foreach ($entry in $script:RepoNavGlobalEntries) {
            if (-not $seen.ContainsKey($entry)) { [void]$updated.Add($entry) }
        }

        $changed = ($updated.Count -ne $lines.Count)
        if (-not $changed) {
            for ($i = 0; $i -lt $lines.Count; $i++) {
                if ([string]$lines[$i] -cne $updated[$i]) { $changed = $true; break }
            }
        }
        if ($changed) {
            $parent = Split-Path -Parent $path
            if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
            Set-Content -LiteralPath $path -Value $updated.ToArray() -Encoding utf8
        }

        return Get-GlobalExcludeState
    }
    catch {
        $failure = New-GlobalExcludeFailure -Path $path -ErrorRecord $_
        $failure.Readable = $state.Readable
        $failure.VerifiedEntries = $state.VerifiedEntries
        return $failure
    }
}

function Ensure-LocalExclude {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ExcludePath,
        [Parameter(Mandatory = $true)][string]$Entry
    )

    Assert-OrdinaryWriteTarget -Path $ExcludePath
    if (Test-TrackedPath -Root $Root -RelativePath $Entry.TrimEnd('/')) {
        return "$Entry tracked; not excluded"
    }

    $lines = Get-ExcludeLines -ExcludePath $ExcludePath
    if (@($lines | Where-Object { $_.Trim() -eq $Entry }).Count -gt 0) {
        return "$Entry present"
    }

    if (-not (Test-Path -LiteralPath $ExcludePath)) {
        New-Item -ItemType File -Path $ExcludePath -Force | Out-Null
    }
    Add-Content -LiteralPath $ExcludePath -Value $Entry -Encoding UTF8
    return "$Entry added"
}

function Ensure-OptionalLocalExclude {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ExcludePath,
        [Parameter(Mandatory = $true)][string]$Entry
    )
    try {
        return Ensure-LocalExclude -Root $Root -ExcludePath $ExcludePath -Entry $Entry
    }
    catch {
        return "WARNING: optional $Entry exclusion unavailable"
    }
}

function Get-ToolInfo {
    param([Parameter(Mandatory = $true)][string]$Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        return [PSCustomObject]@{ Available = $false; Version = 'missing'; Path = '' }
    }

    $versionResult = Invoke-NativeCapture -FilePath $command.Source -Arguments @('--version')
    $version = 'available'
    if ($versionResult.Output.Count -gt 0) {
        $version = ([string]$versionResult.Output[0]).Trim()
    }
    return [PSCustomObject]@{
        Available = ($versionResult.ExitCode -eq 0 -or $Name -eq 'rg')
        Version   = $version
        Path      = $command.Source
    }
}

function Assert-SupportedDependencies {
    foreach ($name in @('git','uv','rg','serena','graphify')) {
        if (-not (Get-Command $name -ErrorAction SilentlyContinue)) { throw "Missing prerequisite: $name. See INSTALL.md. No software was installed." }
    }
    foreach ($spec in @(@('serena-agent','1.7.0','serena',(Get-StateSafetyPython)), @('graphifyy','0.9.72','graphify',(Get-GraphifyPython)))) {
        $launcher = (Get-Command $spec[2] -ErrorAction Stop).Source
        $result = Invoke-NativeCapture -FilePath $spec[3] -Arguments @('-B',(Join-Path $script:RepoNavScriptRoot 'check_dependencies.py'),$spec[0],$spec[1],$spec[2],$launcher)
        if ($result.ExitCode -ne 0) { throw ($result.Output -join "`n") }
    }

}

function Get-GraphifyAttributeState {
    param([Parameter(Mandatory = $true)][string]$Root)

    $attributePath = Join-Path $Root '.gitattributes'
    $attributeLine = 'graphify-out/graph.json merge=graphify'
    $tracked = Test-TrackedPath -Root $Root -RelativePath '.gitattributes'
    $exists = Test-Path -LiteralPath $attributePath
    $hasGraphifyLine = $false
    $hasOtherContent = $false

    if ($exists) {
        $lines = @(Get-Content -LiteralPath $attributePath)
        $hasGraphifyLine = @($lines | Where-Object { $_.Trim() -eq $attributeLine }).Count -gt 0
        $hasOtherContent = @($lines | Where-Object {
                $trimmed = $_.Trim()
                $trimmed.Length -gt 0 -and $trimmed -notmatch '^#' -and $trimmed -ne $attributeLine
            }).Count -gt 0
    }

    [PSCustomObject]@{
        Path            = $attributePath
        AttributeLine   = $attributeLine
        Tracked         = $tracked
        Exists          = $exists
        HasGraphifyLine = $hasGraphifyLine
        HasOtherContent = $hasOtherContent
    }
}

function Test-GraphifyRootMarker {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$MarkerPath)

    $marker = $MarkerPath.Trim()
    if ($marker.Length -eq 0) {
        return $false
    }
    if (-not [IO.Path]::IsPathRooted($marker)) {
        $marker = Join-Path $Root $marker
    }
    try {
        return ((Resolve-Path -LiteralPath $marker).Path -eq $Root)
    }
    catch {
        return $false
    }
}

function Get-HookStatus {
    param([Parameter(Mandatory = $true)][string]$Root)
    $safety = Get-RepositoryStateSafety -Root $Root -Scope graphify -Operation inspect

    $result = Invoke-InRepository -Root $Root -Action {
        Invoke-NativeCapture -FilePath 'graphify' -Arguments @('hook', 'status')
    }
    $text = ($result.Output -join "`n")
    [PSCustomObject]@{
        ExitCode = $result.ExitCode
        Text     = $text
        Installed = ($result.ExitCode -eq 0 -and
            $text -match 'post-commit:\s+installed' -and
            $text -match 'post-checkout:\s+installed' -and
            $text -match 'merge driver:\s+registered')
    }
}

function Invoke-SafeSerenaIndex {
    param([string]$Root, [string[]]$Arguments)
    $base=[IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    Assert-OrdinaryRepositoryPath -Path $base
    $token=[guid]::NewGuid().ToString('N')
    $temporary=Join-Path $base ('reponav-serena-' + $token)
    $serenaTemporaryHome=Join-Path $temporary 'home'
    New-Item -ItemType Directory -Path $temporary,$serenaTemporaryHome | Out-Null
    Set-Content -LiteralPath (Join-Path $temporary '.owner') -Value $token
    $oldHome=$env:SERENA_HOME
    try {
        $prepare = & (Get-StateSafetyPython) -B (Join-Path $script:RepoNavScriptRoot 'state_safety.py') prepare-home $Root $serenaTemporaryHome 2>&1
        if($LASTEXITCODE -ne 0) { throw ($prepare -join "`n") }
        $env:SERENA_HOME=$serenaTemporaryHome
        # Repeat project-state checks immediately before the consumer, using the
        # prepared global configuration rather than the user's registration list.
        $check=Get-RepositoryStateSafety -Root $Root -Scope serena -Operation ensure
        if($check.serena.serialized) { throw 'STATE SAFETY: executable cache appeared before Serena invocation.' }
        return (Invoke-UnattendedCapture -FilePath 'serena' -Arguments $Arguments)
    } finally {
        if($null -eq $oldHome) { Remove-Item Env:SERENA_HOME -ErrorAction SilentlyContinue } else { $env:SERENA_HOME=$oldHome }
        $resolved=[IO.Path]::GetFullPath($temporary)
        if($resolved -cne (Join-Path $base ('reponav-serena-' + $token))) { throw 'Unsafe Serena temporary cleanup target' }
        Assert-OrdinaryRepositoryPath -Path $resolved
        $marker=Get-Item -LiteralPath (Join-Path $resolved '.owner') -Force
        if($marker.Attributes -band [IO.FileAttributes]::ReparsePoint -or (Get-Content -LiteralPath $marker.FullName -Raw).Trim() -cne $token) { throw 'Unsafe Serena temporary ownership marker' }
        $pending=New-Object 'Collections.Generic.Stack[string]'; $pending.Push($resolved)
        while($pending.Count) {
            foreach($child in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
                if($child.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked Serena temporary artifact retained; cleanup refused' }
                if($child.PSIsContainer) { $pending.Push($child.FullName) }
            }
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

function Ensure-Serena {
    param([Parameter(Mandatory = $true)][string]$Root)

    $safety = Get-RepositoryStateSafety -Root $Root -Scope serena -Operation ensure
    if ($safety.serena.serialized) { return 'WARNING — unverified executable Serena cache preserved; indexing skipped. Review provenance and prepare a separate clean state before retrying. Independent MCP launches are not protected.' }
    if ($safety.serena.config -and -not $safety.serena.cache_files -and -not $safety.serena.index_safe) { return 'WARNING — existing Serena configuration requires migration; indexing skipped to preserve it. Review and migrate it explicitly before retrying.' }
    $projectDir = Join-Path $Root '.serena'
    $projectConfig = Join-Path $projectDir 'project.yml'
    if ($safety.serena.config -and $safety.serena.cache_files -gt 0) { return 'existing index' }

    if (Test-Path -LiteralPath $projectConfig) {
        $result = Invoke-SafeSerenaIndex -Root $Root -Arguments @('project', 'index', $Root)
        $action = 'indexed'
    }
    else {
        $languages = @(Get-SerenaLanguages -Root $Root)
        if ($languages.Count -eq 0) { return 'WARNING — no supported active source language detected' }
        $createArgs = @('project', 'create', $Root, '--index')
        foreach ($language in $languages) { $createArgs += @('--ls', $language) }
        $result = Invoke-SafeSerenaIndex -Root $Root -Arguments $createArgs
        $action = 'initialized and indexed'
    }

    if ($result.ExitCode -ne 0) {
        $tail = @($result.Output | Select-Object -Last 6) -join "`n"
        throw "Serena failed to initialize this repository.`n$tail"
    }
    return $action
}

function Get-SerenaLanguages {
    param([Parameter(Mandatory = $true)][string]$Root)
    # Default server IDs/extensions verified against installed Serena 1.7.0.
    # Data/config formats do not enable a server; one real source file suffices
    # after excluding generated/vendor/fixture paths and generated filenames.
    $mapping = @{
        python = @('.py', '.pyi'); typescript = @('.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs')
        go = @('.go'); rust = @('.rs'); java = @('.java'); kotlin = @('.kt', '.kts')
        cpp = @('.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hxx', '.cu', '.mm')
        csharp = @('.cs'); powershell = @('.ps1', '.psm1'); ruby = @('.rb', '.erb')
        php = @('.php', '.phtml'); dart = @('.dart'); bash = @('.sh', '.bash')
        swift = @('.swift'); lua = @('.lua'); luau = @('.luau'); zig = @('.zig')
        fsharp = @('.fs', '.fsx', '.fsi'); scala = @('.scala'); elixir = @('.ex', '.exs')
        vue = @('.vue'); svelte = @('.svelte'); terraform = @('.tf', '.tfvars')
    }
    $files = Invoke-InRepository -Root $Root -Action {
        Invoke-NativeCapture -FilePath git -Arguments @('-c', 'core.quotePath=false', 'ls-files')
    }
    if ($files.ExitCode -ne 0) { throw (Format-GitFailure -Result $files -Stage 'Serena project/index: tracked source detection' -Path $Root) }
    $found = @{}
    foreach ($path in $files.Output) {
        $path = [string]$path
        if ($path -match '(^|/)(node_modules|\.venv|venv|vendor|third[_-]party|external|generated|results|graphify-out|\.serena|build|dist|fixtures?|testdata)(/|$)' -or
            $path -match '(\.min\.js|\.generated\.[^/]+|_pb2\.py|\.pb\.go)$' -or
            -not (Test-Path -LiteralPath (Join-Path $Root $path) -PathType Leaf)) { continue }
        $extension = [IO.Path]::GetExtension($path).ToLowerInvariant()
        foreach ($language in $mapping.Keys) {
            if ($extension -in $mapping[$language]) { $found[$language] = $true }
        }
    }
    $found.Keys | Sort-Object
}

function Invoke-UnattendedCapture {
    param([string]$FilePath, [string[]]$Arguments)
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = (Get-Command $FilePath -ErrorAction Stop).Source
    # Windows argv quoting, including trailing backslashes before closing quote.
    $info.Arguments = (@($Arguments | ForEach-Object {
        '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
    }) -join ' ')
    $info.WorkingDirectory = (Get-Location).Path
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $process.StandardInput.Close()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        [PSCustomObject]@{ ExitCode = $process.ExitCode; Output = @(($stdout.Result + "`n" + $stderr.Result) -split "`r?`n") }
    }
    finally { $process.Dispose() }
}

function Get-SerenaMcpState {
    $userHome = [Environment]::GetFolderPath('UserProfile')
    if ([string]::IsNullOrWhiteSpace($userHome)) { $userHome = $env:USERPROFILE }
    $configHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $userHome '.codex' }
    $path = Join-Path $configHome 'config.toml'
    try {
        $codex = Get-Command codex -ErrorAction SilentlyContinue
        if ($null -eq $codex) {
            return [PSCustomObject]@{ State = 'UNAVAILABLE'; Path = $path; Message = 'Codex executable unavailable; local indexing remains usable.' }
        }
        $result = Invoke-NativeCapture -FilePath $codex.Source -Arguments @('mcp', 'get', 'serena', '--json')
        if ($result.ExitCode -ne 0) {
            $detail = $result.Output -join "`n"
            $state = if ($detail -match "No MCP server named|not found|does not exist") { 'MISSING' } else { 'UNAVAILABLE' }
            return [PSCustomObject]@{ State = $state; Path = $path; Message = $detail }
        }
        $server = ($result.Output -join "`n") | ConvertFrom-Json
        if (-not $server.enabled) {
            return [PSCustomObject]@{ State = 'DISABLED'; Path = $path; Message = 'Serena server disabled in effective Codex configuration.' }
        }
        $transport = $server.transport
        if ($transport.type -ne 'stdio') {
            return [PSCustomObject]@{ State = 'INVALID'; Path = $path; Message = 'Expected a Serena STDIO server.' }
        }
        $args = @($transport.args)
        $context = ('--context=codex' -in $args) -or (($args -join ' ') -match '(?:^| )--context codex(?: |$)')
        if ('start-mcp-server' -notin $args -or '--project-from-cwd' -notin $args -or -not $context) {
            return [PSCustomObject]@{ State = 'INVALID'; Path = $path; Message = 'Expected STDIO start-mcp-server --project-from-cwd --context=codex.' }
        }
        return [PSCustomObject]@{ State = 'CONFIGURED'; Path = $path; Message = 'Enabled in effective Codex configuration; fresh-session tool calls verify connectivity.' }
    }
    catch { return [PSCustomObject]@{ State = 'UNAVAILABLE'; Path = $path; Message = $_.Exception.Message } }
}

function Format-SerenaMcpState {
    param($State)
    if ($State.State -eq 'CONFIGURED') { return 'configured (enabled; connectivity requires a fresh Codex session)' }
    return "WARNING [Serena MCP configuration] $($State.State): $($State.Path). $($State.Message) Continuing local navigation checks."
}

function Get-GraphifyPython {
    $location = Invoke-NativeCapture -FilePath uv -Arguments @('tool', 'dir')
    if ($location.ExitCode -ne 0) { throw 'Cannot locate the uv Graphify installation.' }
    $python = Join-Path ([string]$location.Output[0]).Trim() 'graphifyy\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Graphify must use its uv tool installation.' }
    return $python
}

function Get-GraphifyState {
    param([string]$Root)
    $safety = Get-RepositoryStateSafety -Root $Root -Scope graphify -Operation inspect
    $result = Invoke-InRepository -Root $Root -Action {
        Invoke-NativeCapture -FilePath (Get-GraphifyPython) -Arguments @((Join-Path $script:RepoNavScriptRoot 'graphify-state.py'), 'status', $Root)
    }
    if ($result.ExitCode -ne 0) { throw ("Graphify freshness check failed: " + ($result.Output -join "`n")) }
    return (($result.Output -join "`n") | ConvertFrom-Json)
}

function Ensure-Graphify {
    param([Parameter(Mandatory = $true)][string]$Root)
    $safety = Get-RepositoryStateSafety -Root $Root -Scope graphify -Operation ensure
    $graphPath = Join-Path $Root 'graphify-out\graph.json'
    $state = Get-GraphifyState -Root $Root
    if ($state.state -ne 'missing') {
        $message = $state.state
        if ($state.state -like 'stale*') { $message += '; use repo-nav refresh when fresh architecture analysis is needed' }
        if ($state.PSObject.Properties['semantic'] -and $state.semantic) { $message += '; documentation semantic refresh may be pending' }
        return $message
    }
    $result = Invoke-InRepository -Root $Root -Action {
        Invoke-NativeCapture -FilePath graphify -Arguments @('update', '.')
    }
    if ($result.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $graphPath)) {
        $tail = @($result.Output | Select-Object -Last 8) -join "`n"
        throw "Graphify failed to build the repository graph.`n$tail"
    }
    return 'initialized and built'
}

function Invoke-GraphifyRefresh {
    param([string]$Root)
    $safety = Get-RepositoryStateSafety -Root $Root -Scope graphify -Operation refresh
    $result = Invoke-InRepository -Root $Root -Action {
        Invoke-NativeCapture -FilePath (Get-GraphifyPython) -Arguments @((Join-Path $script:RepoNavScriptRoot 'graphify-state.py'), 'refresh', $Root)
    }
    $result.Output | Write-Output
    if ($result.ExitCode -ne 0) { throw 'Graphify incremental refresh failed; inspect its diagnostics before retrying.' }
}

function Ensure-GraphifyHooks {
    param([Parameter(Mandatory = $true)][string]$Root)
    $safety = Get-RepositoryStateSafety -Root $Root -Scope graphify -Operation inspect

    $attributeState = Get-GraphifyAttributeState -Root $Root
    $current = Get-HookStatus -Root $Root
    if ($current.Installed -and $attributeState.HasGraphifyLine) {
        return 'hooks installed'
    }
    return 'not installed — Graphify hook install also writes .gitattributes; no Git-local-only installer is available'
}

function Get-RepositoryState {
    param([Parameter(Mandatory = $true)][string]$Root)
    $safety = Get-RepositoryStateSafety -Root $Root -Scope all -Operation inspect

    $excludePath = Get-GitPath -Root $Root -PathName 'info/exclude'
    $excludeLines = @()
    try { $excludeLines = Get-ExcludeLines -ExcludePath $excludePath } catch { }
    $excludeState = @{}
    foreach ($entry in @('.gitattributes')) {
        $excludeState[$entry] = @($excludeLines | Where-Object { $_.Trim() -eq $entry }).Count -gt 0
    }

    $global = Get-GlobalExcludeState
    foreach ($entry in $script:RepoNavGlobalEntries) {
        $excludeState[$entry] = $entry -in $global.VerifiedEntries
    }

    $serenaProject = Test-Path -LiteralPath (Join-Path $Root '.serena\project.yml')
    $serenaCache = ($safety.serena.cache_files -gt 0)
    $graph = Test-Path -LiteralPath (Join-Path $Root 'graphify-out\graph.json')
    $rootMarker = Join-Path $Root 'graphify-out\.graphify_root'
    if ($graph -and (Test-Path -LiteralPath $rootMarker)) {
        $graph = Test-GraphifyRootMarker -Root $Root -MarkerPath ((Get-Content -LiteralPath $rootMarker -Raw).Trim())
    }

    $attributeState = Get-GraphifyAttributeState -Root $Root
    $hooks = Invoke-NavigationStage -Stage 'Graphify hooks' -Path $Root -Action { Get-HookStatus -Root $Root }
    [PSCustomObject]@{
        Root          = $Root
        ExcludePath   = $excludePath
        GlobalExcludePath = $global.Path
        GlobalExcludeReadable = $global.Readable
        GlobalExclude = $global
        ExcludeState  = $excludeState
        SerenaProject = $serenaProject
        SerenaCache   = $serenaCache
        StateSafety   = $safety
        Graph         = $graph
        Attributes    = $attributeState
        Hooks         = $hooks
    }
}

function Write-Status {
    param([Parameter(Mandatory = $true)][string]$Root)

    $state = Get-RepositoryState -Root $Root
    Write-Output "Repository: $($state.Root)"
    foreach ($name in @('rg', 'serena', 'graphify')) {
        $tool = Get-ToolInfo -Name $name
        if ($tool.Available) {
            Write-Output ("{0,-9} executable OK  {1}" -f $name, $tool.Version)
        }
        else {
            Write-Output ("{0,-9} ERR missing" -f $name)
        }
    }

    $serenaText = if ($state.SerenaProject -and $state.SerenaCache) { 'indexed' } elseif ($state.SerenaProject) { 'project exists; index missing' } else { 'not initialized' }
    $graphState = Invoke-NavigationStage -Stage 'Graphify graph' -Path $Root -Action { Get-GraphifyState -Root $Root }
    $graphText = $graphState.state
    if ($graphState.PSObject.Properties['semantic'] -and $graphState.semantic) { $graphText += '; documentation semantic refresh may be pending' }
    $hookText = if ($state.Hooks.Installed) { 'hooks installed' } elseif ($state.Attributes.HasOtherContent) { 'not installed — .gitattributes conflict' } else { 'not installed' }
    if ($state.PSObject.Properties['StateSafety'] -and $state.StateSafety.serena.serialized) { $serenaText = 'unverified executable cache; RepoNav indexing skipped; independent MCP launches are not protected' }
    if ($state.PSObject.Properties['StateSafety']) {
        if ($state.StateSafety.serena.category -eq 'unclear-ownership') { $serenaText = 'existing state preserved; ownership review required before initialization' }
        elseif (-not $state.StateSafety.serena.serialized -and $state.StateSafety.serena.config -and -not $state.StateSafety.serena.cache_files -and -not $state.StateSafety.serena.index_safe) { $serenaText = 'configuration preserved; migration review required before indexing' }
        if ($state.StateSafety.graphify.category -eq 'unclear-ownership') { $graphText += '; existing output preserved; ownership review required before initialization' }
    }
    Write-Output ("Serena project: {0}" -f $serenaText)
    Write-Output ("Serena Codex MCP: {0}" -f (Format-SerenaMcpState (Get-SerenaMcpState)))
    Write-Output ("Graphify {0}; {1}" -f $graphText, $hookText)
    Write-Output ("Global Git excludes: {0} ({1})" -f $state.GlobalExcludePath, $state.GlobalExclude.State)
    if (-not $state.GlobalExclude.Ready) { Write-Output ("WARNING " + (Format-GlobalExcludeWarning $state.GlobalExclude)) }
    foreach ($entry in $script:RepoNavGlobalEntries) {
        $mark = if (-not $state.GlobalExcludeReadable) { 'unavailable' } elseif ($state.ExcludeState[$entry]) { 'OK' } else { 'missing' }
        Write-Output ("  {0,-15} {1}" -f $entry, $mark)
    }
    $localMark = if ($state.ExcludeState['.gitattributes']) { 'optional generated entry: OK' } else { 'optional generated entry: unavailable / not needed' }
    Write-Output ("Repo-local exclusions ({0}): {1}" -f $state.ExcludePath, $localMark)
}

function Invoke-EnsureRepository {
    param([Parameter(Mandatory = $true)][string]$Root)
    $safety = Get-RepositoryStateSafety -Root $Root -Scope all -Operation ensure

    $tools = @{}
    foreach ($name in @('rg', 'serena', 'graphify')) {
        $tools[$name] = Get-ToolInfo -Name $name
        if (-not $tools[$name].Available) {
            throw "[$name executable] $($tools[$name].Path): $($tools[$name].Version). Stopped; required tool unavailable."
        }
    }

    $globalExclude = Ensure-GlobalExcludes
    if (-not $globalExclude.Ready) { Write-Warning (Format-GlobalExcludeWarning $globalExclude) }
    $excludePath = Get-GitPath -Root $Root -PathName 'info/exclude'
    $excludeResults = @()

    $serenaState = Invoke-GuardedRepositoryStage -Root $Root -Stage 'Serena project/index' -Action { Invoke-NavigationStage -Stage 'Serena project/index' -Path $Root -Action { Ensure-Serena -Root $Root } }
    $mcpState = Get-SerenaMcpState
    $graphState = Invoke-GuardedRepositoryStage -Root $Root -Stage 'Graphify graph (graphify update .)' -Action { Invoke-NavigationStage -Stage 'Graphify graph' -Path $Root -Action { Ensure-Graphify -Root $Root } }
    $hookState = Invoke-GuardedRepositoryStage -Root $Root -Stage 'Graphify hook status' -Action { Invoke-NavigationStage -Stage 'Graphify hooks' -Path $Root -Action { Ensure-GraphifyHooks -Root $Root } }

    $attributeState = Get-GraphifyAttributeState -Root $Root
    if ($attributeState.Exists -and -not $attributeState.Tracked -and -not $attributeState.HasOtherContent) {
        $excludeResults += Ensure-OptionalLocalExclude -Root $Root -ExcludePath $excludePath -Entry '.gitattributes'
    }

    $warnings = @()
    if (-not $globalExclude.Ready) { $warnings += Format-GlobalExcludeWarning $globalExclude }
    if ($mcpState.State -ne 'CONFIGURED') { $warnings += Format-SerenaMcpState $mcpState }
    if ($serenaState -like 'WARNING*') { $warnings += $serenaState }
    if ($graphState -eq 'graph invalid') { $warnings += $graphState }
    if ($hookState -like 'not installed*') {
        $warnings += $hookState
    }
    foreach ($excludeResult in $excludeResults) {
        if ($excludeResult -match 'tracked; not excluded|^WARNING:') {
            $warnings += $excludeResult
        }
    }

    [PSCustomObject]@{
        Root          = $Root
        Tools         = $tools
        SerenaState   = $serenaState
        SerenaMcp     = $mcpState
        GraphState    = $graphState
        HookState     = $hookState
        ExcludePath   = $excludePath
        GlobalExclude = $globalExclude
        ExcludeResults = $excludeResults
        Warnings      = @($warnings)
    }
}

function Get-PhysicalRepositoryKey {
    param([string]$Path)
    Assert-OrdinaryRepositoryPath -Path $Path
    if(-not ('RepoNavDirectoryIdentity' -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class RepoNavDirectoryIdentity {
    [StructLayout(LayoutKind.Sequential)] struct Info {
        public uint Attributes, CreationLow, CreationHigh, AccessLow, AccessHigh, WriteLow, WriteHigh;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateFileW(string path, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetFileInformationByHandle(IntPtr handle, out Info info);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    public static string Key(string path) {
        IntPtr handle=CreateFileW(path,0,7,IntPtr.Zero,3,0x02200000,IntPtr.Zero);
        if(handle==new IntPtr(-1)) throw new InvalidOperationException("Repository identity unavailable");
        try {
            Info info;
            if(!GetFileInformationByHandle(handle,out info) || (info.Attributes & 0x400)!=0)
                throw new InvalidOperationException("Repository identity unsafe or unavailable");
            return info.Volume.ToString("x8")+":"+info.IndexHigh.ToString("x8")+info.IndexLow.ToString("x8");
        } finally { CloseHandle(handle); }
    }
}
"@
    }
    return [RepoNavDirectoryIdentity]::Key([IO.Path]::GetFullPath($Path))
}

function Get-CanonicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
        $full = [IO.Path]::GetFullPath($resolved)
        if ($full.Length -gt 3) {
            $full = $full.TrimEnd('\', '/')
        }
        return $full
    }
    catch {
        return $null
    }
}

function Get-PathKey {
    param([Parameter(Mandatory = $true)][string]$Path)

    return $Path.ToLowerInvariant()
}

function Test-SkippedTraversalDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$InsideRepository
    )

    if ($Name -in @('.git', '.serena', 'graphify-out', 'node_modules', '.venv', 'venv', '__pycache__', '.pytest_cache', 'build', 'dist')) {
        return $true
    }
    if ($Name -match '^baseline-checkout-') {
        return $true
    }
    if ($Name -ieq 'temporary benchmark checkouts' -or $Name -ieq '.scratch' -or $Name -match '(^|-)eval-runs$') {
        return $true
    }
    if ($InsideRepository -and $Name -in @('results', 'logs', '.local', '.tmp', '.scratch', 'scratch', 'tmp', 'temp')) {
        return $true
    }
    return $false
}

function Get-GitCandidateInfo {
    param([Parameter(Mandatory = $true)][string]$Path)

    $bareResult = Invoke-NativeCapture -FilePath 'git' -Arguments @('-C', $Path, 'rev-parse', '--is-bare-repository')
    if ($bareResult.ExitCode -eq 0 -and $bareResult.Output.Count -gt 0 -and
        ([string]$bareResult.Output[0]).Trim() -eq 'true') {
        return [PSCustomObject]@{
            Kind   = 'Bare'
            Path   = $Path
            Reason = 'bare repository'
        }
    }

    $insideResult = Invoke-NativeCapture -FilePath 'git' -Arguments @('-C', $Path, 'rev-parse', '--is-inside-work-tree')
    if ($insideResult.ExitCode -eq 0 -and $insideResult.Output.Count -gt 0 -and
        ([string]$insideResult.Output[0]).Trim() -eq 'true') {
        $topResult = Invoke-NativeCapture -FilePath 'git' -Arguments @('-C', $Path, 'rev-parse', '--show-toplevel')
        if ($topResult.ExitCode -eq 0 -and $topResult.Output.Count -gt 0) {
            $worktree = Get-CanonicalPath -Path ([string]$topResult.Output[0]).Trim()
            if ($null -ne $worktree) {
                return [PSCustomObject]@{
                    Kind = 'Worktree'
                    Path = $worktree
                    Reason = ''
                }
            }
        }
    }

    return [PSCustomObject]@{
        Kind   = 'Invalid'
        Path   = $Path
        Reason = 'Git metadata could not be validated'
    }
}

function Test-BareRepositoryShape {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (
        (Test-Path -LiteralPath (Join-Path $Path 'HEAD') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Path 'config') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Path 'objects') -PathType Container) -and
        (Test-Path -LiteralPath (Join-Path $Path 'refs') -PathType Container)
    )
}

function Find-RepositoryCandidates {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][hashtable]$VisitedDirectories
    )

    $candidates = New-Object 'System.Collections.Generic.List[object]'
    $issues = New-Object 'System.Collections.Generic.List[object]'
    $pruned = New-Object 'System.Collections.Generic.List[object]'
    $stack = New-Object 'System.Collections.Generic.Stack[object]'
    $rootKey = Get-PathKey -Path $Root
    $stack.Push([PSCustomObject]@{ Path = $Root; InsideRepository = $false; DoNotDescend = $false })

    while ($stack.Count -gt 0) {
        $item = $stack.Pop()
        try { Assert-OrdinaryRepositoryPath -Path $item.Path }
        catch { [void]$issues.Add([PSCustomObject]@{Path=$item.Path;Reason='redirected or inaccessible scan directory'}); continue }
        $current = Get-CanonicalPath -Path $item.Path
        if ($null -eq $current) {
            [void]$issues.Add([PSCustomObject]@{ Path = $item.Path; Reason = 'path could not be resolved' })
            continue
        }

        $currentKey = Get-PathKey -Path $current
        if ($VisitedDirectories.ContainsKey($currentKey)) {
            continue
        }
        $VisitedDirectories[$currentKey] = $true

        $isSuppliedRoot = ($currentKey -eq $rootKey)
        $gitMarker = Join-Path $current '.git'
        try {
            if([IO.File]::GetAttributes($gitMarker) -band [IO.FileAttributes]::ReparsePoint) {
                [void]$issues.Add([PSCustomObject]@{Path=$current;Reason='redirected Git metadata marker'}); continue
            }
        } catch [IO.FileNotFoundException] { } catch [IO.DirectoryNotFoundException] { }
        catch { [void]$issues.Add([PSCustomObject]@{Path=$current;Reason='Git metadata marker inaccessible'}); continue }
        $hasGitMarker = Test-Path -LiteralPath $gitMarker
        $hasBareShape = Test-BareRepositoryShape -Path $current
        $candidate = $null
        if ($isSuppliedRoot -or $hasGitMarker -or $hasBareShape) {
            $candidate = Get-GitCandidateInfo -Path $current
            if ($null -ne $candidate -and ($candidate.Kind -ne 'Invalid' -or $hasGitMarker -or $hasBareShape)) {
                [void]$candidates.Add($candidate)
            }
        }

        if ($null -ne $candidate -and $candidate.Kind -eq 'Bare') {
            continue
        }

        $insideRepository = $item.InsideRepository -or ($null -ne $candidate -and $candidate.Kind -eq 'Worktree')
        try {
            $children = @(Get-ChildItem -LiteralPath $current -Directory -Force -ErrorAction Stop)
        }
        catch {
            [void]$issues.Add([PSCustomObject]@{ Path = $current; Reason = 'directory could not be read' })
            continue
        }

        foreach ($child in $children) {
            if (Test-SkippedTraversalDirectory -Name $child.Name -InsideRepository $insideRepository) {
                [void]$pruned.Add([PSCustomObject]@{ Path = $child.FullName; Reason = 'generated/heavy directory' })
                continue
            }

            $isReparsePoint = (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
            if ($isReparsePoint) { [void]$pruned.Add([PSCustomObject]@{Path=$child.FullName;Reason='redirected directory'}); continue }
            $stack.Push([PSCustomObject]@{
                    Path = $child.FullName
                    InsideRepository = $insideRepository
                    DoNotDescend = $isReparsePoint
                })
        }
    }

    [PSCustomObject]@{
        Candidates = @($candidates.ToArray())
        Issues     = @($issues.ToArray())
        Pruned     = @($pruned.ToArray())
    }
}

function Write-BootstrapLine {
    param(
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $false)][string]$Reason = ''
    )

    if ([string]::IsNullOrWhiteSpace($Reason)) {
        Write-Output ("{0,-8} {1}" -f $Status, $Path)
    }
    else {
        Write-Output ("{0,-8} {1}  {2}" -f $Status, $Path, $Reason)
    }
}

function Invoke-BootstrapAll {
    param([Parameter(Mandatory = $false)][string[]]$BootstrapArguments = @())

    $dryRun = $false
    $rootArguments = @()
    foreach ($argument in $BootstrapArguments) {
        if ($argument -ieq '--dry-run') {
            if ($dryRun) {
                Write-Output 'Usage: repo-nav bootstrap-all [--dry-run] <root> [<root2> ...]'
                return 2
            }
            $dryRun = $true
            continue
        }
        if ($argument.StartsWith('-')) {
            Write-Output "Unknown bootstrap-all option: $argument"
            Write-Output 'Usage: repo-nav bootstrap-all [--dry-run] <root> [<root2> ...]'
            return 2
        }
        $rootArguments += $argument
    }

    if ($rootArguments.Count -eq 0) {
        Write-Output 'Usage: repo-nav bootstrap-all [--dry-run] <root> [<root2> ...]'
        return 2
    }

    $validRoots = New-Object 'System.Collections.Generic.List[string]'
    $rootProblems = New-Object 'System.Collections.Generic.List[object]'
    foreach ($rootArgument in $rootArguments) {
        try {
            Assert-OrdinaryRepositoryPath -Path $rootArgument
            $item = Get-Item -LiteralPath $rootArgument -Force -ErrorAction Stop
            if (-not $item.PSIsContainer) {
                [void]$rootProblems.Add([PSCustomObject]@{ Path = $rootArgument; Reason = 'not a directory' })
                continue
            }
            $resolved = Get-CanonicalPath -Path $item.FullName
            if ($null -eq $resolved) {
                [void]$rootProblems.Add([PSCustomObject]@{ Path = $rootArgument; Reason = 'could not be resolved' })
                continue
            }
            [void]$validRoots.Add($resolved)
        }
        catch {
            [void]$rootProblems.Add([PSCustomObject]@{ Path = $rootArgument; Reason = 'does not exist or is inaccessible' })
        }
    }

    foreach ($problem in $rootProblems) {
        Write-BootstrapLine -Status 'FAILED' -Path $problem.Path -Reason $problem.Reason
    }
    if ($validRoots.Count -eq 0) {
        Write-Output 'No valid scan roots were supplied.'
        return 2
    }

    $startTime = Get-Date
    Write-Output ("Discovering Git repositories under {0}..." -f ($validRoots -join ', '))
    $visitedDirectories = @{}
    $seenCandidates = @{}
    $candidates = New-Object 'System.Collections.Generic.List[object]'
    $discoveryIssues = New-Object 'System.Collections.Generic.List[object]'
    $prunedDirectories = New-Object 'System.Collections.Generic.List[object]'

    foreach ($root in $validRoots) {
        $found = Find-RepositoryCandidates -Root $root -VisitedDirectories $visitedDirectories
        foreach ($issue in $found.Issues) { [void]$discoveryIssues.Add($issue) }
        foreach ($prunedItem in $found.Pruned) { [void]$prunedDirectories.Add($prunedItem) }
        foreach ($candidate in $found.Candidates) {
            $canonicalCandidate = Get-CanonicalPath -Path $candidate.Path
            if ($null -eq $canonicalCandidate) {
                [void]$discoveryIssues.Add([PSCustomObject]@{ Path = $candidate.Path; Reason = 'candidate path could not be resolved' })
                continue
            }
            $candidate.Path = $canonicalCandidate
            try { $key = Get-PhysicalRepositoryKey -Path $canonicalCandidate }
            catch { [void]$discoveryIssues.Add([PSCustomObject]@{Path=$canonicalCandidate;Reason='physical repository identity unavailable'}); continue }
            if ($seenCandidates.ContainsKey($key)) {
                continue
            }
            $seenCandidates[$key] = $true
            [void]$candidates.Add($candidate)
        }
    }

    foreach ($issue in $discoveryIssues) {
        Write-BootstrapLine -Status 'FAILED' -Path $issue.Path -Reason $issue.Reason
    }

    $counts = @{ READY = 0; WARNING = 0; SKIPPED = 0; FAILED = 0 }
    if ($dryRun) {
        foreach ($candidate in ($candidates | Sort-Object Path)) {
            if ($candidate.Kind -eq 'Bare') {
                Write-BootstrapLine -Status 'SKIPPED' -Path $candidate.Path -Reason 'bare repository'
                $counts['SKIPPED']++
            }
            elseif ($candidate.Kind -eq 'Worktree') {
                Write-BootstrapLine -Status 'DRY-RUN' -Path $candidate.Path -Reason 'would run ensure'
                $counts['READY']++
            }
            else {
                Write-BootstrapLine -Status 'FAILED' -Path $candidate.Path -Reason $candidate.Reason
                $counts['FAILED']++
            }
        }
        foreach ($issue in $discoveryIssues) { $counts['FAILED']++ }
        $counts['FAILED'] += $rootProblems.Count
        $elapsed = ((Get-Date) - $startTime).TotalSeconds
        Write-Output ''
        Write-Output 'Dry run complete.'
        Write-Output ("Discovered:  {0}" -f $candidates.Count)
        Write-Output ("Would process: {0}" -f $counts['READY'])
        Write-Output ("Skipped:     {0}" -f $counts['SKIPPED'])
        Write-Output ("Failed:      {0}" -f $counts['FAILED'])
        Write-Output ("Elapsed:     {0:n1}s" -f $elapsed)
        if ($prunedDirectories.Count -gt 0) {
            Write-Output ("Traversal pruned: {0} generated/heavy directories" -f $prunedDirectories.Count)
        }
        if ($discoveryIssues.Count -gt 0 -or $rootProblems.Count -gt 0) { return 2 }
        return 0
    }

    foreach ($candidate in ($candidates | Sort-Object Path)) {
        if ($candidate.Kind -eq 'Bare') {
            Write-BootstrapLine -Status 'SKIPPED' -Path $candidate.Path -Reason 'bare repository'
            $counts['SKIPPED']++
            continue
        }
        if ($candidate.Kind -ne 'Worktree') {
            Write-BootstrapLine -Status 'FAILED' -Path $candidate.Path -Reason $candidate.Reason
            $counts['FAILED']++
            continue
        }

        try {
            $result = Invoke-EnsureRepository -Root $candidate.Path
            if ($result.Warnings.Count -gt 0) {
                Write-BootstrapLine -Status 'WARNING' -Path $candidate.Path -Reason ($result.Warnings -join '; ')
                $counts['WARNING']++
            }
            else {
                Write-BootstrapLine -Status 'READY' -Path $candidate.Path
                $counts['READY']++
            }
        }
        catch {
            $reason = $_.Exception.Message -replace "`r", ' ' -replace "`n", ' '
            if ($reason.Length -gt 180) { $reason = $reason.Substring(0, 177) + '...' }
            Write-BootstrapLine -Status 'FAILED' -Path $candidate.Path -Reason $reason
            $counts['FAILED']++
        }
    }

    foreach ($issue in $discoveryIssues) { $counts['FAILED']++ }
    $counts['FAILED'] += $rootProblems.Count
    $elapsed = ((Get-Date) - $startTime).TotalSeconds
    Write-Output ''
    Write-Output 'Bootstrap complete.'
    Write-Output ("Discovered:  {0}" -f $candidates.Count)
    Write-Output ("Ready:       {0}" -f $counts['READY'])
    Write-Output ("Warnings:    {0}" -f $counts['WARNING'])
    Write-Output ("Skipped:     {0}" -f $counts['SKIPPED'])
    Write-Output ("Failed:      {0}" -f $counts['FAILED'])
    Write-Output ("Elapsed:     {0:n1}s" -f $elapsed)
    if ($prunedDirectories.Count -gt 0) {
        Write-Output ("Traversal pruned: {0} generated/heavy directories" -f $prunedDirectories.Count)
    }

    if ($rootProblems.Count -gt 0 -or $discoveryIssues.Count -gt 0) { return 2 }
    if ($counts['WARNING'] -gt 0 -or $counts['FAILED'] -gt 0) { return 1 }
    return 0
}

try {
    Assert-SupportedDependencies
    if ($Command -eq 'bootstrap-all') {
        $bootstrapOutput = @(Invoke-BootstrapAll -BootstrapArguments $Arguments)
        $bootstrapExit = 1
        if ($bootstrapOutput.Count -gt 0 -and $bootstrapOutput[$bootstrapOutput.Count - 1] -is [int]) {
            $bootstrapExit = [int]$bootstrapOutput[$bootstrapOutput.Count - 1]
            if ($bootstrapOutput.Count -gt 1) {
                $bootstrapOutput[0..($bootstrapOutput.Count - 2)] | Write-Output
            }
        }
        exit $bootstrapExit
    }

    if ($Command -notin @('ensure', 'status', 'refresh') -or $Arguments.Count -gt 0) {
        Write-Output 'Usage: repo-nav ensure | status | refresh | bootstrap-all [--dry-run] <root> [<root2> ...]'
        exit 2
    }

    $root = Get-RepositoryRoot
    if ($Command -eq 'refresh') { Invoke-GuardedRepositoryStage -Root $root -Stage 'Graphify graph refresh (graphify-state.py refresh)' -Action { Invoke-NavigationStage -Stage 'Graphify graph refresh' -Path $root -Action { Invoke-GraphifyRefresh -Root $root } }; exit 0 }
    if ($Command -eq 'status') {
        $tools = @{}
        foreach ($name in @('rg', 'serena', 'graphify')) {
            $tools[$name] = Get-ToolInfo -Name $name
            if (-not $tools[$name].Available) {
                throw "[$name executable] $($tools[$name].Path): $($tools[$name].Version). Stopped; required tool unavailable."
            }
        }
        Write-Status -Root $root
        exit 0
    }

    $result = Invoke-EnsureRepository -Root $root
    Write-Output "Repository: $root"
    Write-Output "rg        OK  $($result.Tools['rg'].Version)"
    Write-Output "Serena project: $($result.SerenaState)"
    Write-Output ("Serena Codex MCP: " + (Format-SerenaMcpState $result.SerenaMcp))
    Write-Output "Graphify  OK  $($result.GraphState); $($result.HookState)"
    Write-Output ''
    if ($result.Warnings.Count -gt 0) { Write-Output 'Repository navigation checks completed with warnings.' } else { Write-Output 'Repository navigation ready.' }
    exit 0
}
catch {
    Write-Output ("repo-nav error: " + $_.Exception.Message)
    exit 1
}
