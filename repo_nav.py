"""RepoNav for Linux and macOS: the Unix counterpart of repo-nav.ps1.

Same commands, messages and refusal rules as the Windows script. Safety
decisions stay in the shared helpers (state_safety.py, preservation-state.py,
graphify-state.py, check_dependencies.py), each run in its own process with the
Python of the uv tool it inspects, exactly as repo-nav.ps1 does. Standard
library only; Python 3.9+.
"""
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time

SCRIPT_ROOT = Path(__file__).resolve().parent
USAGE = 'Usage: repo-nav ensure [--languages a,b] | status | refresh | bootstrap-all [--dry-run] <root> [<root2> ...]'
BOOTSTRAP_USAGE = 'Usage: repo-nav bootstrap-all [--dry-run] <root> [<root2> ...]'
STATE_ENTRIES = ('.serena/', 'graphify-out/')
SUPPORTED = (('serena-agent', '1.7.0', 'serena'), ('graphifyy', '0.9.72', 'graphify'))

# Default server IDs/extensions verified against installed Serena 1.7.0.
LANGUAGE_EXTENSIONS = {
    'python': ('.py', '.pyi'), 'typescript': ('.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs'),
    'go': ('.go',), 'rust': ('.rs',), 'java': ('.java',), 'kotlin': ('.kt', '.kts'),
    'cpp': ('.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hxx', '.cu', '.mm'),
    'csharp': ('.cs',), 'powershell': ('.ps1', '.psm1'), 'ruby': ('.rb', '.erb'),
    'php': ('.php', '.phtml'), 'dart': ('.dart',), 'bash': ('.sh', '.bash'),
    'swift': ('.swift',), 'lua': ('.lua',), 'luau': ('.luau',), 'zig': ('.zig',),
    'fsharp': ('.fs', '.fsx', '.fsi'), 'scala': ('.scala',), 'elixir': ('.ex', '.exs'),
    'vue': ('.vue',), 'svelte': ('.svelte',), 'terraform': ('.tf', '.tfvars'),
}
EXCLUDED_SOURCE = re.compile(r'(^|/)(node_modules|\.venv|venv|vendor|third[_-]party|external|generated|results|'
                             r'graphify-out|\.serena|build|dist|fixtures?|testdata)(/|$)')
GENERATED_SOURCE = re.compile(r'(\.min\.js|\.generated\.[^/]+|_pb2\.py|\.pb\.go)$')
SKIPPED_TRAVERSAL = {'.git', '.serena', 'graphify-out', 'node_modules', '.venv', 'venv', '__pycache__',
                     '.pytest_cache', 'build', 'dist'}
SKIPPED_INSIDE_REPOSITORY = {'results', 'logs', '.local', '.tmp', '.scratch', 'scratch', 'tmp', 'temp'}


class RepoNavError(Exception):
    pass


# --- process helpers -------------------------------------------------------

