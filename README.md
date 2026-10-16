# RepoNav

RepoNav is a small Windows command-line helper that prepares repository navigation tools in a Git working tree. Serena provides symbol navigation through language servers; Graphify maps code relationships; RepoNav checks their installations, initializes missing state, and reports when a code graph needs refreshing.

Use it when you want the same navigation setup across several repositories without repeating setup commands yourself.

**v0.1.0 is an initial experimental release:** safe regression and launcher checks pass, but real Serena indexing and Graphify indexing/refresh after hardening remain unverified. Fresh-machine dependency installation is also unverified.

## Windows setup

The initial configuration is Windows PowerShell 5.1, Git for Windows, ripgrep (`rg`), uv, and independently installed uv tools: **Serena 1.7.0** and **Graphify 0.9.72**. Other versions and operating systems are not established configurations. Language servers need separate preparation; RepoNav does not install these tools or configure every AI coding assistant.

Keep the source folder together under your user tools directory. Follow [INSTALL.md](INSTALL.md) for pinned Python runtimes, dependency installation and language-server precautions.

## First use

After following INSTALL.md and preparing the selected language server, open a trusted Git working tree. This example assumes RepoNav is in `Tools/RepoNav` under your user profile:

```powershell
$repoNav = Join-Path $env:USERPROFILE 'Tools/RepoNav/repo-nav.cmd'
& $repoNav status
& $repoNav ensure
& $repoNav status
```

The full path works without a PATH change and preserves your current directory.

## Commands

| Command | What it does |
| --- | --- |
| `ensure` | Checks dependencies; initializes missing Serena project/index and missing Graphify code graph when safety checks permit. Reports stale existing graphs without rebuilding them. |
| `status` | Reports tools and repository state without indexing or changing repository configuration. Temporary scan storage may be created. |
| `refresh` | Explicitly asks Graphify to reconcile new, changed and deleted code through its native incremental operation. It does not reindex Serena or run a semantic/LLM refresh. |
| `bootstrap-all [--dry-run] <root> [<root2> ...]` | Discovers repositories under supplied roots and processes them sequentially. Start with `--dry-run`. This processes repositories; it is not a dependency installer. |

## Behavior, privacy and limits

RepoNav may create or update `.serena/` and `graphify-out/`, configure Git-global exclusions for these directories, and add a narrow local Git metadata exclusion for an existing generated `.gitattributes`. Existing Graphify hooks are left in place; RepoNav does not install them. It invokes independently installed developer tools, whose language-server setup can download software or perform installation writes. See [INSTALL.md](INSTALL.md) before indexing.

RepoNav itself is not designed to upload source code. AI coding clients may transmit repository content to their providers according to their own settings. Graphify-generated HTML loads JavaScript from a CDN when opened. Offline operation is not guaranteed.

Existing untrusted serialized Serena caches are preserved and indexing is skipped, including caches created by an earlier RepoNav run. Unsafe state, filesystem junctions/symlinks and some existing configurations may be refused. Independently launched Serena MCP services are outside RepoNav's protection boundary. Language support depends on RepoNav's limited detection mapping and the installed tools and servers; every language is not supported.

Preservation checks detect tracked-file and policy-file changes after tool execution and report failure; they do not prevent or undo those writes. RepoNav is not a sandbox against malicious tools or concurrent filesystem changes. Removing its source folder does not remove generated state or restore configuration.

## Licensing

RepoNav's original code is licensed under the [MIT License](LICENSE), Copyright (c) 2026 Johnniedarkoo. External tools retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
