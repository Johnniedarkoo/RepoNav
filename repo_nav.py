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
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

SCRIPT_ROOT = Path(__file__).resolve().parent
USAGE = '\n'.join((
    'Usage: repo-nav ensure [--languages a,b] | status | refresh [--quiet] [--background]',
    '       repo-nav setup [--languages a,b]          ensure + hooks install + agent install',
    '       repo-nav hooks install | uninstall | status',
    '       repo-nav agent install | uninstall | status | context',
    '       repo-nav skill install | uninstall | status      user-wide skill for Claude, Codex, pi, OpenClaw',
    '       repo-nav watch [--debounce SECONDS]',
    '       repo-nav upgrade [--dry-run] | upgrade --self [--dry-run]',
    '       repo-nav bootstrap-all [--dry-run] <root> [<root2> ...]'))
BOOTSTRAP_USAGE = 'Usage: repo-nav bootstrap-all [--dry-run] <root> [<root2> ...]'
STATE_ENTRIES = ('.serena/', 'graphify-out/')
LAUNCHERS = (('serena-agent', 'serena'), ('graphifyy', 'graphify'))
HOOK_NAMES = ('post-commit', 'post-merge', 'post-checkout', 'post-rewrite')
HOOK_BEGIN = '# >>> repo-nav >>>'
HOOK_END = '# <<< repo-nav <<<'
SHELL_INTERPRETERS = {'sh', 'bash', 'dash', 'zsh', 'ksh'}
AGENT_SETTINGS = '.claude/settings.local.json'
AGENT_COMMAND = re.compile(r"repo-nav'? agent context$")
# Variables a Git hook may export that would point a detached refresh at a
# temporary index or another repository.
GIT_REDIRECTS = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
                 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_PREFIX', 'GIT_REFLOG_ACTION')
LOG_LIMIT = 256 * 1024

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


def supported_versions():
    return json.loads((SCRIPT_ROOT / 'supported-versions.json').read_text(encoding='utf-8'))


def check_dependency(distribution, launcher, versions):
    return run([tool_python(distribution), '-B', SCRIPT_ROOT / 'check_dependencies.py',
                distribution, '|'.join(versions), launcher, shutil.which(launcher) or launcher])


def assert_supported_dependencies():
    for name in ('git', 'uv', 'rg', 'serena', 'graphify'):
        if shutil.which(name) is None:
            raise RepoNavError(f'Missing prerequisite: {name}. See INSTALL.md. No software was installed.')
    tested = supported_versions()['tested']
    for distribution, launcher in LAUNCHERS:
        code, output = check_dependency(distribution, launcher, sorted({pair[distribution] for pair in tested}))
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
        remove_serena_temporary(base, token)


def remove_serena_temporary(base, token):
    temporary = base / f'reponav-serena-{token}'
    if stat.S_ISLNK(temporary.lstat().st_mode) or temporary.resolve() != temporary:
        raise RepoNavError('Unsafe Serena temporary cleanup target')
    marker = temporary / '.owner'
    if stat.S_ISLNK(marker.lstat().st_mode) or marker.read_text(encoding='utf-8').strip() != token:
        raise RepoNavError('Unsafe Serena temporary ownership marker')
    # Language-server installs leave npm's node_modules/.bin symlinks here.
    # Where rmtree is symlink-attack safe it unlinks links without following
    # them; elsewhere keep refusing, as repo-nav.ps1 does for reparse points.
    if not shutil.rmtree.avoids_symlink_attacks:
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


def graphify_refresh(root, quiet=False):
    state_safety(root, 'graphify', 'refresh')
    code, output = run([tool_python('graphifyy'), SCRIPT_ROOT / 'graphify-state.py', 'refresh', root],
                       cwd=root, env=in_repository_env())
    for line in (output[-1:] if quiet and code == 0 else output):
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
    if hooks_state(root)['installed']:
        return 'auto-refresh hooks installed'
    if graphify_hook_status(root) and graphify_attribute_state(root)['graphify_line']:
        return 'hooks installed'
    return ('not installed — run repo-nav hooks install for Git-local auto-refresh '
            '(Graphify hook install also writes .gitattributes)')


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
    hook_text = ('auto-refresh hooks installed' if hooks_state(root)['installed'] else 'hooks installed' if hooks
                 else 'not installed — .gitattributes conflict' if graphify_attribute_state(root)['other_content']
                 else 'not installed')
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
    print(f'Claude Code session context: {"installed" if agent_settings_state(root)["installed"] else "not installed"}')


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


