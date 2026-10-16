$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'TestEnvironment.ps1')
$environment=Start-RepoNavTestEnvironment
$links=New-Object 'Collections.Generic.List[string]'
try {
    $script:RepoNavScriptRoot=$PSScriptRoot
    $script:RepoNavGlobalEntries=@('.serena/','graphify-out/')
    $toolRoot=([string](& uv tool dir)).Trim()
    $script:SafetyPython=Join-Path $toolRoot 'serena-agent/Scripts/python.exe'
    $script:GraphPython=Join-Path $toolRoot 'graphifyy/Scripts/python.exe'
    $script:ConfigTemplate=Get-Content (Join-Path $toolRoot 'serena-agent/Lib/site-packages/serena/resources/project.template.yml') -Raw
    $tokens=$null; $errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'repo-nav.ps1'),[ref]$tokens,[ref]$errors)
    if($errors.Count) { throw ($errors -join "`n") }
    foreach($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) { Invoke-Expression $f.Extent.Text }
    $script:RealNative=(Get-Item Function:Invoke-NativeCapture).ScriptBlock
    $script:RealCandidate=(Get-Item Function:Get-GitCandidateInfo).ScriptBlock
    function Get-StateSafetyPython { $script:SafetyPython }
    function Get-GraphifyPython { $script:GraphPython }
    function Get-ToolInfo { param($Name) [PSCustomObject]@{Available=$true;Version='mock';Path='mock'} }
    function Get-SerenaMcpState { [PSCustomObject]@{State='CONFIGURED';Path='mock';Message='mock'} }
    function Invoke-UnattendedCapture {
        param($FilePath,$Arguments)
        $script:Consumers++
        $script:LastSerenaArgs=$Arguments
        $script:ConsumerHome=$env:SERENA_HOME
        $script:ConsumerConfig=if(Test-Path (Join-Path $env:SERENA_HOME 'serena_config.yml')) { Get-Content (Join-Path $env:SERENA_HOME 'serena_config.yml') -Raw } else { '' }
        if($script:FailConsumer) { return [PSCustomObject]@{ExitCode=7;Output=@('controlled failure')} }
        $root=$Arguments[2]
        New-Item -ItemType Directory -Path (Join-Path $root '.serena/cache') -Force | Out-Null
        if(-not (Test-Path (Join-Path $root '.serena/project.yml'))) { Set-Content (Join-Path $root '.serena/project.yml') $script:ConfigTemplate }
        Set-Content (Join-Path $root '.serena/cache/symbols.bin') 'safe mock generated cache'
        [PSCustomObject]@{ExitCode=0;Output=@()}
    }
    function Invoke-NativeCapture {
        param($FilePath,$Arguments)
        if($FilePath -eq 'graphify') {
            $script:GraphCalls+=,($Arguments -join ',')
            if($Arguments[0] -eq 'update') {
                New-Item -ItemType Directory graphify-out -Force | Out-Null
                Set-Content graphify-out/graph.json '{"nodes":[],"links":[]}'
            }
            return [PSCustomObject]@{ExitCode=0;Output=@('mock hook status')}
        }
        if($Arguments.Count -ge 2 -and $Arguments[0] -like '*graphify-state.py' -and $Arguments[1] -eq 'refresh') {
            $script:GraphCalls+=,('adapter:' + ($Arguments[1..2] -join ','))
            return [PSCustomObject]@{ExitCode=0;Output=@('mock native incremental refresh')}
        }
        & $script:RealNative -FilePath $FilePath -Arguments $Arguments
    }
    function Get-GraphifyState {
        param($Root)
        [PSCustomObject]@{state=if(Test-Path (Join-Path $Root 'graphify-out/graph.json')) {'graph current'} else {'missing'};semantic=$false}
    }
    function Get-GitCandidateInfo { param($Path) $script:Classified+=,$Path; & $script:RealCandidate -Path $Path }
    function Assert($Value,$Message) { if(-not $Value) { throw $Message } }
    function New-Fixture($Name) {
        $root=Join-Path $environment.Root $Name
        New-Item -ItemType Directory $root -Force | Out-Null
        & git -C $root init -q; & git -C $root config user.name 'Synthetic Test'; & git -C $root config user.email 'test@example.invalid'
        Set-Content (Join-Path $root 'app.py') 'def source(): return 1'
        & git -C $root add app.py; & git -C $root commit -qm initial
        if($LASTEXITCODE -ne 0) { throw 'Fixture Git setup failed' }
        return $root
    }
    function Add-Config($Root) { New-Item -ItemType Directory (Join-Path $Root '.serena') -Force | Out-Null; Set-Content (Join-Path $Root '.serena/project.yml') $script:ConfigTemplate }
    function Add-Graph($Root) { New-Item -ItemType Directory (Join-Path $Root 'graphify-out') -Force | Out-Null; Set-Content (Join-Path $Root 'graphify-out/graph.json') '{"nodes":[],"links":[]}' }
    function Expect-Refusal($Action) { $message=''; try { & $Action | Out-Null } catch { $message=$_.Exception.Message }; Assert ($message -match 'STATE SAFETY|unsafe|inaccessible') "Expected safe refusal: $message" }
    function New-Junction($Link,$Target) {
        New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null
        $links.Add($Link)
    }
    $outside=Join-Path $environment.Root 'outside'; New-Item -ItemType Directory $outside | Out-Null
    $sentinel=Join-Path $outside 'sentinel.txt'; Set-Content $sentinel 'outside sentinel'; $sentinelHash=(Get-FileHash $sentinel).Hash
    $script:Passed=0
    function Run($Name,$Action) {
        $script:Consumers=0; $script:GraphCalls=@(); $script:Classified=@(); $script:FailConsumer=$false
        & $Action
        Assert ((Get-FileHash $sentinel).Hash -ceq $sentinelHash) 'Outside sentinel changed'
        $script:Passed++; Write-Output "PASS $Name"
    }
    Run '01 absent Serena state' {
        $r=New-Fixture 'fresh'; Assert ((Ensure-Serena $r) -eq 'initialized and indexed') 'Fresh indexing failed'; Assert ($script:Consumers -eq 1) 'Consumer missing'
    }
    Run '02 existing configuration preserved' {
        $r=New-Fixture 'config'; Add-Config $r; $before=(Get-FileHash (Join-Path $r '.serena/project.yml')).Hash
        Assert ((Ensure-Serena $r) -eq 'indexed') 'Safe configuration did not index'; Assert ($script:LastSerenaArgs[1] -eq 'index') 'Configuration recreated'
        Assert ((Get-FileHash (Join-Path $r '.serena/project.yml')).Hash -ceq $before) 'Configuration rewritten'
    }
    Run '03 ordinary cache reused' {
        $r=New-Fixture 'ordinary'; Add-Config $r; New-Item -ItemType Directory (Join-Path $r '.serena/cache') | Out-Null; Set-Content (Join-Path $r '.serena/cache/symbols.bin') 'ordinary'
        Assert ((Ensure-Serena $r) -eq 'existing index') 'Ordinary cache not reused'; Assert ($script:Consumers -eq 0) 'Unnecessary consumer'
    }
    Run '04 raw pickle consumer refused' {
        $r=New-Fixture 'pickle'; Add-Config $r; New-Item -ItemType Directory (Join-Path $r '.serena/cache/python') -Force | Out-Null
        Set-Content (Join-Path $r '.serena/cache/python/raw_document_symbols.pkl') 'inert dummy bytes'
        Assert ((Ensure-Serena $r) -like 'WARNING*unverified*') 'Missing pickle diagnostic'; Assert ($script:Consumers -eq 0) 'Unsafe consumer invoked'
    }
    Run '05 legacy cache without configuration refused' {
        $r=New-Fixture 'legacy'; New-Item -ItemType Directory (Join-Path $r '.serena/cache/python') -Force | Out-Null; Set-Content (Join-Path $r '.serena/cache/python/document_symbols_cache_v23-06-25.pkl') 'inert'
        Assert ((Ensure-Serena $r) -like 'WARNING*') 'Legacy cache not refused'; Assert ($script:Consumers -eq 0) 'Unsafe create invoked'
    }
    Run '06 cache bytes preserved' {
        $r=New-Fixture 'preserved'; Add-Config $r; New-Item -ItemType Directory (Join-Path $r '.serena/cache/python') -Force | Out-Null
        $p=Join-Path $r '.serena/cache/python/document_symbols.pkl'; Set-Content $p 'inert'; $hash=(Get-FileHash $p).Hash; $c=(Get-FileHash (Join-Path $r '.serena/project.yml')).Hash
        Ensure-Serena $r | Out-Null; Assert ((Get-FileHash $p).Hash -ceq $hash) 'Cache changed'; Assert ((Get-FileHash (Join-Path $r '.serena/project.yml')).Hash -ceq $c) 'Config changed'
    }
    Run '07 ensure existing graph preserved' {
        $r=New-Fixture 'graph'; Add-Graph $r; $hash=(Get-FileHash (Join-Path $r 'graphify-out/graph.json')).Hash
        Assert ((Ensure-Graphify $r) -eq 'graph current') 'Graph not reused'; Assert ($script:GraphCalls.Count -eq 0) 'Graph rebuilt'; Assert ((Get-FileHash (Join-Path $r 'graphify-out/graph.json')).Hash -ceq $hash) 'Graph changed'
    }
    Run '08 refresh adapter dispatch unchanged' {
        $r=New-Fixture 'refresh'; Add-Graph $r; Invoke-GraphifyRefresh $r | Out-Null; Assert ($script:GraphCalls.Count -eq 1 -and $script:GraphCalls[0] -like 'adapter:refresh,*') 'Refresh did not use native adapter'
    }
    Run '09 unclear Graphify ownership' {
        $r=New-Fixture 'unknown'; New-Item -ItemType Directory (Join-Path $r 'graphify-out') | Out-Null; $p=Join-Path $r 'graphify-out/user.txt'; Set-Content $p 'user state'; $hash=(Get-FileHash $p).Hash
        Expect-Refusal { Ensure-Graphify $r }; Assert ($script:GraphCalls.Count -eq 0) 'Unknown output overwritten'; Assert ((Get-FileHash $p).Hash -ceq $hash) 'Unknown file changed'
    }
    Run '10 Serena junction' { $r=New-Fixture 'serena-link'; New-Junction (Join-Path $r '.serena') $outside; Expect-Refusal { Ensure-Serena $r }; Assert ($script:Consumers -eq 0) 'Redirected consumer' }
    Run '11 Graphify junction' { $r=New-Fixture 'graph-link'; New-Junction (Join-Path $r 'graphify-out') $outside; Expect-Refusal { Ensure-Graphify $r }; Assert ($script:GraphCalls.Count -eq 0) 'Redirected writer' }
    Run '12 nested state junction' { $r=New-Fixture 'nested-link'; Add-Config $r; New-Item -ItemType Directory (Join-Path $r '.serena/cache') | Out-Null; New-Junction (Join-Path $r '.serena/cache/python') $outside; Expect-Refusal { Ensure-Serena $r } }
    Run '13 legitimate linked worktree' {
        $r=New-Fixture 'worktree-main'; $w=Join-Path $environment.Root 'worktree-secondary'; & git -C $r worktree add --detach -q $w; Assert ($LASTEXITCODE -eq 0) 'Worktree creation failed'
        Assert ((Ensure-Serena $w) -eq 'initialized and indexed') 'Worktree refused'; Assert (-not (Get-Item -Force (Join-Path $w '.git')).PSIsContainer) 'Not a linked worktree fixture'
    }
    Run '14 bulk junction checked before classification' {
        $scan=Join-Path $environment.Root 'scan'; New-Item -ItemType Directory $scan | Out-Null; $r=New-Fixture 'scan/real'; $link=Join-Path $scan 'alias'; New-Junction $link $r
        $found=Find-RepositoryCandidates -Root $scan -VisitedDirectories @{}
        Assert (@($found.Candidates).Count -eq 1) 'Duplicate repository'; Assert ($script:Classified -notcontains $link) 'Link classified before refusal'
    }
    Run '15 explicitly redirected root refused' {
        $r=New-Fixture 'root-target'; $link=Join-Path $environment.Root 'root-alias'; New-Junction $link $r
        $result=@(Invoke-BootstrapAll @($link)); Assert ($result[-1] -eq 2) 'Explicit linked root accepted'; Assert ($script:Consumers -eq 0) 'Linked root processed'
    }
    Run '16 bulk failure isolation' {
        $scan=Join-Path $environment.Root 'batch'; $bad=New-Fixture 'batch/01-unsafe'; Set-Content (Join-Path $bad '.serena') 'not a directory'; $good=New-Fixture 'batch/02-safe'
        $result=@(Invoke-BootstrapAll @($scan)); $output=$result -join "`n"
        Assert ($output -match 'FAILED.*01-unsafe' -and $output -match '(READY|WARNING).*02-safe') 'Bulk failure stopped safe repository'; Assert ($script:Consumers -eq 1) 'Unsafe repository reached consumer'
    }
    Run '17 spaces and Unicode' { $r=New-Fixture ('space caf' + [char]0xe9); Assert ((Ensure-Serena $r) -eq 'initialized and indexed') 'Unicode path lost' }
    Run '18 malformed state' { $r=New-Fixture 'malformed'; New-Item -ItemType Directory (Join-Path $r '.serena/project.yml') -Force | Out-Null; Expect-Refusal { Ensure-Serena $r } }
    Run '19 failure and environment restoration' {
        $r=New-Fixture 'failure'; Add-Config $r; $hash=(Get-FileHash (Join-Path $r '.serena/project.yml')).Hash; $priorSerenaHome=$env:SERENA_HOME; $script:FailConsumer=$true
        $message=''; try { Ensure-Serena $r } catch { $message=$_.Exception.Message }; Assert ($message -match 'controlled failure') 'Failure diagnostic lost'
        Assert ($priorSerenaHome -ceq $env:SERENA_HOME) 'Serena environment leaked'; Assert ((Get-FileHash (Join-Path $r '.serena/project.yml')).Hash -ceq $hash) 'Failed indexing changed config'
    }
    Run '20 B2.1 dirty preservation still active' {
        $r=New-Fixture 'dirty'; $p=Join-Path $r 'app.py'; Add-Content $p '# baseline'; $message=''
        try { Invoke-GuardedRepositoryStage $r mock { Add-Content $p '# extra' } } catch { $message=$_.Exception.Message }
        Assert ($message -match 'TRACKED FILE POLLUTION') 'Dirty guard weakened'
    }
    Run '21 custom global state location refused' {
        $r=New-Fixture 'custom'; $cfg=Join-Path $env:SERENA_HOME 'serena_config.yml'; Set-Content $cfg 'project_serena_folder_location: "$projectDir/other"'
        try { Expect-Refusal { Ensure-Serena $r }; Assert ($script:Consumers -eq 0) 'Custom target consumer invoked' } finally { Remove-Item -LiteralPath $cfg }
    }
    Run '22 original global config and registrations preserved' {
        $r=New-Fixture 'global-copy'; $cfg=Join-Path $env:SERENA_HOME 'serena_config.yml'; Set-Content $cfg "projects: [unrelated-project]`nproject_serena_folder_location: '`$projectDir/.serena'`nlog_level: INFO"
        $hash=(Get-FileHash $cfg).Hash
        try { Ensure-Serena $r | Out-Null; Assert ((Get-FileHash $cfg).Hash -ceq $hash) 'Original global config changed'; Assert ($script:ConsumerConfig -match 'projects: \[\]') 'Unrelated registrations not isolated'; Assert (-not (Test-Path $script:ConsumerHome)) 'Temporary home leaked' } finally { Remove-Item -LiteralPath $cfg }
    }
    Run '23 migration-prone configuration preserved' {
        $r=New-Fixture 'incomplete'; New-Item -ItemType Directory (Join-Path $r '.serena') | Out-Null; $p=Join-Path $r '.serena/project.yml'; Set-Content $p 'language_servers: [python_jedi]'; $hash=(Get-FileHash $p).Hash
        Assert ((Ensure-Serena $r) -like 'WARNING*migration*') 'Migration not refused'; Assert ($script:Consumers -eq 0) 'Migration consumer invoked'; Assert ((Get-FileHash $p).Hash -ceq $hash) 'Incomplete config overwritten'
    }
    Run '24 hardlinked generated file refused' {
        $r=New-Fixture 'hardlink'; New-Item -ItemType Directory (Join-Path $r 'graphify-out') | Out-Null; $p=Join-Path $r 'graphify-out/graph.json'; New-Item -ItemType HardLink -Path $p -Target $sentinel | Out-Null
        Expect-Refusal { Ensure-Graphify $r }; Assert ($script:GraphCalls.Count -eq 0) 'Hardlink writer invoked'
    }
    Run '25 read-only status discloses executable cache' {
        $r=New-Fixture 'status'; Add-Config $r; New-Item -ItemType Directory (Join-Path $r '.serena/cache') | Out-Null; $p=Join-Path $r '.serena/cache/raw_document_symbols.pkl'; Set-Content $p 'inert'; $hash=(Get-FileHash $p).Hash
        $output=Write-Status $r; Assert (($output -join "`n") -match 'unverified executable cache') 'Status hides risk'; Assert ($script:Consumers -eq 0) 'Status invokes index'; Assert ((Get-FileHash $p).Hash -ceq $hash) 'Status changed cache'
    }
    Run '26 CLI unsafe ensure exits one before tools' {
        $r=New-Fixture 'cli-refusal'; Set-Content (Join-Path $r '.serena') 'malformed state'
        Push-Location $r
        try { $output=& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $script:RepoNavScriptRoot 'repo-nav.ps1') ensure; $code=$LASTEXITCODE } finally { Pop-Location }
        Assert ($code -eq 1 -and ($output -join "`n") -match 'STATE SAFETY') 'CLI refusal exit changed'
    }
    Run '27 physical repository deduplication' {
        $scan=Join-Path $environment.Root 'physical'; New-Item -ItemType Directory $scan | Out-Null; $r=New-Fixture 'physical/repo'
        Assert ((Get-PhysicalRepositoryKey $r) -ceq (Get-PhysicalRepositoryKey (Join-Path $r '.'))) 'Physical identity is not stable'
        $result=@(Invoke-BootstrapAll -BootstrapArguments @('--dry-run',$scan,$r)); $output=$result -join "`n"
        Assert ($output -match 'Discovered:\s+1' -and $result[-1] -eq 0) 'Physical repository processed twice'
    }
    Run '28 CLI redirected current directory refused' {
        $r=New-Fixture 'cwd-target'; $link=Join-Path $environment.Root 'cwd-alias'; New-Junction $link $r
        Push-Location $link
        try { $output=& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $script:RepoNavScriptRoot 'repo-nav.ps1') ensure; $code=$LASTEXITCODE } finally { Pop-Location }
        Assert ($code -eq 1 -and ($output -join "`n") -match 'Redirected') 'Current directory alias accepted'
    }
    Run '29 linked Git metadata marker refused' {
        $r=New-Fixture 'git-link'; Move-Item -LiteralPath (Join-Path $r '.git') -Destination (Join-Path $r '.git-original')
        New-Junction (Join-Path $r '.git') (Join-Path $r '.git-original'); Expect-Refusal { Ensure-Serena $r }
        Assert ($script:Consumers -eq 0) 'Linked Git metadata consumer invoked'
    }
    Run '30 metadata ignore target cannot redirect writes' {
        $r=New-Fixture 'info-link'; Move-Item -LiteralPath (Join-Path $r '.git/info') -Destination (Join-Path $r '.git/info-original')
        New-Junction (Join-Path $r '.git/info') $outside
        Expect-Refusal { Ensure-LocalExclude -Root $r -ExcludePath (Join-Path $r '.git/info/exclude') -Entry 'generated/' }
        Assert (-not (Test-Path (Join-Path $outside 'exclude'))) 'External Git ignore target created'
    }
    Run '31 bulk metadata link refused before Git classification' {
        $scan=Join-Path $environment.Root 'metadata-scan'; New-Item -ItemType Directory $scan | Out-Null; $r=New-Fixture 'metadata-scan/repo'
        Move-Item -LiteralPath (Join-Path $r '.git') -Destination (Join-Path $r '.git-original'); New-Junction (Join-Path $r '.git') (Join-Path $r '.git-original')
        $found=Find-RepositoryCandidates -Root $scan -VisitedDirectories @{}
        Assert ($script:Classified -notcontains $r) 'Redirected metadata reached Git classification'
        Assert (@($found.Issues).Count -eq 1) 'Metadata refusal missing'
    }
    Run '32 status reports unclear ownership without modifying state' {
        $r=New-Fixture 'status-ownership'; New-Item -ItemType Directory (Join-Path $r '.serena'),(Join-Path $r 'graphify-out') | Out-Null
        $p=Join-Path $r 'graphify-out/user.txt'; Set-Content $p 'user output'; Set-Content (Join-Path $r '.serena/user.txt') 'user state'; $hash=(Get-FileHash $p).Hash
        $output=Write-Status $r; Assert (($output -join "`n") -match 'ownership review required') 'Status hides unclear ownership'; Assert ((Get-FileHash $p).Hash -ceq $hash) 'Status modified unknown state'; Assert ($script:Consumers -eq 0) 'Status consumed unknown state'
    }
    Run '33 executable Serena cache does not block safe Graphify reuse' {
        $r=New-Fixture 'mixed-state'; Add-Config $r; Add-Graph $r; New-Item -ItemType Directory (Join-Path $r '.serena/cache') | Out-Null
        Set-Content (Join-Path $r '.serena/cache/document_symbols.pkl') 'inert'; $result=Invoke-EnsureRepository $r
        Assert ($script:Consumers -eq 0 -and $result.GraphState -eq 'graph current') 'Serena refusal blocked safe Graphify reuse'
        Assert (($result.Warnings -join "`n") -match 'unverified executable') 'Unsafe-cache warning lost'
    }
    Write-Output "B2.2 PowerShell regressions passed: $script:Passed"
} finally {
    foreach($link in $links) { if(Test-Path -LiteralPath $link) { [IO.Directory]::Delete($link) } }
    Stop-RepoNavTestEnvironment $environment
}



