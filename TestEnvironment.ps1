function Start-RepoNavTestEnvironment {
    # Capture dependency discovery before redirecting user state. Never install tools.
    $saved = @{}
    $names = @('GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_COMMON_DIR','GIT_OBJECT_DIRECTORY','GIT_ALTERNATE_OBJECT_DIRECTORIES','GIT_CONFIG','GIT_CONFIG_GLOBAL','GIT_CONFIG_SYSTEM','GIT_CONFIG_NOSYSTEM','GIT_CONFIG_COUNT','GIT_CONFIG_PARAMETERS','HOME','USERPROFILE','XDG_CONFIG_HOME','XDG_CACHE_HOME','XDG_DATA_HOME','APPDATA','LOCALAPPDATA','TEMP','TMP','CODEX_HOME','SERENA_HOME','UV_TOOL_DIR','UV_CACHE_DIR','UV_OFFLINE','PIP_NO_INDEX','GRAPHIFY_NO_AUTO_REFRESH')
    foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
    $toolDir = $env:UV_TOOL_DIR
    if (-not $toolDir -and (Get-Command uv -ErrorAction SilentlyContinue)) {
        $toolOutput = @(& uv tool dir 2>$null)
        if ($LASTEXITCODE -ne 0) { throw 'Cannot discover installed uv tools safely.' }
        $toolDir = [string]$toolOutput[0]
    }
    $base = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $token = [guid]::NewGuid().ToString('N')
    $root = Join-Path $base ('reponav-isolated-' + $token)
    New-Item -ItemType Directory -Path $root | Out-Null
    Set-Content -LiteralPath (Join-Path $root '.owner') -Value $token
    $context = [PSCustomObject]@{ Root=$root; Base=$base; Token=$token; Saved=$saved }
    try {
        foreach ($dir in @('home','config','cache','data','appdata','localappdata','tmp','codex','serena')) {
            New-Item -ItemType Directory -Path (Join-Path $root $dir) | Out-Null
        }
        $values = @{ GIT_DIR=$null; GIT_WORK_TREE=$null; GIT_INDEX_FILE=$null; GIT_COMMON_DIR=$null; GIT_OBJECT_DIRECTORY=$null; GIT_ALTERNATE_OBJECT_DIRECTORIES=$null; GIT_CONFIG=$null; GIT_CONFIG_GLOBAL=(Join-Path $root 'gitconfig'); GIT_CONFIG_SYSTEM=(Join-Path $root 'system-gitconfig'); GIT_CONFIG_NOSYSTEM='1'; HOME=(Join-Path $root 'home'); USERPROFILE=(Join-Path $root 'home'); XDG_CONFIG_HOME=(Join-Path $root 'config'); XDG_CACHE_HOME=(Join-Path $root 'cache'); XDG_DATA_HOME=(Join-Path $root 'data'); APPDATA=(Join-Path $root 'appdata'); LOCALAPPDATA=(Join-Path $root 'localappdata'); TEMP=(Join-Path $root 'tmp'); TMP=(Join-Path $root 'tmp'); CODEX_HOME=(Join-Path $root 'codex'); SERENA_HOME=(Join-Path $root 'serena'); UV_CACHE_DIR=(Join-Path $root 'cache'); UV_OFFLINE='1'; PIP_NO_INDEX='1'; GRAPHIFY_NO_AUTO_REFRESH='1'; GIT_CONFIG_COUNT=$null; GIT_CONFIG_PARAMETERS=$null }
        if ($toolDir) { $values.UV_TOOL_DIR=$toolDir }
        foreach ($name in $values.Keys) {
            if ($null -eq $values[$name]) { Remove-Item -LiteralPath ('Env:' + $name) -ErrorAction SilentlyContinue }
            else { [Environment]::SetEnvironmentVariable($name,$values[$name],'Process') }
        }
        New-Item -ItemType File -Path $env:GIT_CONFIG_GLOBAL,$env:GIT_CONFIG_SYSTEM | Out-Null
        return $context
    } catch { Stop-RepoNavTestEnvironment $context; throw }
}

function Stop-RepoNavTestEnvironment($Context) {
    try {
        $root=[IO.Path]::GetFullPath($Context.Root)
        if ($root -cne (Join-Path $Context.Base ('reponav-isolated-' + $Context.Token)) -or
            (Get-Item -LiteralPath $root -Force).Attributes -band [IO.FileAttributes]::ReparsePoint -or
            (Get-Item -LiteralPath (Join-Path $root '.owner') -Force).Attributes -band [IO.FileAttributes]::ReparsePoint -or
            (Get-Content -LiteralPath (Join-Path $root '.owner') -Raw).Trim() -cne $Context.Token) { throw 'Unsafe cleanup target' }
        # Refuse recursive cleanup if a tool introduced any linked directory/file.
        $pending = New-Object 'Collections.Generic.Stack[string]'
        $pending.Push($root)
        while ($pending.Count) {
            foreach ($item in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked test artifact retained; cleanup refused' }
                if ($item.PSIsContainer) { $pending.Push($item.FullName) }
            }
        }
        Remove-Item -LiteralPath $root -Recurse -Force
    } finally {
        foreach ($name in $Context.Saved.Keys) {
            if ($null -eq $Context.Saved[$name]) { Remove-Item -LiteralPath ('Env:' + $name) -ErrorAction SilentlyContinue }
            else { [Environment]::SetEnvironmentVariable($name,$Context.Saved[$name],'Process') }
        }
    }
}

