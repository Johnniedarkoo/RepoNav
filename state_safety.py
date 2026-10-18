"""Read-only state classification and explicit fresh-home preparation; never pickle."""
import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import time


class StateSafetyError(ValueError):
    pass


def redirected(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def checked_chain(path, inspections):
    path = Path(os.path.abspath(path))
    if str(path).startswith('\\\\'):
        raise StateSafetyError('Network state roots are not supported by containment validation')
    for part in (*reversed(path.parents), path):
        inspections[0] += 1
        try:
            info = part.lstat()
        except FileNotFoundError:
            return path
        if redirected(info):
            raise StateSafetyError('Redirected path component rejected')
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise StateSafetyError('Non-directory path component')
    return path


def safe_yaml(path, empty_ok=False):
    # This is the already-installed Serena dependency; no Serena module or
    # deserialization loader is imported. Unknown executable YAML tags fail.
    from ruamel.yaml import YAML
    data = YAML(typ='safe', pure=True).load(path.read_text(encoding='utf-8-sig'))
    if data is None and empty_ok:
        return {}
    if not isinstance(data, dict):
        raise StateSafetyError('Configuration must be a mapping')
    return data


def index_preserves_config(data):
    # Static inspection of the installed schema avoids invoking Serena's config
    # loader, which can write migrations even before indexing starts.
    spec = importlib.util.find_spec('serena')
    if spec is None or not spec.submodule_search_locations:
        return False
    source = Path(next(iter(spec.submodule_search_locations))) / 'config/serena_config.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    def fields(name):
        node = classes.get(name)
        if node is None:
            return set()
        result = {item.target.id for item in node.body if isinstance(item, ast.AnnAssign)
                  and isinstance(item.target, ast.Name) and not item.target.id.startswith('_')}
        for base in node.bases:
            if isinstance(base, ast.Name):
                result.update(fields(base.id))
        return result
    required = fields('ProjectConfig') - {'project_name', 'language_servers'}
    return bool(required) and required.issubset(data) and 'language_servers' in data and not any(
        key in data for key in ('language', 'languages', 'additional_workspace_folders'))


def inspect_state(root, scope='all', operation='inspect'):
    started = time.perf_counter()
    inspections = [0]
    try:
        root = checked_chain(root, inspections)
        if not root.is_dir():
            raise StateSafetyError('Repository root is not a directory')
        inspections[0] += 1
        try:
            if redirected((root / '.git').lstat()):
                raise StateSafetyError('Redirected Git metadata marker rejected; ordinary gitdir files remain supported')
        except FileNotFoundError:
            pass
        result = {}
        scopes = ('serena', 'graphify') if scope == 'all' else (scope,)
        for kind in scopes:
            name = '.serena' if kind == 'serena' else 'graphify-out'
            directory = root / name
            entry = {'category': 'absent', 'files': 0, 'cache_files': 0,
                     'config': False, 'index_safe': False, 'serialized': False,
                     'graph': False, 'graph_valid': False}
            result[kind] = entry
            inspections[0] += 1
            try:
                info = directory.lstat()
            except FileNotFoundError:
                info = None
            if info is not None:
                if redirected(info):
                    raise StateSafetyError(name + ': unsafe redirected state')
                if not stat.S_ISDIR(info.st_mode):
                    raise StateSafetyError(name + ': expected a state directory')
                entry['category'] = 'ordinary'
                pending = [directory]
                while pending:
                    with os.scandir(pending.pop()) as children:
                        for child in children:
                            inspections[0] += 1
                            # Windows DirEntry.stat may report st_nlink=0; lstat is required.
                            child_info = Path(child.path).lstat()
                            if redirected(child_info):
                                raise StateSafetyError(json.dumps(name + '/' + Path(child.path).relative_to(directory).as_posix(), ensure_ascii=True) + ': redirected state component')
                            if stat.S_ISDIR(child_info.st_mode):
                                pending.append(Path(child.path))
                            elif stat.S_ISREG(child_info.st_mode):
                                if child_info.st_nlink > 1:
                                    raise StateSafetyError(json.dumps(name + '/' + Path(child.path).relative_to(directory).as_posix(), ensure_ascii=True) + ': multiply linked state file')
                                entry['files'] += 1
                                relative = Path(child.path).relative_to(directory)
                                if relative.parts[0].casefold() == 'cache':
                                    entry['cache_files'] += 1
                                if kind == 'serena' and Path(child.name).suffix.casefold() in ('.pkl', '.pickle'):
                                    entry['serialized'] = True
                            else:
                                raise StateSafetyError(name + ': unsupported state file type')
                for child_name in (('project.yml', 'project.local.yml', 'cache', 'logs', 'memories') if kind == 'serena' else
                                   ('graph.json', 'manifest.json', '.graphify_root', 'cache')):
                    child = directory / child_name
                    if child.exists() and (child_name in ('cache', 'logs', 'memories')) != child.is_dir():
                        raise StateSafetyError(name + ': malformed expected state component')
                if kind == 'serena':
                    config = directory / 'project.yml'
                    entry['config'] = config.is_file()
                    if entry['serialized']:
                        entry['category'] = 'unverified-executable-cache'
                    elif entry['config']:
                        data = safe_yaml(config)
                        entry['index_safe'] = index_preserves_config(data)
                        local = directory / 'project.local.yml'
                        if local.is_file():
                            # Serena writes this override file as a comments-only template.
                            safe_yaml(local, empty_ok=True)
                    elif entry['files']:
                        entry['category'] = 'unclear-ownership'
                else:
                    graph = directory / 'graph.json'
                    entry['graph'] = graph.is_file()
                    if entry['graph']:
                        try:
                            data = json.loads(graph.read_text(encoding='utf-8-sig'))
                            entry['graph_valid'] = (isinstance(data, dict) and isinstance(data.get('nodes'), list)
                                and isinstance(data.get('links', data.get('edges')), list))
                        except (ValueError, UnicodeError):
                            pass
                    elif entry['files']:
                        entry['category'] = 'unclear-ownership'
            if kind == 'serena' and operation != 'inspect':
                home = Path(os.environ.get('SERENA_HOME') or Path.home() / '.serena')
                global_config = checked_chain(home / 'serena_config.yml', inspections)
                if global_config.exists():
                    if not global_config.is_file():
                        raise StateSafetyError('Serena global configuration is not an ordinary file')
                    config_data = safe_yaml(global_config)
                    if config_data.get('project_serena_folder_location', '$projectDir/.serena') != '$projectDir/.serena':
                        raise StateSafetyError('Custom Serena data location is not supported by this containment boundary; preserve it and use a separately reviewed configuration')
            if operation != 'inspect' and entry['category'] == 'unclear-ownership':
                raise StateSafetyError(name + ': existing contents have unclear ownership; preserve and review them before initialization')
        result['metrics'] = {'inspections': inspections[0], 'hashes': 0, 'full_repository_scans': 0,
                             'seconds': time.perf_counter() - started}
        return result
    except StateSafetyError as error:
        error.inspections = inspections[0]
        error.seconds = time.perf_counter() - started
        raise
    except Exception as error:
        raise StateSafetyError('Malformed or inaccessible state: ' + type(error).__name__) from None



def prepare_serena_home(root, destination):
    inspect_state(root, 'serena', 'ensure')
    inspections = [0]
    destination = checked_chain(destination, inspections)
    if not destination.is_dir() or any(destination.iterdir()):
        raise StateSafetyError('Fresh empty temporary Serena home required')
    original = checked_chain(Path(os.environ.get('SERENA_HOME') or Path.home() / '.serena') / 'serena_config.yml', inspections)
    if original.is_file():
        data = safe_yaml(original)
        # Do not load/migrate configurations of unrelated registered projects.
        # The original owner configuration remains authoritative for all other
        # settings; only the disposable registration list is isolated.
        data['projects'] = []
        from ruamel.yaml import YAML
        buffer = io.StringIO()
        YAML(typ='safe', pure=True).dump(data, buffer)
        with (destination / 'serena_config.yml').open('x', encoding='utf-8') as stream:
            stream.write(buffer.getvalue())
    return {'prepared': True}


if __name__ == '__main__':
    try:
        result = prepare_serena_home(sys.argv[2], sys.argv[3]) if sys.argv[1] == 'prepare-home' else inspect_state(*sys.argv[1:])
        print(json.dumps(result, ensure_ascii=True))
    except Exception as error:
        # Do not echo personal target paths or third-party parser diagnostics.
        message = str(error) if isinstance(error, StateSafetyError) else type(error).__name__
        print('STATE SAFETY: ' + message, file=sys.stderr)
        sys.exit(1)

