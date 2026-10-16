# Local source snapshot preparation

This source-only v0.1.0 candidate is an initial experimental release. Licensing and owner provenance confirmation are complete. Safe mocks and regressions have passed; real end-to-end Serena/Graphify indexing and refresh remain unverified. Publication requires separate explicit owner authorization. The exporter performs no Git initialization or upload.

Use existing Python 3.9+ (standard library only), from this checkout:

```powershell
python -B prepare_snapshot.py --output "$env:TEMP\reponav-source-review"
```

The destination must not exist. Its parent must already exist, be outside the
source tree, and contain no symlink/junction/reparse ancestors. Use a private,
user-controlled directory; do not run while other processes can replace source
or destination paths. The tool detects ordinary link/path hazards but is not a
sandbox against hostile concurrent filesystem replacement.

`release-files.txt` is the authoritative exact-path allowlist, independent of Git
tracking and ignore state. New files require deliberate review and a manifest
edit. The manifest and publication tooling are deliberately included so the
snapshot can reproduce itself. Finalized LICENSE and THIRD_PARTY_NOTICES.md are required entries. Only the exact extensionless LICENSE filename is permitted for licensing.

The exporter reads only the manifest and named ordinary source files. It never
invokes Git or accesses commits, objects, custom refs, or arbitrary unlisted
files. It copies bytes, checks the exact output tree, hashes both output and
source again, and prints sorted relative filenames and SHA-256 values as JSON.
Capture this summary outside the snapshot if desired. No summary file is added
to its allowlisted contents. A failed output is not approved: partial directories
are deliberately left untouched for inspection; choose a new destination for a
retry. There is no overwrite, automatic rollback, or cleanup operation.

Excluded classes include Git metadata/history, navigation caches/graphs, local
audits, machine settings, tool installations, third-party environments and binary
assets. These remain excluded even when tracked, unless someone changes the
manifest and applicable validator rules. `.gitignore` provides defense in depth;
it neither authorizes publication nor replaces content review. Existing runtime
and real integration test scripts are source assets only; both test entry points now establish a disposable global Git environment.
Real integration still requires a no-install dependency preflight: installed
Serena language adapters can invoke installers; Git isolation does not disable them.

Local scanning rejects common token/private-key formats, quoted credential
assignments, non-synthetic email addresses, absolute drive/UNC paths, and fingerprinted
audit-specific private identifiers. It rejects binary/non-UTF-8 content and
non-source extensions. Errors never echo candidate content or OS diagnostics.
This is deliberately conservative and may reject legitimate examples: revise
examples or review the scanner explicitly, rather than bypass it silently.
Patterns are not exhaustive secret detection, legal clearance, or proof that
private snippets are absent. Review every allowed file and dependency provenance
before any future publication. No source is sent to a scanning service.

Run the isolated publication tests only:

```powershell
python -B -m unittest -v test_prepare_snapshot
```

Tests use synthetic temporary trees. The Windows junction test creates and
removes only its own disposable link; it skips if that capability is unavailable.
No real Git configuration, refs, repository tools or caches are modified.