# --- auto-refresh: locked and background refresh ---------------------------

def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_refresh_lock(root):
    """Per-worktree lock; a refresh requested while one runs queues one more pass."""
    lock = git_path(root, 'repo-nav-refresh.lock')
    for _ in range(2):
        try:
            lock.mkdir()
            (lock / 'pid').write_text(str(os.getpid()), encoding='utf-8')
            return lock
        except FileExistsError:
            try:
                pid = int((lock / 'pid').read_text(encoding='utf-8').strip())
            except (OSError, ValueError):
                pid = None
            if pid is not None and process_alive(pid):
                return None
            # Owner exited without cleanup: take the lock over.
            for child in lock.iterdir():
                child.unlink()
            lock.rmdir()
    return None


def release_refresh_lock(lock):
    for child in lock.iterdir():
        child.unlink()
    lock.rmdir()


def locked_refresh(root, quiet=False):
    rerun = git_path(root, 'repo-nav-refresh.rerun')
    lock = acquire_refresh_lock(root)
    if lock is None:
        rerun.touch()
        print('refresh already running; one more pass queued')
        return
    try:
        while True:
            if rerun.exists():
                rerun.unlink()
            guarded(root, 'Graphify graph refresh (graphify-state.py refresh)',
                    lambda: stage('Graphify graph refresh', root, lambda: graphify_refresh(root, quiet)))
            if not rerun.exists():
                break
    finally:
        release_refresh_lock(lock)


