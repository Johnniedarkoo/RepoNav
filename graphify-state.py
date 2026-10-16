"""Small adapter to Graphify's own detection and official hook rebuild path.

Run with the Python from the installed uv tool, never an unrelated Python.
Status puts Graphify's scan caches in a temporary directory, leaving the repo
untouched. No extraction, merge, clustering, or locking algorithms live here.
"""
import contextlib
import io
import importlib.metadata
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from state_safety import inspect_state


def inspect(root, read_only=True):
    inspect_state(root, "graphify", "inspect" if read_only else "refresh")
    out = root / "graphify-out"
    # Be conservative: even a stale lock marker requires investigation, not a
    # competing rebuild. Graphify removes its lock when it finishes normally.
    if (out / ".rebuild.lock").exists() or any(out.glob(".pending_changes*")):
        return {"state": "update pending/running", "changed": []}
    graph_path = out / "graph.json"
    if not graph_path.exists():
        return {"state": "missing", "changed": []}
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8-sig"))
        if not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("links", graph.get("edges")), list):
            raise ValueError("missing node/edge arrays")
        marker = out / ".graphify_root"
        if marker.exists() and (root / marker.read_text(encoding="utf-8-sig").strip()).resolve() != root:
            raise ValueError("graph root differs from repository")
    except (OSError, ValueError, TypeError) as exc:
        return {"state": "graph invalid", "detail": str(exc), "changed": []}
    from graphify.detect import detect_incremental, classify_file
    from graphify.watch import _read_build_excludes, _read_build_gitignore
    # Native AST hashes distinguish actual content changes from mtime drift.
    # Native ignores/corpus classification include new and deleted files too.
    with tempfile.TemporaryDirectory(prefix="reponav-scan-") as cache:
        cache_root = Path(cache) if read_only else root
        if read_only and (out / "cache/stat-index.json").is_file():
            target = cache_root / "graphify-out/cache/stat-index.json"
            target.parent.mkdir(parents=True)
            shutil.copyfile(out / "cache/stat-index.json", target)
        delta = detect_incremental(root, manifest_path=str(out / "manifest.json"),
                                   kind="ast", extra_excludes=_read_build_excludes(out) or None,
                                   gitignore=_read_build_gitignore(out), cache_root=cache_root)
    changed = list(delta["new_files"].get("code", []))
    changed += [p for p in delta.get("deleted_files", []) if classify_file(Path(p)) == "code"]
    semantic = (out / "needs_update").exists() or any(
        delta["new_files"].get(k) for k in ("document", "paper", "image", "audio", "video"))
    return {"state": f"stale — {len(changed)} code files changed" if changed else "graph current",
            "changed": changed, "semantic": semantic,
            "nodes": len(graph["nodes"]), "edges": len(graph.get("links", graph.get("edges", [])))}


def main():
    version = importlib.metadata.version("graphifyy")
    if version != "0.9.72":
        raise RuntimeError("RepoNav supports Graphify 0.9.72 only; see INSTALL.md")
    root = Path(os.path.abspath(sys.argv[2]))
    with contextlib.redirect_stdout(io.StringIO()):
        state = inspect(root, read_only=sys.argv[1] == "status")
    if sys.argv[1] == "status":
        print(json.dumps(state, ensure_ascii=True))
        return 0
    if state["state"] in ("missing", "graph invalid"):
        print(state["state"] + "; run ensure for a missing graph; repair an invalid graph explicitly")
        return 1
    if state["state"] == "update pending/running" or not state["changed"]:
        print(state["state"])
        return 0
    # Same native changed_paths operation used by official post-commit hooks.
    # Keep clustering/labels and native merge, shrink guard, queue and lock.
    # CLI `update` has no changed-file argument and reprocesses the corpus.
    from graphify.watch import _rebuild_code
    ok = _rebuild_code(root, changed_paths=[Path(p) for p in state["changed"]])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
