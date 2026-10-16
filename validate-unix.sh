#!/bin/sh
# Unix counterpart of TestEnvironment.ps1 + Validate-*.ps1: runs the Python
# regression suites with user state redirected into a disposable directory.
# Never installs tools; uses the installed Serena uv tool Python when present.
set -eu
here=$(cd "$(dirname "$0")" && pwd -P)

tool_dir=${UV_TOOL_DIR:-}
if [ -z "$tool_dir" ] && command -v uv >/dev/null 2>&1; then
    tool_dir=$(uv tool dir 2>/dev/null || true)
fi
python=$(command -v python3)
if [ -n "$tool_dir" ] && [ -x "$tool_dir/serena-agent/bin/python" ]; then
    python=$tool_dir/serena-agent/bin/python
fi

# Resolve the temp base: macOS /tmp and /var are symlinks, which the state
# safety checks rightly refuse as repository ancestors.
base=$(cd "${TMPDIR:-/tmp}" && pwd -P)
root=$(mktemp -d "$base/reponav-isolated-XXXXXXXX")
cleanup() {
    if find "$root" -type l | grep -q .; then
        echo "Linked artifact retained under $root; cleanup refused" >&2
        return
    fi
    rm -rf "$root"
}
trap cleanup EXIT INT TERM
for dir in home config cache data tmp codex serena; do mkdir "$root/$dir"; done
: >"$root/gitconfig"
: >"$root/system-gitconfig"

unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CONFIG GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS
export GIT_CONFIG_GLOBAL="$root/gitconfig" GIT_CONFIG_SYSTEM="$root/system-gitconfig" GIT_CONFIG_NOSYSTEM=1
export HOME="$root/home" XDG_CONFIG_HOME="$root/config" XDG_CACHE_HOME="$root/cache" XDG_DATA_HOME="$root/data"
export TMPDIR="$root/tmp" CODEX_HOME="$root/codex" SERENA_HOME="$root/serena" UV_CACHE_DIR="$root/cache"
export UV_OFFLINE=1 PIP_NO_INDEX=1 GRAPHIFY_NO_AUTO_REFRESH=1
if [ -n "$tool_dir" ]; then export UV_TOOL_DIR="$tool_dir"; fi

cd "$here"
"$python" -B -m unittest -v test_state_safety test_preservation test_dependencies test_prepare_snapshot test_repo_nav_unix
