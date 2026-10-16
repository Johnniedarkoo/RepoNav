param([ValidateSet('unit','performance')][string]$Mode='unit')
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'TestEnvironment.ps1')
$context=Start-RepoNavTestEnvironment
try {
    $python=Join-Path ([string](& uv tool dir)).Trim() 'serena-agent/Scripts/python.exe'
    Push-Location $PSScriptRoot
    try {
        if($Mode -eq 'unit') { & $python -B -m unittest -v test_state_safety }
        else { & $python -B benchmark_state_safety.py }
        if($LASTEXITCODE -ne 0) { throw "B2.2 validation failed: $Mode" }
    } finally { Pop-Location }
} finally { Stop-RepoNavTestEnvironment $context }
