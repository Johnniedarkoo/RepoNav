---
name: repo-nav
description: Find code fast in a Git repository that has a RepoNav code graph (a graphify-out/ folder at the repository root). Use BEFORE grepping or opening files one by one whenever you need to locate where something is defined, what calls it, how two parts connect, or which files a change touches. Local and free, so it saves tokens and stops searches in the wrong place.
---

<!-- Managed by repo-nav skill install; edits are overwritten. Remove with: repo-nav skill uninstall -->

# RepoNav: look it up before you grep

RepoNav keeps two local indexes of the repository current: a Graphify code graph (`graphify-out/`) and a Serena symbol index (`.serena/`). Both run on this machine. Asking them costs no model tokens and returns a handful of exact locations instead of pages of grep output.

## When the repository has `graphify-out/graph.json`

1. Ask the graph first, with the names or words from the task:
   - `graphify query "<question in plain words>"`: a small subgraph of the relevant code.
   - `graphify explain <Symbol>`: where a class or function lives, plus what it connects to.
   - `graphify path <A> <B>`: how two things are linked.
   Exit code 3 means the name matched several nodes (pick one from the list shown); 4 means no match.
2. If the Serena MCP server is connected, use `find_symbol`, `find_referencing_symbols` and `get_symbols_overview` for exact definitions and every reference.
3. Open only the files and lines these point to. Fall back to `rg` for things the code graph does not hold: literal strings, configuration values, templates, translations.

## Freshness

The graph refreshes itself after commits, merges, branch switches and agent start-up when `repo-nav hooks install` has been run. If you have just edited code and need the new symbols, run `repo-nav refresh --quiet`; it only re-parses changed files. `repo-nav status` shows whether the graph is current or stale.

## When there is no graph yet

`repo-nav setup` (or `repo-nav ensure`) builds the indexes. It writes `.serena/` and `graphify-out/`, which RepoNav keeps out of Git, and can take a minute on a large repository, so ask the user before running it unless they asked you to set up navigation.

## Do not

- Run `graphify extract`, `graphify label` or other semantic commands: they call an LLM and cost tokens. RepoNav's refresh is code-only.
- Run `graphify hook install` or `graphify claude install`: they edit tracked files (`.gitattributes`, `CLAUDE.md`). RepoNav's hooks are Git-local.
- Delete or hand-edit `.serena/` or `graphify-out/`.