def run(args, cwd=None, env=None):
    """Run a native tool unattended; return (exit code, output lines)."""
    environment = dict(os.environ if env is None else env, GRAPHIFY_NO_AUTO_REFRESH='1')
    try:
        result = subprocess.run([str(a) for a in args], cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        return 127, [f'{args[0]}: not found']
    text = result.stdout.decode('utf-8', 'replace')
    return result.returncode, [line for line in text.splitlines()]


def in_repository_env():
    return dict(os.environ, PYTHONHASHSEED='0')


def tool_python(distribution):
    code, output = run(['uv', 'tool', 'dir'])
    if code != 0 or not output:
        raise RepoNavError(f'Cannot locate the uv {distribution} installation.')
    python = Path(output[0].strip()) / distribution / 'bin' / 'python'
    if not python.is_file():
        if distribution == 'graphifyy':
            raise RepoNavError('Graphify must use its uv tool installation.')
        raise RepoNavError('Installed Serena Python is required for safe configuration inspection.')
    return python


def helper_json(python, helper, *arguments, cwd=None, env=None):
    code, output = run([python, '-B', SCRIPT_ROOT / helper, *arguments], cwd=cwd, env=env)
    if code != 0:
        raise RepoNavError('\n'.join(output).strip())
    return json.loads('\n'.join(output))


# --- path safety -----------------------------------------------------------

def assert_ordinary_path(path, message='Redirected repository/scan root is unsafe.'):
    """Check every ancestor before resolution can erase evidence of indirection."""
    candidate = Path(os.path.abspath(path))
    for part in (*reversed(candidate.parents), candidate):
        if stat.S_ISLNK(part.lstat().st_mode):
            raise RepoNavError(message)


def assert_ordinary_write_target(path):
    candidate = Path(os.path.abspath(path))
    for part in (*reversed(candidate.parents), candidate):
        try:
            info = part.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise RepoNavError('STATE SAFETY: redirected Git metadata target refused.')


def state_safety(root, scope='all', operation='inspect'):
    # A dedicated process boundary imports only the helper plus Serena's
    # installed safe YAML reader.
    return helper_json(tool_python('serena-agent'), 'state_safety.py', root, scope, operation)


# --- git -------------------------------------------------------------------

def format_git_failure(code, output, stage, path):
    detail = '\n'.join(output)
    if re.search(r'detected dubious ownership|safe\.directory', detail):
        kind = 'Git ownership/trust rejection'
    elif re.search(r'permission denied|access (is )?denied|operation not permitted', detail, re.I):
        kind = 'Git permission/access failure'
    elif re.search(r'not a git repository|must be run in a work tree', detail):
        kind = 'Not inside a Git working tree'
    else:
        kind = f'Git command failure (exit {code})'
    return f'[{stage}] {kind} at {path}. Stopped.\n{detail}'


def repository_root():
    cwd = os.getcwd()
    assert_ordinary_path(cwd)
    code, output = run(['git', 'rev-parse', '--show-toplevel'])
    if code != 0 or not output:
        raise RepoNavError(format_git_failure(code, output, 'repository detection', cwd))
    return str(Path(output[0].strip()).resolve())


def git_path(root, name):
    code, output = run(['git', 'rev-parse', '--git-path', name], cwd=root)
    if code != 0 or not output:
        raise RepoNavError(format_git_failure(code, output, f'repository detection: git-path {name}', root))
    candidate = Path(output[0].strip())
    target = candidate if candidate.is_absolute() else Path(root) / candidate
    assert_ordinary_write_target(target)
    return target


def is_tracked(root, relative):
    code, output = run(['git', 'ls-files', '--', relative], cwd=root)
    return code == 0 and bool(output)


def stage(name, path, action):
    try:
        return action()
    except RepoNavError as error:
        raise RepoNavError(f'[{name}] Path: {path}. Stopped.\n{error}') from None


# --- preservation guard ----------------------------------------------------

def protected_policy_path(path):
    parts = path.replace('\\', '/').split('/')
    if parts[-1] in ('CLAUDE.md', 'CLAUDE.local.md', 'AGENTS.md', 'AGENTS.override.md', 'GEMINI.md'):
        return True
    return bool(re.search(r'(^|/)(\.claude|\.codex|\.cursor)(/|$)|(^|/)\.github/(instructions(/|$)|copilot-instructions\.md$)',
                          path))


def tracked_snapshot(root):
    try:
        snapshot = helper_json(tool_python('graphifyy'), 'preservation-state.py', root)
    except RepoNavError as error:
        raise RepoNavError(f'Preservation snapshot failed at {root}. {error}') from None
    return snapshot['records']


def guarded(root, name, action):
    before = tracked_snapshot(root)
    result = failure = None
    try:
        result = action()
    except (OSError, ValueError, RepoNavError) as error:
        failure = error
    after = tracked_snapshot(root)
    changed_tracked = sorted({key[2:] for key in set(before) | set(after) if before.get(key) != after.get(key)})
    changed_policy = [path for path in changed_tracked if protected_policy_path(path)]
    if changed_policy:
        raise RepoNavError(f'TRACKED WORKFLOW POLLUTION\nRepoNav stage: {name}\nRepository: {root}\n'
                           f'Affected path(s): {", ".join(changed_policy)}\n'
                           'No files were reverted; inspect and repair the change before continuing.')
    if changed_tracked:
        raise RepoNavError(f'TRACKED FILE POLLUTION\nRepoNav stage: {name}\nRepository: {root}\n'
                           f'Affected tracked path(s): {", ".join(changed_tracked)}\n'
                           'No files were reverted; inspect and repair the change before continuing.')
    if failure is not None:
        raise failure
    return result


# --- exclusions ------------------------------------------------------------
# Unix keeps both state directories out of Git with the repository's own
# info/exclude instead of the user's global core.excludesFile.

def read_lines(path):
    try:
        return Path(path).read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return []


def exclude_state(root):
    path = git_path(root, 'info/exclude')
    try:
        lines = {line.strip() for line in read_lines(path)}
        return {'path': path, 'readable': True, 'entries': {entry: entry in lines for entry in STATE_ENTRIES + ('.gitattributes',)}}
    except OSError:
        return {'path': path, 'readable': False, 'entries': {}}


def ensure_local_exclude(root, path, entry):
    assert_ordinary_write_target(path)
    if is_tracked(root, entry.rstrip('/')):
        return f'{entry} tracked; not excluded'
    lines = read_lines(path)
    if any(line.strip() == entry for line in lines):
        return f'{entry} present'
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as stream:
        if lines and not Path(path).read_bytes().endswith(b'\n'):
            stream.write('\n')
        stream.write(entry + '\n')
    return f'{entry} added'


def ensure_optional_local_exclude(root, path, entry):
    try:
        return ensure_local_exclude(root, path, entry)
    except (OSError, RepoNavError):
        return f'WARNING: optional {entry} exclusion unavailable'


# --- tools -----------------------------------------------------------------

def tool_info(name):
    path = shutil.which(name)
    if path is None:
        return {'available': False, 'version': 'missing', 'path': ''}
    code, output = run([path, '--version'])
    return {'available': code == 0 or name == 'rg', 'version': output[0].strip() if output else 'available', 'path': path}


def assert_supported_dependencies():
    for name in ('git', 'uv', 'rg', 'serena', 'graphify'):
        if shutil.which(name) is None:
            raise RepoNavError(f'Missing prerequisite: {name}. See INSTALL.md. No software was installed.')
    for distribution, version, launcher in SUPPORTED:
        code, output = run([tool_python(distribution), '-B', SCRIPT_ROOT / 'check_dependencies.py',
                            distribution, version, launcher, shutil.which(launcher)])
        if code != 0:
            raise RepoNavError('\n'.join(output))


def require_tools():
    tools = {}
    for name in ('rg', 'serena', 'graphify'):
        tools[name] = tool_info(name)
        if not tools[name]['available']:
            raise RepoNavError(f'[{name} executable] {tools[name]["path"]}: {tools[name]["version"]}. Stopped; required tool unavailable.')
    return tools


# --- Serena ----------------------------------------------------------------

def serena_languages(root):
    code, output = run(['git', '-c', 'core.quotePath=false', 'ls-files'], cwd=root)
    if code != 0:
        raise RepoNavError(format_git_failure(code, output, 'Serena project/index: tracked source detection', root))
    found = set()
    for path in output:
        if EXCLUDED_SOURCE.search(path) or GENERATED_SOURCE.search(path) or not (Path(root) / path).is_file():
            continue
        extension = os.path.splitext(path)[1].lower()
        found.update(language for language, extensions in LANGUAGE_EXTENSIONS.items() if extension in extensions)
    return sorted(found)


def safe_serena_index(root, arguments):
    base = Path(os.path.realpath(tempfile.gettempdir()))
    assert_ordinary_path(base)
    token = secrets.token_hex(16)
    temporary = base / f'reponav-serena-{token}'
    serena_home = temporary / 'home'
    temporary.mkdir(mode=0o700)
    serena_home.mkdir()
    (temporary / '.owner').write_text(token, encoding='utf-8')
    try:
        code, output = run([tool_python('serena-agent'), '-B', SCRIPT_ROOT / 'state_safety.py', 'prepare-home', root, serena_home])
        if code != 0:
            raise RepoNavError('\n'.join(output))
        env = dict(in_repository_env(), SERENA_HOME=str(serena_home))
        # Repeat project-state checks immediately before the consumer, using the
        # prepared global configuration rather than the user's registration list.
        check = helper_json(tool_python('serena-agent'), 'state_safety.py', root, 'serena', 'ensure', env=env)
        if check['serena']['serialized']:
            raise RepoNavError('STATE SAFETY: executable cache appeared before Serena invocation.')
        return run(['serena', *arguments], cwd=root, env=env)
    finally:
        if temporary.resolve() != base / f'reponav-serena-{token}':
            raise RepoNavError('Unsafe Serena temporary cleanup target')
        marker = temporary / '.owner'
        if stat.S_ISLNK(marker.lstat().st_mode) or marker.read_text(encoding='utf-8').strip() != token:
            raise RepoNavError('Unsafe Serena temporary ownership marker')
        for directory, subdirectories, files in os.walk(temporary):
            for name in subdirectories + files:
                if stat.S_ISLNK(os.lstat(os.path.join(directory, name)).st_mode):
                    raise RepoNavError('Linked Serena temporary artifact retained; cleanup refused')
        shutil.rmtree(temporary)


def ensure_serena(root, languages=None):
    safety = state_safety(root, 'serena', 'ensure')['serena']
    if safety['serialized']:
        return ('WARNING — unverified executable Serena cache preserved; indexing skipped. Review provenance and prepare a '
                'separate clean state before retrying. Independent MCP launches are not protected.')
    if safety['config'] and not safety['cache_files'] and not safety['index_safe']:
        return 'WARNING — existing Serena configuration requires migration; indexing skipped to preserve it. Review and migrate it explicitly before retrying.'
    if safety['config'] and safety['cache_files'] > 0:
        return 'existing index'
    if (Path(root) / '.serena' / 'project.yml').exists():
        code, output = safe_serena_index(root, ['project', 'index', root])
        action = 'indexed'
    else:
        languages = languages or serena_languages(root)
        if not languages:
            return 'WARNING — no supported active source language detected'
        arguments = ['project', 'create', root, '--index']
        for language in languages:
            arguments += ['--ls', language]
        code, output = safe_serena_index(root, arguments)
        action = 'initialized and indexed'
    if code != 0:
        raise RepoNavError('Serena failed to initialize this repository.\n' + '\n'.join(output[-6:]))
    return action


def serena_mcp_state():
    path = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'
    codex = shutil.which('codex')
    if codex is None:
        return {'state': 'UNAVAILABLE', 'path': path, 'message': 'Codex executable unavailable; local indexing remains usable.'}
    code, output = run([codex, 'mcp', 'get', 'serena', '--json'])
    detail = '\n'.join(output)
    if code != 0:
        state = 'MISSING' if re.search(r'No MCP server named|not found|does not exist', detail) else 'UNAVAILABLE'
        return {'state': state, 'path': path, 'message': detail}
    try:
        server = json.loads(detail)
    except ValueError as error:
        return {'state': 'UNAVAILABLE', 'path': path, 'message': str(error)}
    if not server.get('enabled'):
        return {'state': 'DISABLED', 'path': path, 'message': 'Serena server disabled in effective Codex configuration.'}
    transport = server.get('transport') or {}
    if transport.get('type') != 'stdio':
        return {'state': 'INVALID', 'path': path, 'message': 'Expected a Serena STDIO server.'}
    args = [str(a) for a in transport.get('args') or []]
    context = '--context=codex' in args or re.search(r'(?:^| )--context codex(?: |$)', ' '.join(args))
    if 'start-mcp-server' not in args or '--project-from-cwd' not in args or not context:
        return {'state': 'INVALID', 'path': path, 'message': 'Expected STDIO start-mcp-server --project-from-cwd --context=codex.'}
    return {'state': 'CONFIGURED', 'path': path, 'message': 'Enabled in effective Codex configuration; fresh-session tool calls verify connectivity.'}


def format_serena_mcp_state(state):
    if state['state'] == 'CONFIGURED':
        return 'configured (enabled; connectivity requires a fresh Codex session)'
    return f'WARNING [Serena MCP configuration] {state["state"]}: {state["path"]}. {state["message"]} Continuing local navigation checks.'


# --- Graphify --------------------------------------------------------------

def graphify_state(root):
    state_safety(root, 'graphify', 'inspect')
    code, output = run([tool_python('graphifyy'), SCRIPT_ROOT / 'graphify-state.py', 'status', root],
                       cwd=root, env=in_repository_env())
    if code != 0:
        raise RepoNavError('Graphify freshness check failed: ' + '\n'.join(output))
    return json.loads('\n'.join(output))


def ensure_graphify(root):
    state_safety(root, 'graphify', 'ensure')
    state = graphify_state(root)
    if state['state'] != 'missing':
        message = state['state']
        if message.startswith('stale'):
            message += '; use repo-nav refresh when fresh architecture analysis is needed'
        if state.get('semantic'):
            message += '; documentation semantic refresh may be pending'
        return message
    code, output = run(['graphify', 'update', '.'], cwd=root, env=in_repository_env())
    if code != 0 or not (Path(root) / 'graphify-out' / 'graph.json').exists():
        raise RepoNavError('Graphify failed to build the repository graph.\n' + '\n'.join(output[-8:]))
    return 'initialized and built'


def graphify_refresh(root):
    state_safety(root, 'graphify', 'refresh')
    code, output = run([tool_python('graphifyy'), SCRIPT_ROOT / 'graphify-state.py', 'refresh', root],
                       cwd=root, env=in_repository_env())
    for line in output:
        print(line)
    if code != 0:
        raise RepoNavError('Graphify incremental refresh failed; inspect its diagnostics before retrying.')


def graphify_attribute_state(root):
    path = Path(root) / '.gitattributes'
    line = 'graphify-out/graph.json merge=graphify'
    lines = [entry.strip() for entry in read_lines(path)] if path.exists() else []
    return {'tracked': is_tracked(root, '.gitattributes'), 'exists': path.exists(),
            'graphify_line': line in lines,
            'other_content': any(entry and not entry.startswith('#') and entry != line for entry in lines)}


def graphify_hook_status(root):
    state_safety(root, 'graphify', 'inspect')
    code, output = run(['graphify', 'hook', 'status'], cwd=root, env=in_repository_env())
    text = '\n'.join(output)
    return code == 0 and bool(re.search(r'post-commit:\s+installed', text) and re.search(r'post-checkout:\s+installed', text)
                              and re.search(r'merge driver:\s+registered', text))


def ensure_graphify_hooks(root):
    state_safety(root, 'graphify', 'inspect')
    if graphify_hook_status(root) and graphify_attribute_state(root)['graphify_line']:
        return 'hooks installed'
    return 'not installed — Graphify hook install also writes .gitattributes; no Git-local-only installer is available'


# --- commands --------------------------------------------------------------

def write_status(root):
    safety = state_safety(root, 'all', 'inspect')
    excludes = exclude_state(root)
    print(f'Repository: {root}')
    for name in ('rg', 'serena', 'graphify'):
        tool = tool_info(name)
        print(f'{name:<9} executable OK  {tool["version"]}' if tool['available'] else f'{name:<9} ERR missing')
    serena = safety['serena']
    project = (Path(root) / '.serena' / 'project.yml').exists()
    serena_text = 'indexed' if project and serena['cache_files'] > 0 else 'project exists; index missing' if project else 'not initialized'
    if serena['serialized']:
        serena_text = 'unverified executable cache; RepoNav indexing skipped; independent MCP launches are not protected'
    if serena['category'] == 'unclear-ownership':
        serena_text = 'existing state preserved; ownership review required before initialization'
    elif not serena['serialized'] and serena['config'] and not serena['cache_files'] and not serena['index_safe']:
        serena_text = 'configuration preserved; migration review required before indexing'
    graph = stage('Graphify graph', root, lambda: graphify_state(root))
    graph_text = graph['state'] + ('; documentation semantic refresh may be pending' if graph.get('semantic') else '')
    if safety['graphify']['category'] == 'unclear-ownership':
        graph_text += '; existing output preserved; ownership review required before initialization'
    hooks = stage('Graphify hooks', root, lambda: graphify_hook_status(root))
    hook_text = ('hooks installed' if hooks else 'not installed — .gitattributes conflict'
                 if graphify_attribute_state(root)['other_content'] else 'not installed')
    print(f'Serena project: {serena_text}')
    print(f'Serena Codex MCP: {format_serena_mcp_state(serena_mcp_state())}')
    print(f'Graphify {graph_text}; {hook_text}')
    complete = excludes['readable'] and all(excludes['entries'].get(entry) for entry in STATE_ENTRIES)
    print(f'Git excludes: {excludes["path"]} ({"OK" if complete else "MISSING" if excludes["readable"] else "INACCESSIBLE"})')
    for entry in STATE_ENTRIES:
        mark = 'unavailable' if not excludes['readable'] else 'OK' if excludes['entries'][entry] else 'missing'
        print(f'  {entry:<15} {mark}')
    local = 'optional generated entry: OK' if excludes['entries'].get('.gitattributes') else 'optional generated entry: unavailable / not needed'
    print(f'Repo-local exclusions ({excludes["path"]}): {local}')


def ensure_repository(root, languages=None):
    state_safety(root, 'all', 'ensure')
    tools = require_tools()
    exclude_path = git_path(root, 'info/exclude')
    warnings, exclude_results = [], []
    for entry in STATE_ENTRIES:
        try:
            exclude_results.append(ensure_local_exclude(root, exclude_path, entry))
        except (OSError, RepoNavError) as error:
            warnings.append(f'[git-excludes] {exclude_path}: {error}; {entry} may appear untracked. Continuing navigation checks.')
    serena = guarded(root, 'Serena project/index', lambda: stage('Serena project/index', root, lambda: ensure_serena(root, languages)))
    mcp = serena_mcp_state()
    graph = guarded(root, 'Graphify graph (graphify update .)', lambda: stage('Graphify graph', root, lambda: ensure_graphify(root)))
    hooks = guarded(root, 'Graphify hook status', lambda: stage('Graphify hooks', root, lambda: ensure_graphify_hooks(root)))
    attributes = graphify_attribute_state(root)
    if attributes['exists'] and not attributes['tracked'] and not attributes['other_content']:
        exclude_results.append(ensure_optional_local_exclude(root, exclude_path, '.gitattributes'))
    if mcp['state'] != 'CONFIGURED':
        warnings.append(format_serena_mcp_state(mcp))
    if serena.startswith('WARNING'):
        warnings.append(serena)
    if graph == 'graph invalid':
        warnings.append(graph)
    if hooks.startswith('not installed'):
        warnings.append(hooks)
    warnings += [result for result in exclude_results if re.search(r'tracked; not excluded|^WARNING:', result)]
    return {'tools': tools, 'serena': serena, 'mcp': mcp, 'graph': graph, 'hooks': hooks, 'warnings': warnings}


# --- bootstrap-all ---------------------------------------------------------

def canonical(path):
    try:
        return str(Path(path).resolve(strict=True))
    except OSError:
        return None


def physical_key(path):
    assert_ordinary_path(path)
    info = os.stat(path)
    return f'{info.st_dev:x}:{info.st_ino:x}'


def skipped_directory(name, inside_repository):
    if name in SKIPPED_TRAVERSAL or name.startswith('baseline-checkout-'):
        return True
    if name.lower() in ('temporary benchmark checkouts', '.scratch') or re.search(r'(^|-)eval-runs$', name):
        return True
    return inside_repository and name in SKIPPED_INSIDE_REPOSITORY


def git_candidate(path):
    code, output = run(['git', '-C', path, 'rev-parse', '--is-bare-repository'])
    if code == 0 and output and output[0].strip() == 'true':
        return {'kind': 'Bare', 'path': path, 'reason': 'bare repository'}
    code, output = run(['git', '-C', path, 'rev-parse', '--is-inside-work-tree'])
    if code == 0 and output and output[0].strip() == 'true':
        code, output = run(['git', '-C', path, 'rev-parse', '--show-toplevel'])
        worktree = canonical(output[0].strip()) if code == 0 and output else None
        if worktree is not None:
            return {'kind': 'Worktree', 'path': worktree, 'reason': ''}
    return {'kind': 'Invalid', 'path': path, 'reason': 'Git metadata could not be validated'}


def bare_shape(path):
    path = Path(path)
    return (path / 'HEAD').is_file() and (path / 'config').is_file() and (path / 'objects').is_dir() and (path / 'refs').is_dir()


def find_candidates(root, visited):
    candidates, issues, pruned = [], [], []
    stack = [(root, False)]
    while stack:
        path, inside = stack.pop()
        try:
            assert_ordinary_path(path)
        except (OSError, RepoNavError):
            issues.append({'path': path, 'reason': 'redirected or inaccessible scan directory'})
            continue
        current = canonical(path)
        if current is None:
            issues.append({'path': path, 'reason': 'path could not be resolved'})
            continue
        if current in visited:
            continue
        visited.add(current)
        marker = Path(current) / '.git'
        try:
            if stat.S_ISLNK(marker.lstat().st_mode):
                issues.append({'path': current, 'reason': 'redirected Git metadata marker'})
                continue
        except FileNotFoundError:
            pass
        except OSError:
            issues.append({'path': current, 'reason': 'Git metadata marker inaccessible'})
            continue
        has_marker, has_bare = marker.exists(), bare_shape(current)
        candidate = None
        if current == root or has_marker or has_bare:
            candidate = git_candidate(current)
            if candidate['kind'] != 'Invalid' or has_marker or has_bare:
                candidates.append(candidate)
        if candidate is not None and candidate['kind'] == 'Bare':
            continue
        inside = inside or (candidate is not None and candidate['kind'] == 'Worktree')
        try:
            children = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError:
            issues.append({'path': current, 'reason': 'directory could not be read'})
            continue
        for child in children:
            if child.is_symlink():
                if child.is_dir():
                    pruned.append({'path': child.path, 'reason': 'redirected directory'})
                continue
            if not child.is_dir(follow_symlinks=False):
                continue
            if skipped_directory(child.name, inside):
                pruned.append({'path': child.path, 'reason': 'generated/heavy directory'})
                continue
            stack.append((child.path, inside))
    return candidates, issues, pruned


def bootstrap_line(status, path, reason=''):
    print(f'{status:<8} {path}  {reason}' if reason else f'{status:<8} {path}')


def bootstrap_all(arguments):
    dry_run, roots = False, []
    for argument in arguments:
        if argument.lower() == '--dry-run':
            if dry_run:
                print(BOOTSTRAP_USAGE)
                return 2
            dry_run = True
        elif argument.startswith('-'):
            print(f'Unknown bootstrap-all option: {argument}')
            print(BOOTSTRAP_USAGE)
            return 2
        else:
            roots.append(argument)
    if not roots:
        print(BOOTSTRAP_USAGE)
        return 2
    valid, problems = [], []
    for argument in roots:
        try:
            assert_ordinary_path(argument)
            if not Path(argument).is_dir():
                problems.append({'path': argument, 'reason': 'not a directory'})
                continue
            resolved = canonical(argument)
            if resolved is None:
                problems.append({'path': argument, 'reason': 'could not be resolved'})
                continue
            valid.append(resolved)
        except (OSError, RepoNavError):
            problems.append({'path': argument, 'reason': 'does not exist or is inaccessible'})
    for problem in problems:
        bootstrap_line('FAILED', problem['path'], problem['reason'])
    if not valid:
        print('No valid scan roots were supplied.')
        return 2

    started = time.monotonic()
    print(f'Discovering Git repositories under {", ".join(valid)}...')
    visited, seen, candidates, issues, pruned = set(), set(), [], [], []
    for root in valid:
        found, found_issues, found_pruned = find_candidates(root, visited)
        issues += found_issues
        pruned += found_pruned
        for candidate in found:
            path = canonical(candidate['path'])
            if path is None:
                issues.append({'path': candidate['path'], 'reason': 'candidate path could not be resolved'})
                continue
            candidate['path'] = path
            try:
                key = physical_key(path)
            except (OSError, RepoNavError):
                issues.append({'path': path, 'reason': 'physical repository identity unavailable'})
                continue
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)
    for issue in issues:
        bootstrap_line('FAILED', issue['path'], issue['reason'])

    counts = {'READY': 0, 'WARNING': 0, 'SKIPPED': 0, 'FAILED': 0}
    for candidate in sorted(candidates, key=lambda item: item['path']):
        if candidate['kind'] == 'Bare':
            bootstrap_line('SKIPPED', candidate['path'], 'bare repository')
            counts['SKIPPED'] += 1
        elif candidate['kind'] != 'Worktree':
            bootstrap_line('FAILED', candidate['path'], candidate['reason'])
            counts['FAILED'] += 1
        elif dry_run:
            bootstrap_line('DRY-RUN', candidate['path'], 'would run ensure')
            counts['READY'] += 1
        else:
            try:
                result = ensure_repository(candidate['path'])
                if result['warnings']:
                    bootstrap_line('WARNING', candidate['path'], '; '.join(result['warnings']))
                    counts['WARNING'] += 1
                else:
                    bootstrap_line('READY', candidate['path'])
                    counts['READY'] += 1
            except (OSError, ValueError, RepoNavError) as error:
                reason = str(error).replace('\r', ' ').replace('\n', ' ')
                bootstrap_line('FAILED', candidate['path'], reason[:177] + '...' if len(reason) > 180 else reason)
                counts['FAILED'] += 1
    counts['FAILED'] += len(issues) + len(problems)
    print('')
    if dry_run:
        print('Dry run complete.')
        print(f'Discovered:  {len(candidates)}')
        print(f'Would process: {counts["READY"]}')
    else:
        print('Bootstrap complete.')
        print(f'Discovered:  {len(candidates)}')
        print(f'Ready:       {counts["READY"]}')
        print(f'Warnings:    {counts["WARNING"]}')
    print(f'Skipped:     {counts["SKIPPED"]}')
    print(f'Failed:      {counts["FAILED"]}')
    print(f'Elapsed:     {time.monotonic() - started:.1f}s')
    if pruned:
        print(f'Traversal pruned: {len(pruned)} generated/heavy directories')
    if issues or problems:
        return 2
    if not dry_run and (counts['WARNING'] or counts['FAILED']):
        return 1
    return 0