def start_background_refresh(root):
    """Detach a quiet refresh so Git hooks and agent start-up never wait for it."""
    for name in GIT_REDIRECTS:
        os.environ.pop(name, None)
    log = git_path(root, 'repo-nav.log')
    try:
        if log.stat().st_size > LOG_LIMIT:
            log.write_bytes(log.read_bytes()[-LOG_LIMIT // 4:])
    except FileNotFoundError:
        pass
    with open(log, 'ab') as stream:
        stream.write(f'--- {time.strftime("%Y-%m-%d %H:%M:%S")} refresh\n'.encode('utf-8'))
        stream.flush()
        subprocess.Popen([sys.executable, '-B', str(SCRIPT_ROOT / 'repo_nav.py'), 'refresh', '--quiet'],
                         cwd=root, env=dict(os.environ), stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                         start_new_session=True)
    return log


# --- auto-refresh: Git-local hooks -----------------------------------------

def hooks_directory(root):
    directory = git_path(root, 'hooks')
    common = git_path(root, '.')
    resolved = Path(os.path.realpath(directory))
    inside_worktree = Path(os.path.realpath(root)) in (resolved, *resolved.parents)
    inside_git = Path(os.path.realpath(common)) in (resolved, *resolved.parents)
    if inside_worktree and not inside_git:
        raise RepoNavError(f'core.hooksPath points inside the work tree ({directory}); refusing to write hooks that '
                           'could be tracked. Add "repo-nav refresh --background --quiet" to your hook manager instead.')
    return directory


def hook_block(name):
    command = f'{shlex.quote(str(SCRIPT_ROOT / "repo-nav"))} refresh --background --quiet >/dev/null 2>&1'
    condition = '[ -f "$(git rev-parse --show-toplevel 2>/dev/null)/graphify-out/graph.json" ]'
    if name == 'post-checkout':
        condition = '[ "${3:-0}" = 1 ] && ' + condition  # branch switches only, not file checkouts
    return '\n'.join((HOOK_BEGIN, '# Git-local auto-refresh of the RepoNav code graph; remove with: repo-nav hooks uninstall',
                      f'{condition} && {command} || true', HOOK_END)) + '\n'


def shell_hook(text):
    first = text.split('\n', 1)[0]
    if not first.startswith('#!'):
        return False
    words = first[2:].split()
    if words and os.path.basename(words[0]) == 'env':
        words = [word for word in words[1:] if not word.startswith('-')]
    return bool(words) and os.path.basename(words[0]) in SHELL_INTERPRETERS


def without_block(text):
    pattern = re.compile(re.escape(HOOK_BEGIN) + r'\n.*?' + re.escape(HOOK_END) + r'\n', re.S)
    return pattern.sub('', text)


def hooks_state(root):
    try:
        directory = hooks_directory(root)
    except RepoNavError as error:
        return {'installed': False, 'hooks': {}, 'error': str(error)}
    hooks = {}
    for name in HOOK_NAMES:
        path = directory / name
        try:
            text = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            hooks[name] = 'missing'
            continue
        except (OSError, UnicodeError):
            hooks[name] = 'unreadable'
            continue
        hooks[name] = ('installed' if HOOK_BEGIN in text and os.access(path, os.X_OK) else
                       'missing' if shell_hook(text) else 'foreign hook (not a shell script)')
    return {'installed': all(state == 'installed' for state in hooks.values()), 'hooks': hooks, 'directory': directory}


def hooks_install(root):
    directory = hooks_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    lines = []
    for name in HOOK_NAMES:
        path = directory / name
        assert_ordinary_write_target(path)
        block = hook_block(name)
        if path.exists():
            text = path.read_text(encoding='utf-8')
            if not shell_hook(text):
                lines.append(f'{name}: skipped — existing hook is not a shell script; add '
                             '"repo-nav refresh --background --quiet" to it yourself')
                continue
            updated = without_block(text)
            shebang, _, rest = updated.partition('\n')
            # Directly after the shebang, so an "exit 0" in the existing hook
            # cannot skip it; the block itself never fails or blocks.
            updated = shebang + '\n' + block + rest
            if updated == text:
                lines.append(f'{name}: already installed')
                continue
            path.write_text(updated, encoding='utf-8')
            lines.append(f'{name}: added to existing hook')
        else:
            path.write_text('#!/bin/sh\n' + block, encoding='utf-8')
            lines.append(f'{name}: installed')
        path.chmod(path.stat().st_mode | 0o111)
    return lines


def hooks_uninstall(root):
    directory = hooks_directory(root)
    lines = []
    for name in HOOK_NAMES:
        path = directory / name
        try:
            text = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            continue
        if HOOK_BEGIN not in text:
            continue
        assert_ordinary_write_target(path)
        updated = without_block(text)
        if updated.strip() == '#!/bin/sh':
            path.unlink()
            lines.append(f'{name}: removed')
        else:
            path.write_text(updated, encoding='utf-8')
            lines.append(f'{name}: repo-nav block removed; existing hook kept')
    return lines or ['no repo-nav hooks found']


def hooks_command(root, arguments):
    if arguments == ['install']:
        lines = hooks_install(root)
    elif arguments == ['uninstall']:
        lines = hooks_uninstall(root)
    elif arguments == ['status']:
        state = hooks_state(root)
        if 'error' in state:
            raise RepoNavError(state['error'])
        print(f'Hooks directory: {state["directory"]}')
        lines = [f'{name}: {status}' for name, status in state['hooks'].items()]
    else:
        print(USAGE)
        return 2
    for line in lines:
        print(line)
    return 0


# --- agent wiring (Claude Code) --------------------------------------------

def agent_settings_state(root):
    path = Path(root) / AGENT_SETTINGS
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'path': path, 'data': {}, 'installed': False}
    if not isinstance(data, dict):
        raise RepoNavError(f'{path}: expected a JSON object')
    installed = any(isinstance(hook, dict) and AGENT_COMMAND.search(str(hook.get('command', '')))
                    for entry in (data.get('hooks') or {}).get('SessionStart') or []
                    for hook in (entry.get('hooks') or [] if isinstance(entry, dict) else []))
    return {'path': path, 'data': data, 'installed': installed}


def write_json(path, data):
    assert_ordinary_write_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.repo-nav-{secrets.token_hex(4)}')
    temporary.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def claude_serena(root):
    claude = shutil.which('claude')
    if claude is None:
        return None, 'Claude Code CLI unavailable'
    code, output = run([claude, 'mcp', 'get', 'serena'], cwd=root)
    text = '\n'.join(output)
    if code != 0 or 'No MCP server named' in text:
        return claude, 'missing'
    return claude, text


def agent_install(root):
    if is_tracked(root, AGENT_SETTINGS):
        raise RepoNavError(f'{AGENT_SETTINGS} is tracked; refusing to edit a tracked agent policy file.')
    lines = []
    state = agent_settings_state(root)
    if state['installed']:
        lines.append(f'Session context: already installed ({state["path"]})')
    else:
        data = state['data']
        entries = data.setdefault('hooks', {}).setdefault('SessionStart', [])
        entries.append({'hooks': [{'type': 'command', 'command': f'{shlex.quote(str(SCRIPT_ROOT / "repo-nav"))} agent context',
                                   'timeout': 30}]})
        write_json(state['path'], data)
        lines.append(f'Session context: SessionStart hook added to {state["path"]}')
    code, _ = run(['git', 'check-ignore', '-q', AGENT_SETTINGS], cwd=root)
    if code != 0:
        lines.append('Session context: ' + ensure_local_exclude(root, git_path(root, 'info/exclude'), AGENT_SETTINGS))
    claude, serena = claude_serena(root)
    if claude is None:
        lines.append(f'Serena MCP: skipped — {serena}')
    elif serena != 'missing':
        lines.append('Serena MCP: already configured; left unchanged')
    else:
        code, output = run([claude, 'mcp', 'add', '--scope', 'local', 'serena', '--', shutil.which('serena') or 'serena',
                            'start-mcp-server', '--context', 'claude-code', '--project', root,
                            '--open-web-dashboard', 'False'], cwd=root)
        if code != 0:
            raise RepoNavError('Claude Code MCP registration failed:\n' + '\n'.join(output))
        lines.append('Serena MCP: registered for this repository (Claude Code local scope)')
    return lines


def agent_uninstall(root):
    lines = []
    state = agent_settings_state(root)
    if state['installed']:
        data = state['data']
        hooks = data['hooks']
        kept = []
        for entry in hooks['SessionStart']:
            remaining = [hook for hook in entry.get('hooks') or [] if not AGENT_COMMAND.search(str(hook.get('command', '')))]
            if remaining:
                kept.append(dict(entry, hooks=remaining))
        if kept:
            hooks['SessionStart'] = kept
        else:
            del hooks['SessionStart']
            if not hooks:
                del data['hooks']
        if data:
            write_json(state['path'], data)
        else:
            state['path'].unlink()
        lines.append(f'Session context: removed from {state["path"]}')
    claude, serena = claude_serena(root)
    # Only remove the registration RepoNav itself made.
    if claude is not None and serena != 'missing' and '--context claude-code' in serena and f'--project {root}' in serena:
        code, output = run([claude, 'mcp', 'remove', 'serena', '--scope', 'local'], cwd=root)
        lines.append('Serena MCP: removed' if code == 0 else 'Serena MCP: removal failed: ' + ' '.join(output))
    return lines or ['nothing to remove']


def agent_context(root):
    """One line of context for a new agent session; returns at once.

    Checking freshness means loading the whole graph (seconds on large
    repositories), so the line only says the graph exists and a detached
    refresh brings it up to date; that refresh is a no-op when nothing changed.
    """
    graph = Path(root) / 'graphify-out' / 'graph.json'
    if not graph.is_file():
        print('repo-nav: no code graph in this repository yet; `repo-nav setup` builds one (ask the user first).')
        return 0
    try:
        start_background_refresh(root)
        freshness = 'it refreshes itself in the background when code changed'
    except (OSError, RepoNavError):
        freshness = 'run `repo-nav status` to check it is current'
    print(f'repo-nav: this repository has a local code graph ({freshness}). For code questions, try '
          '`graphify query "<question>"`, `graphify explain <Symbol>` or `graphify path <A> <B>` before grepping, '
          'then open only the files they point to. If the Serena MCP server is connected, use find_symbol and '
          'find_referencing_symbols for exact definitions and references. Both are local and cost no tokens.')
    return 0


def agent_command(root, arguments):
    if arguments == ['context']:
        return agent_context(root)
    if arguments == ['install']:
        lines = agent_install(root)
    elif arguments == ['uninstall']:
        lines = agent_uninstall(root)
    elif arguments == ['status']:
        state = agent_settings_state(root)
        claude, serena = claude_serena(root)
        lines = [f'Session context: {"installed" if state["installed"] else "not installed"} ({state["path"]})',
                 'Serena MCP: ' + (serena if claude is None or serena == 'missing' else 'configured')]
    else:
        print(USAGE)
        return 2
    for line in lines:
        print(line)
    return 0


# --- agent skill (all harnesses) ------------------------------------------
# One SKILL.md, installed per user rather than per repository, so no tracked
# policy file is touched. ~/.agents/skills is the Agent Skills location (read
# by pi and other spec-compliant harnesses); Claude Code and Codex read their
# own folders; OpenClaw's folder is used only when OpenClaw is installed.

SKILL_SOURCE = SCRIPT_ROOT / 'repo-nav-skill.md'
SKILL_MARKER = '<!-- Managed by repo-nav skill install'


def skill_targets():
    home = Path.home()
    targets = [('Agent Skills (pi, OpenCode, ...)', home / '.agents' / 'skills'),
               ('Claude Code', Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude') / 'skills'),
               ('Codex', Path(os.environ.get('CODEX_HOME') or home / '.codex') / 'skills')]
    if (home / '.openclaw').is_dir():
        targets.append(('OpenClaw', home / '.openclaw' / 'skills'))
    return [(name, folder / 'repo-nav' / 'SKILL.md') for name, folder in targets]


def skill_state(path, source):
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return 'not installed'
    if SKILL_MARKER not in text:
        return 'foreign skill named repo-nav'
    return 'installed' if text == source else 'outdated'


def skill_command(arguments):
    source = SKILL_SOURCE.read_text(encoding='utf-8')
    if arguments not in (['install'], ['uninstall'], ['status']):
        print(USAGE)
        return 2
    for name, path in skill_targets():
        state = skill_state(path, source)
        if arguments == ['status']:
            print(f'{name}: {state} ({path})')
        elif state == 'foreign skill named repo-nav':
            print(f'{name}: skipped — {path} exists and is not managed by RepoNav')
        elif arguments == ['install']:
            if state == 'installed':
                print(f'{name}: already installed ({path})')
                continue
            assert_ordinary_write_target(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding='utf-8')
            print(f'{name}: {"updated" if state == "outdated" else "installed"} ({path})')
        elif state != 'not installed':
            assert_ordinary_write_target(path)
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                pass
            print(f'{name}: removed ({path})')
    return 0


# --- watch -----------------------------------------------------------------

WATCH_EXCLUDED = re.compile(r'(^|/)(\.git|graphify-out|\.serena|node_modules)(/|$)')


def watch_source(root):
    if shutil.which('fswatch'):
        return 'fswatch', ['fswatch', '-r', '--latency', '0.5', '-e', r'/\.git/', '-e', '/graphify-out/',
                           '-e', r'/\.serena/', '-e', '/node_modules/', root]
    if shutil.which('inotifywait'):
        return 'inotifywait', ['inotifywait', '-m', '-r', '-q', '-e', 'close_write,create,delete,move',
                               '--format', '%w%f', '--exclude', r'/(\.git|graphify-out|\.serena|node_modules)(/|$)', root]
    return 'polling', None


def worktree_signature(root):
    code, output = run(['git', 'status', '--porcelain=v1', '--untracked-files=normal'], cwd=root)
    head = run(['git', 'rev-parse', 'HEAD'], cwd=root)[1]
    stamps = []
    for line in output:
        path = Path(root) / line[3:].split(' -> ')[-1].strip('"')
        try:
            info = path.stat()
            stamps.append((line, info.st_mtime_ns, info.st_size))
        except OSError:
            stamps.append((line, 0, 0))
    return code, head, stamps


def watch_refresh(root):
    state = graphify_state(root)
    if not state['state'].startswith('stale'):
        return
    print(f'{time.strftime("%H:%M:%S")} {state["state"]}; refreshing')
    try:
        locked_refresh(root, quiet=True)
    except RepoNavError as error:
        print(f'{time.strftime("%H:%M:%S")} refresh failed: {error}')


def stop_on_terminate(signum, frame):
    raise KeyboardInterrupt


def watch(root, debounce):
    # Line-buffered so a log file follows along; SIGTERM stops it like Ctrl-C
    # (background jobs ignore SIGINT).
    sys.stdout.reconfigure(line_buffering=True)
    signal.signal(signal.SIGTERM, stop_on_terminate)
    name, command = watch_source(root)
    print(f'Watching {root} with {name}; refreshing after {debounce:g}s without changes. Ctrl-C stops.')
    watch_refresh(root)
    process = None
    try:
        if command is None:
            previous, changed_at = worktree_signature(root), None
            while True:
                time.sleep(min(debounce, 2.0))
                current = worktree_signature(root)
                if current != previous:
                    previous, changed_at = current, time.monotonic()
                elif changed_at is not None and time.monotonic() - changed_at >= debounce:
                    changed_at = None
                    watch_refresh(root)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        pending_since = None
        while True:
            ready = selector.select(debounce if pending_since is not None else None)
            if ready:
                line = process.stdout.readline()
                if not line:
                    raise RepoNavError(f'{name} stopped unexpectedly (exit {process.wait()}).')
                relative = os.path.relpath(line.strip(), root)
                if not WATCH_EXCLUDED.search(relative.replace(os.sep, '/')):
                    pending_since = time.monotonic()
            elif pending_since is not None and time.monotonic() - pending_since >= debounce:
                pending_since = None
                watch_refresh(root)
    except KeyboardInterrupt:
        print('Stopped watching.')
        return 0
    finally:
        if process is not None:
            process.terminate()


# --- upgrade ---------------------------------------------------------------

def installed_version(distribution):
    try:
        python = tool_python(distribution)
    except RepoNavError:
        return None
    code, output = run([python, '-c', 'import importlib.metadata as m, sys; print(m.version(sys.argv[1]))', distribution])
    return output[-1].strip() if code == 0 and output else None


def install_tool(distribution, version, python):
    code, output = run(['uv', 'tool', 'install', '--force', '--python', python, f'{distribution}=={version}'])
    if code != 0:
        raise RepoNavError(f'uv could not install {distribution} {version}:\n' + '\n'.join(output[-6:]))


def upgrade_tools(dry_run):
    if shutil.which('uv') is None:
        raise RepoNavError('Missing prerequisite: uv. See INSTALL.md. No software was installed.')
    supported = supported_versions()
    target = supported['tested'][-1]
    current = {distribution: installed_version(distribution) for distribution, _ in LAUNCHERS}
    changes = [(distribution, current[distribution], target[distribution]) for distribution, _ in LAUNCHERS
               if current[distribution] != target[distribution]]
    if not changes:
        print('Serena {} and Graphify {} are the newest tested pair; nothing to do.'.format(
            target['serena-agent'], target['graphifyy']))
        return 0
    for distribution, old, new in changes:
        print(f'{distribution}: {old or "not installed"} -> {new}')
    if dry_run:
        print('Dry run; nothing installed.')
        return 0
    done = []
    try:
        for distribution, old, new in changes:
            install_tool(distribution, new, supported['python'][distribution])
            done.append((distribution, old))
        for distribution, launcher in LAUNCHERS:
            code, output = check_dependency(distribution, launcher, [target[distribution]])
            if code != 0:
                raise RepoNavError('\n'.join(output))
    except RepoNavError as error:
        for distribution, old in reversed(done):
            if old is not None:
                install_tool(distribution, old, supported['python'][distribution])
        raise RepoNavError(f'Upgrade failed; previous versions restored.\n{error}') from None
    print('Upgrade complete; dependency and interface checks passed.')
    return 0


def upgrade_self(dry_run):
    def git(*arguments):
        return run(['git', '-C', SCRIPT_ROOT, *arguments])
    code, output = git('rev-parse', '--is-inside-work-tree')
    if code != 0 or output[:1] != ['true']:
        raise RepoNavError(f'{SCRIPT_ROOT} is not a Git checkout; update it the way you installed it.')
    code, output = git('status', '--porcelain', '--untracked-files=no')
    if code != 0 or output:
        raise RepoNavError(f'{SCRIPT_ROOT} has local changes; commit or remove them before updating.')
    code, output = git('fetch', '--quiet')
    if code != 0:
        raise RepoNavError('git fetch failed:\n' + '\n'.join(output))
    code, output = git('log', '--oneline', 'HEAD..@{u}')
    if code != 0:
        raise RepoNavError('The RepoNav checkout has no upstream branch to update from.')
    if not output:
        print('RepoNav is up to date.')
        return 0
    print('Incoming RepoNav changes:')
    for line in output:
        print(f'  {line}')
    if dry_run:
        print('Dry run; checkout unchanged.')
        return 0
    code, output = git('merge', '--ff-only', '--quiet', '@{u}')
    if code != 0:
        raise RepoNavError('Fast-forward update failed; checkout unchanged.\n' + '\n'.join(output))
    print('RepoNav updated (fast-forward only). Review the changes above before your next run.')
    return 0


def upgrade(arguments):
    options = set(arguments)
    if not options <= {'--dry-run', '--self'} or len(options) != len(arguments):
        print(USAGE)
        return 2
    if '--self' in options:
        return upgrade_self('--dry-run' in options)
    return upgrade_tools('--dry-run' in options)


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


def parse_options(arguments, allowed):
    options = set(arguments)
    if not options <= allowed or len(options) != len(arguments):
        raise ValueError(USAGE)
    return options


def parse_debounce(arguments):
    if not arguments:
        return 3.0
    if len(arguments) == 2 and arguments[0] == '--debounce':
        try:
            value = float(arguments[1])
        except ValueError:
            value = 0
        if 0.5 <= value <= 600:
            return value
    raise ValueError(USAGE)


def print_ensure(root, result):
    print(f'Repository: {root}')
    print(f'rg        OK  {result["tools"]["rg"]["version"]}')
    print(f'Serena project: {result["serena"]}')
    print(f'Serena Codex MCP: {format_serena_mcp_state(result["mcp"])}')
    print(f'Graphify  OK  {result["graph"]}; {result["hooks"]}')


def main(argv):
    command, arguments = (argv[0], argv[1:]) if argv else ('ensure', [])
    try:
        if command == 'upgrade':
            return upgrade(arguments)
        if command == 'skill':
            return skill_command(arguments)
        if command == 'agent' and arguments == ['context']:
            # Runs at every agent session start: no dependency checks here;
            # the detached refresh performs them.
            return agent_context(repository_root())
        if command == 'refresh' and '--background' in arguments:
            # Called from Git hooks: return at once and leave the dependency
            # checks and the refresh itself to the detached process.
            parse_options(arguments, {'--background', '--quiet'})
            for name in GIT_REDIRECTS:
                os.environ.pop(name, None)  # before resolving the root, too
            log = start_background_refresh(repository_root())
            if '--quiet' not in arguments:
                print(f'Refresh started in the background; log: {log}')
            return 0
        assert_supported_dependencies()
        if command == 'bootstrap-all':
            return bootstrap_all(arguments)
        if command not in ('ensure', 'setup', 'status', 'refresh', 'hooks', 'agent', 'watch'):
            print(USAGE)
            return 2
        try:
            languages = parse_languages(arguments) if command in ('ensure', 'setup') else None
            debounce = parse_debounce(arguments) if command == 'watch' else None
            if command == 'refresh':
                options = parse_options(arguments, {'--quiet'})
            elif command == 'status' and arguments:
                raise ValueError(USAGE)
        except ValueError as error:
            print(error)
            return 2
        root = repository_root()
        if command == 'refresh':
            locked_refresh(root, '--quiet' in options)
            return 0
        if command == 'hooks':
            return hooks_command(root, arguments)
        if command == 'agent':
            return agent_command(root, arguments)
        if command == 'watch':
            return watch(root, debounce)
        if command == 'status':
            require_tools()
            write_status(root)
            return 0
        # Hooks first: they stay inert until ensure has built the graph.
        setup = hooks_install(root) if command == 'setup' else []
        result = ensure_repository(root, languages)
        if command == 'setup':
            setup += agent_install(root)
        print_ensure(root, result)
        for line in setup:
            print(line)
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
