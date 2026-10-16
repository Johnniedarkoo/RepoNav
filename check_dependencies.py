"""Read-only checks for the single supported uv tool configuration. Never installs."""
import ast
import importlib.metadata as metadata
import os
from pathlib import Path
import sys
import tomllib


def check_version(actual, expected, name):
    if actual != expected:
        raise ValueError(f'{name}: supported version is {expected}; found {actual}. See INSTALL.md; no upgrade attempted.')


def check_launcher(prefix, name, executable):
    receipt = tomllib.loads((prefix / 'uv-receipt.toml').read_text(encoding='utf-8'))
    paths = [item['install-path'] for item in receipt['tool']['entrypoints'] if item['name'] == name]
    if len(paths) != 1 or os.path.normcase(os.path.abspath(executable)) != os.path.normcase(os.path.abspath(paths[0])):
        raise ValueError(f'{name}: PATH launcher differs from selected uv tool environment. Fix PATH or UV_TOOL_DIR; see INSTALL.md.')


def check_symbols(source, required):
    tree = ast.parse(source)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name, arguments in required.items():
        if name not in functions:
            raise ValueError(f'Unsupported tool interface: missing {name}')
        args = functions[name].args
        names = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
        if not set(arguments).issubset(names):
            raise ValueError(f'Unsupported tool interface: {name} arguments differ')


def main():
    distribution, expected, launcher, executable = sys.argv[1:]
    check_version(metadata.version(distribution), expected, distribution)
    prefix = Path(sys.prefix)
    check_launcher(prefix, launcher, executable)
    site = prefix / 'Lib/site-packages'
    if distribution == 'graphifyy':
        check_symbols((site / 'graphify/detect.py').read_text(encoding='utf-8'),
                      {'detect_incremental': ['manifest_path', 'cache_root'], 'classify_file': []})
        check_symbols((site / 'graphify/watch.py').read_text(encoding='utf-8'),
                      {'_rebuild_code': ['changed_paths'], '_read_build_excludes': [], '_read_build_gitignore': []})
    else:
        tree = ast.parse((site / 'serena/cli.py').read_text(encoding='utf-8-sig'))
        project = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ProjectCommands')
        check_symbols(ast.unparse(project), {'create': ['project_path', 'language', 'index'], 'index': ['project']})
        create = next(n for n in project.body if isinstance(n, ast.FunctionDef) and n.name == 'create')
        literals = {n.value for d in create.decorator_list for n in ast.walk(d) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        if not {'--ls', '--index'}.issubset(literals):
            raise ValueError('Unsupported Serena create CLI options')
    print(f'{distribution} {expected}: uv environment and required interfaces verified')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Dependency check failed: {error}', file=sys.stderr)
        sys.exit(1)
