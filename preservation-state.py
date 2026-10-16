"""Read-only byte fingerprints for RepoNav's post-execution preservation guard."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

POLICY_NAMES = {'agents.md', 'agents.override.md', 'claude.md', 'claude.local.md', 'gemini.md'}
POLICY_DIRS = {'.claude', '.codex', '.cursor'}


def protected(name):
    parts = name.replace('\\', '/').casefold().split('/')
    return (parts[-1] in POLICY_NAMES or any(p in POLICY_DIRS for p in parts)
            or '/'.join(parts[-2:]) == '.github/copilot-instructions.md'
            or any(parts[i:i+2] == ['.github', 'instructions'] for i in range(len(parts)-1)))


def linked(info):
    return stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400


def snapshot(root):
    started = time.perf_counter()
    root = Path(root).absolute()
    for ancestor in (*reversed(root.parents), root):
        if linked(ancestor.lstat()):
            raise ValueError('Linked repository root')
    # No optional index refresh/locks, external diffs, filters or text conversion.
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '--stage', '-z'],
                            env=env, capture_output=True, check=True)
    records = {}
    tracked = set()
    for row in result.stdout.split(b'\0'):
        if not row:
            continue
        entry, path = row.split(b'\t', 1)
        name = os.fsdecode(path)
        tracked.add(name)
        records['I:' + name] = records.get('I:' + name, '') + entry.decode('ascii') + ';'
    hashes = size = 0
    directory_info = {}

    def fingerprint(name):
        nonlocal hashes, size
        path = root
        parts = Path(name).parts
        for position, part in enumerate(parts):
            path = path / part
            try:
                info = directory_info.get(path)
                if info is None:
                    info = path.lstat()
                    if position < len(parts) - 1:
                        directory_info[path] = info
            except FileNotFoundError:
                return 'missing'
            if linked(info):
                # Record link metadata/target without opening it or traversing it.
                try:
                    target = os.readlink(path)
                except OSError:
                    target = ''
                return 'link:' + repr((str(path.relative_to(root)), target, info.st_mode,
                                       info.st_size, info.st_mtime_ns))
        if not stat.S_ISREG(info.st_mode):
            return 'type:' + str(info.st_mode)
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
                size += len(block)
        hashes += 1
        return str(stat.S_IMODE(info.st_mode)) + ':' + digest.hexdigest()

    for name in tracked:
        records['W:' + name] = fingerprint(name)
    # Include ignored policy files, too. Never recurse through reparse points.
    pending = [root]
    policies = set()
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                item = Path(entry.path)
                name = item.relative_to(root).as_posix()
                if name == '.git':
                    continue
                info = entry.stat(follow_symlinks=False)
                if protected(name):
                    policies.add(name)
                if stat.S_ISDIR(info.st_mode) and not linked(info):
                    pending.append(item)
    for name in policies:
        # Tracked content is already covered, but retaining a policy entry detects
        # transitions between tracked and untracked state without false baselines.
        records['P:' + name] = records.get('W:' + name) or fingerprint(name)
    return {'records': records, 'metrics': {'tracked': len(tracked), 'hashes': hashes,
            'bytes': size, 'seconds': time.perf_counter() - started}}


if __name__ == '__main__':
    try:
        print(json.dumps(snapshot(sys.argv[1]), ensure_ascii=True))
    except Exception as error:
        print('Preservation snapshot failed: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
