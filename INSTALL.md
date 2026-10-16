# Windows installation

v0.1.0 is an initial experimental release. Safe mocks and regressions have passed, but the hardened runtime has not completed a real end-to-end Serena/Graphify indexing and refresh test. Fresh-machine installation also remains unverified.

## Prerequisites

Initial supported configuration (launcher, dependency and safe regression checks only): Windows, Windows PowerShell 5.1.19041.6456, Git for Windows 2.44.0.windows.1, ripgrep 15.2.0, uv 0.12.7, Serena (serena-agent) 1.7.0 with Python 3.13.7, Graphify (graphifyy) 0.9.72 with Python 3.12.11. PowerShell 7 and other tool versions are not established configurations. Serena and Graphify versions are enforced exactly; Git, uv and rg must be available on PATH. Tool interface checks also run; a version match alone is insufficient. These checks never install or upgrade software.

Git, rg, uv and both separately installed uv tools are required. Python runs inside the two tool environments; a separate `python` command on PATH is unnecessary for normal use. Node.js and Pyright are language-specific, not universal RepoNav dependencies. Codex and a Serena MCP connection are optional and separate.

## Install

Obtain the reviewed RepoNav source folder (later, clone the published repository). Keep all release files together in a user-writable directory. No administrator rights are needed. The release is source-only; no binaries are supplied.

Install [Git for Windows](https://gitforwindows.org/), [ripgrep](https://github.com/BurntSushi/ripgrep/releases) and [uv](https://docs.astral.sh/uv/getting-started/installation/) through their official distributions. Provision the stated Python runtimes before installing the uv tools; uv may download a missing runtime when executing the commands below. In a new PowerShell window, check `git --version`, `rg --version` and `uv --version`. The following are proposed new-machine provisioning commands, not executed installation tests in B3.1. Run them only when you intend to download/install tools; do not run them over an existing installation without reviewing it:

```powershell
uv tool install --python 3.13.7 'serena-agent==1.7.0'
uv tool install --python 3.12.11 'graphifyy==0.9.72'
uv tool dir
uv tool dir --bin
```

Add the directory reported by `uv tool dir --bin` to your **user** PATH manually if necessary, then open a new shell. Do not upgrade Serena automatically. A fresh-machine install of these pins remains unverified; upstream availability and transitive dependency resolution require final validation.

The supported launcher is the included `repo-nav.cmd`. It locates the script beside itself and keeps your current directory. From any directory:

```powershell
$repoNav = Join-Path $env:USERPROFILE 'Tools\RepoNav'
& (Join-Path $repoNav 'repo-nav.cmd') status
```

Substitute your own installation location. Spaces and Unicode paths are supported by the tested launcher. Optionally add the RepoNav folder to your user PATH manually, after `Get-Command repo-nav -All -ErrorAction SilentlyContinue` confirms no existing command would be shadowed. Do not copy the shim away from its sibling files. Nothing edits PATH or overwrites a command automatically.

Direct invocation remains available:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoNav 'repo-nav.ps1') status
```

The launcher uses process-local execution-policy bypass; organizational policy may still prohibit script execution. Review downloaded scripts before running them.

## Language servers: prepare before indexing

Serena indexing starts language servers. `project create --index` and `project index` can trigger installation; independent MCP sessions can do the same when starting servers. `status` and RepoNav's prerequisite checks do not provision servers.

In the inspected Serena 1.7.0 source, Python defaults to `uvx -p 3.13 --from pyright==1.1.403 pyright-langserver --stdio` (or `uv tool run`). That may create environments and download Python/packages. An unrelated Pyright on PATH is not automatically selected. TypeScript checks its managed server location and runs npm install for TypeScript and typescript-language-server if absent. Having a global server on PATH is not sufficient. Other languages have their own adapters and are unverified here.

Serena's supported `ls_specific_settings` in `serena_config.yml` can point to an already provisioned executable, bypassing managed setup for these adapters. Example, requiring your own verified executable path:

```yaml
ls_specific_settings:
  python:
    ls_path: '<absolute path to verified pyright-langserver.exe>'
