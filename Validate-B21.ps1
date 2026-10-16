param([ValidateSet('reproduce','unit','publication','performance')][string]$Mode='unit')
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'TestEnvironment.ps1')
$context=Start-RepoNavTestEnvironment
try {
    Push-Location $PSScriptRoot
    try {
        switch($Mode) {
            reproduce { & (Join-Path $PSScriptRoot 'Preservation.Tests.ps1') -ReproduceOriginal; python -B -m unittest -v test_preservation.PreservationTests.test_raw_diff_reproduction }
            unit { python -B -m unittest -v test_preservation }
            publication { python -B -m unittest -v test_prepare_snapshot }
            performance { python -B benchmark_preservation.py }
        }
        if($LASTEXITCODE -ne 0) { throw "Validation failed: $Mode" }
    } finally { Pop-Location }
} finally { Stop-RepoNavTestEnvironment $context }