# --- entry point -----------------------------------------------------------

def parse_languages(arguments):
    if not arguments:
        return None
    if len(arguments) == 2 and arguments[0] == '--languages':
        languages = [item.strip() for item in arguments[1].split(',') if item.strip()]
        unknown = [item for item in languages if item not in LANGUAGE_EXTENSIONS]
        if languages and not unknown:
            return languages
        if unknown:
            raise ValueError('Unknown language(s): ' + ', '.join(unknown) + '. Known: ' + ', '.join(sorted(LANGUAGE_EXTENSIONS)))
    raise ValueError(USAGE)


def main(argv):
    command, arguments = (argv[0], argv[1:]) if argv else ('ensure', [])
    try:
        assert_supported_dependencies()
        if command == 'bootstrap-all':
            return bootstrap_all(arguments)
        if command not in ('ensure', 'status', 'refresh') or (arguments and command != 'ensure'):
            print(USAGE)
            return 2
        try:
            languages = parse_languages(arguments)
        except ValueError as error:
            print(error)
            return 2
        root = repository_root()
        if command == 'refresh':
            guarded(root, 'Graphify graph refresh (graphify-state.py refresh)',
                    lambda: stage('Graphify graph refresh', root, lambda: graphify_refresh(root)))
            return 0
        if command == 'status':
            require_tools()
            write_status(root)
            return 0
        result = ensure_repository(root, languages)
        print(f'Repository: {root}')
        print(f'rg        OK  {result["tools"]["rg"]["version"]}')
        print(f'Serena project: {result["serena"]}')
        print(f'Serena Codex MCP: {format_serena_mcp_state(result["mcp"])}')
        print(f'Graphify  OK  {result["graph"]}; {result["hooks"]}')
        print('')
        if result['warnings']:
            for warning in result['warnings']:
                print(warning if warning.startswith('WARNING') else f'WARNING: {warning}')
            print('Repository navigation checks completed with warnings.')
        else:
            print('Repository navigation ready.')
        return 0
    except (OSError, ValueError, RepoNavError) as error:
        print(f'repo-nav error: {error}')
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