```

`ls_base_cmd` is also supported for an explicit command list. A Python Pyright wrapper can itself download Node, so verify the complete launcher and its dependencies; merely setting `ls_path` is insufficient. Node is needed for Node-based Pyright and TypeScript servers, but not for repositories using servers that do not require it. The Slice A Node 24.14.0/Pyright 1.1.403 observations are not evidence of integration compatibility.

There is no blanket no-install switch established by this investigation. Network blocking alone is insufficient. Provision and inspect the selected server in a disposable Windows environment with constrained filesystem/process access before running indexing. RepoNav preserves reviewed global settings in its temporary Serena home, but it is not a process sandbox. Do not use the legacy polyglot integration script until every selected language server has been reviewed and provisioned.

## First use and commands

Open a trusted Git working tree with `Set-Location`, then use the full launcher path or your manually configured PATH command:

```powershell
& (Join-Path $repoNav 'repo-nav.cmd') status
& (Join-Path $repoNav 'repo-nav.cmd') ensure
& (Join-Path $repoNav 'repo-nav.cmd') status
& (Join-Path $repoNav 'repo-nav.cmd') refresh
```

`ensure` initializes a missing Serena index and missing Graphify code graph. It reuses existing graphs and reports stale code without automatically refreshing it. `status` checks tools and reports state without indexing. `refresh` explicitly reconciles new, changed and deleted code through Graphify's native incremental operation; it does not run a semantic/LLM refresh. Confirm a new graph is current, repeat ensure to check reuse, then change a disposable code file and check status/refresh/status.

`bootstrap-all [--dry-run] <root> [<root2> ...]` processes discovered repositories sequentially. Start with `--dry-run`. It does **not** install the underlying tools. Indexing the discovered languages still requires the language-server preparation above.

## Side effects and optional AI setup

Indexing creates `.serena/` and `graphify-out/` state. Ensure can configure Git-global exclusions for those paths and add a local Git metadata exclusion for an existing generated-only, untracked .gitattributes. Existing safe user settings are preserved. RepoNav does not install Graphify hooks because that installer also changes tracked attributes. The preservation guard detects tool changes to tracked/policy files after execution; it does not undo them.

Codex/Serena MCP setup is separate. RepoNav reports optional MCP registration, but does not configure every AI client. Independently launched Serena is outside RepoNav cache protections. Existing executable Serena caches are preserved and indexing is skipped even if an earlier RepoNav run produced them. Filesystem junctions/symlinks, multiply linked state files and unclear ownership can cause refusal; use an ordinary trusted working tree. Removing RepoNav does not remove generated state or undo Git exclusions. Review those files/settings before any manual cleanup.

RepoNav itself is not designed to upload source. AI clients may send repository content to their providers under their own settings. The installed Graphify 0.9.72 HTML exporter references CDN-hosted vis-network JavaScript; opening generated HTML can contact that CDN. No blanket offline, privacy or malicious-tool containment guarantee is made.

## Troubleshooting

- Missing prerequisite: use `Get-Command git,rg,uv,serena,graphify` and check the tool bin directory on PATH. No automatic installation is attempted.
- Unsupported version/interface: inspect `uv tool list` and the error. Only Serena 1.7.0/Graphify 0.9.72 are supported; do not resolve this by blindly upgrading.
- Graphify environment mismatch: check `Get-Command graphify -All`, `uv tool dir` and `UV_TOOL_DIR`. The PATH launcher must match that uv environment's receipt. Remove the unintended PATH entry or select the intended existing environment explicitly.
- Missing server/download request: stop and pre-provision the selected adapter in a disposable environment. Check `ls_path` and its complete runtime. Offline mode does not prohibit local installation writes.
- Unsafe cache/state: preserve it for owner review. Use a separate clean checkout rather than deleting or trusting pickle caches. Redirected state, unclear ownership and migration-prone configurations require explicit investigation.
- Git ownership rejection: inspect ownership and trust before changing Git settings; RepoNav does not add safe.directory automatically.

## Integration release gate

On a disposable Windows machine/account boundary, install the pinned tools, provision and verify one Python server without installer fallback, and run recognition/status/ensure/repeated ensure/change/refresh/delete/refresh. Confirm graph contents, unchanged policy/tracked files and preserved user configuration. Exercise refusal cases separately. Record actual tool/runtime/server versions and results. Until then, launch/status checks, source inspection and mocks must not be described as real indexing validation.
