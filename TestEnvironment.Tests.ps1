$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'TestEnvironment.ps1')
$outer=Start-RepoNavTestEnvironment
try {
$sentinelIgnore=Join-Path $outer.Root 'existing-ignore'
Set-Content $sentinelIgnore 'existing user ignore'
& git config --global core.excludesFile $sentinelIgnore
& git config --global sentinel.existing preserved
$sentinelConfig=$env:GIT_CONFIG_GLOBAL
$configBytes=[IO.File]::ReadAllBytes($sentinelConfig)
$ignoreBytes=[IO.File]::ReadAllBytes($sentinelIgnore)
$before=@{}; foreach($name in @('HOME','USERPROFILE','GIT_CONFIG_GLOBAL','GIT_CONFIG_NOSYSTEM','TEMP','UV_TOOL_DIR')) { $before[$name]=[Environment]::GetEnvironmentVariable($name) }
$checks=0
foreach($fail in @($false,$true)) {
    $context=Start-RepoNavTestEnvironment
    try {
        if($env:GIT_CONFIG_NOSYSTEM -ne '1' -or $env:GIT_CONFIG_GLOBAL -notlike "$($context.Root)*") { throw 'Git isolation missing' }; $checks++
        $sentinel=& git config --global sentinel.existing
        if($LASTEXITCODE -ne 1 -or $sentinel) { throw 'Parent configuration exposed' }; $checks++
        $childHome=& python -B -c 'from pathlib import Path; print(Path.home())'
        if($LASTEXITCODE -ne 0 -or $childHome -cne $env:USERPROFILE) { throw 'Python home not isolated' }; $checks++
        & git config --global core.excludesFile (Join-Path $env:HOME 'ignore')
        & git config --global test.second command
        if($LASTEXITCODE -ne 0 -or (& git config --global test.second) -ne 'command') { throw 'Persistent configuration failed' }; $checks++
        $code='[Console]::Write($env:GIT_CONFIG_GLOBAL + [char]124 + $env:HOME + [char]124 + $env:GIT_CONFIG_NOSYSTEM)'; $child=& powershell -NoProfile -EncodedCommand ([Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code)))
        if($child -ne ($env:GIT_CONFIG_GLOBAL+'|'+$env:HOME+'|1')) { throw 'Child environment lost' }; $checks++
        $actual=$context.Root; $context.Root=$context.Base
        try { Stop-RepoNavTestEnvironment $context; throw 'Unsafe target accepted' } catch { if($_ -notmatch 'Unsafe cleanup target') { throw } } finally { $context.Root=$actual }
        # Refused cleanup restores the environment; re-enter a separate context below.
        $checks++
        if($fail) { throw 'controlled failure' }
    } catch { if($_ -notmatch 'controlled failure') { throw } }
    finally { Stop-RepoNavTestEnvironment $context }
    foreach($name in $before.Keys) { if([Environment]::GetEnvironmentVariable($name) -cne $before[$name]) { throw "Environment not restored: $name" } }; $checks++
    if([Convert]::ToBase64String([IO.File]::ReadAllBytes($sentinelConfig)) -cne [Convert]::ToBase64String($configBytes)) { throw 'Parent global configuration changed' }; $checks++
    if([Convert]::ToBase64String([IO.File]::ReadAllBytes($sentinelIgnore)) -cne [Convert]::ToBase64String($ignoreBytes)) { throw 'Parent ignore changed' }; $checks++
    if(Test-Path -LiteralPath $context.Root) { throw 'Owned root not removed' }; $checks++
}
Write-Output "Isolation checks passed: $checks"


} finally { Stop-RepoNavTestEnvironment $outer }
