"""Local source-only export. No Git access, network calls, or source writes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


class ValidationError(Exception):
    """Safe diagnostic: never include source content or secret values."""


FORBIDDEN = {'.git', '.serena', 'graphify-out', '.codex', '.claude',
             '.venv', 'venv', 'node_modules', '__pycache__'}
FORBIDDEN_FILES = {'workflow-pollution-audit.md', 'reponav_publication_audit.md',
                   'dependency_license_inventory.md'}
TEXT_SUFFIXES = {'.md', '.ps1', '.py', '.txt', '.json'}
# One-way deny fingerprints avoid distributing reversible private identifiers.
# These are privacy review markers, not credential hashes or a secrecy guarantee.
PRIVATE_FINGERPRINTS = ((7, '3297c2f67a5806237006a2b5ae3a176a078178b0e1f430cf7f73987ebd251557'), (5, '497992d56d56de5e2b1176f67916f7aa09976be1506e850fb24d4c991b23baf3'), (15, '773cf23d0c41d105fae68c0653aa22d0c5c1d973ff6dae946a544ce8aeec9930'))

def private_marker_present(text):
    folded = text.casefold()
    return any(hashlib.sha256(folded[i:i + size].encode()).hexdigest() == expected
               for size, expected in PRIVATE_FINGERPRINTS
               for i in range(max(0, len(folded) - size + 1)))


PATTERNS = (
    ('absolute machine path', re.compile(r'(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\[a-z0-9_.-]+[\\/])')),
    ('private key', re.compile(r'-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----')),
    ('credential token', re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{20,})')),
    ('credential assignment', re.compile(r'''(?ix)(?:password|api[_-]?key|access[_-]?token|secret[_-]?key)\s*[:=]\s*["'][^"'\r\n]{8,}["']''')),
    ('personal email review', re.compile(r'(?i)\b[A-Z0-9._%+-]+@(?![A-Z0-9.-]*example\.(?:invalid|com|org|net)\b)[A-Z0-9.-]+\.[A-Z]{2,}\b')),
)


def ordinary(path, directory=False):
    """lstat avoids following links, including Windows junction/reparse tags."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValidationError('Link/reparse point rejected')
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValidationError('Expected ordinary directory' if directory else 'Expected ordinary file')


def checked_chain(path, directory=False):
    for parent in reversed(path.parents):
        ordinary(parent, directory=True)
    ordinary(path, directory=directory)


def relative_name(name):
    if (not name or name != name.strip() or '\\' in name or ':' in name
            or name.startswith('/') or any(c in name for c in '*?<>|"')
            or any(ord(c) < 32 for c in name)):
        raise ValidationError('Unsafe manifest path')
    parts = name.split('/')
    for part in parts:
        if (part in ('', '.', '..') or part.endswith((' ', '.'))
                or part.casefold() in FORBIDDEN
                or part.casefold() in FORBIDDEN_FILES
                or re.fullmatch(r'(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)):
            raise ValidationError('Unsafe or excluded manifest path')
    if Path(name).suffix.lower() not in TEXT_SUFFIXES and name not in {'.gitignore', 'repo-nav.cmd', 'LICENSE'}:
        raise ValidationError('Non-source file type rejected')
    return name


def read_manifest(root, manifest='release-files.txt'):
    relative_name(manifest)
    path = root / manifest
    checked_chain(path)
    names = []
    seen = set()
    spellings = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if not line or line.startswith('#'):
            continue
        name = relative_name(line)
        if name.casefold() in seen:
            raise ValidationError('Duplicate/case-colliding manifest path')
        seen.add(name.casefold())
        for index in range(1, len(name.split('/')) + 1):
            prefix = '/'.join(name.split('/')[:index])
            previous = spellings.setdefault(prefix.casefold(), prefix)
            if previous != prefix:
                raise ValidationError('Case-colliding manifest directory')
        names.append(name)
    if not names:
        raise ValidationError('Empty manifest')
    if any(parent.as_posix().casefold() in seen for name in names
           for parent in Path(name).parents if str(parent) != '.'):
        raise ValidationError('Manifest file/directory collision')
    return sorted(names)


def scan(name, data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValidationError('Non-UTF-8 release content rejected') from None
    if '\x00' in text:
        raise ValidationError('Binary release content rejected')
    if private_marker_present(text) or private_marker_present(name):
        raise ValidationError('Privacy scan rejected file: private identifier')
    for label, pattern in PATTERNS:
        if pattern.search(text) or pattern.search(name):
            raise ValidationError('Privacy scan rejected file: ' + label)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verify(root, destination, names, hashes):
    checked_chain(destination, directory=True)
    actual = []
    expected_dirs = {str(p).replace('\\', '/') for n in names
                     for p in Path(n).parents if str(p) != '.'}
    for base, dirs, files in os.walk(destination, followlinks=False):
        for entry in dirs:
            path = Path(base) / entry
            ordinary(path, directory=True)
            if path.relative_to(destination).as_posix() not in expected_dirs:
                raise ValidationError('Unexpected export directory')
        for entry in files:
            path = Path(base) / entry
            ordinary(path)
            name = path.relative_to(destination).as_posix()
            actual.append(name)
            if name not in hashes:
                raise ValidationError('Unexpected export file')
            data = path.read_bytes()
            scan(name, data)
            if digest(data) != hashes[name]:
                raise ValidationError('Export hash mismatch')
    if sorted(actual) != names:
        raise ValidationError('Export file list mismatch')
    for name in names:
        checked_chain(root / name)
        if digest((root / name).read_bytes()) != hashes[name]:
            raise ValidationError('Source changed during export')


def export(root, destination):
    root = Path(os.path.abspath(root))
    destination = Path(os.path.abspath(destination))
    checked_chain(root, directory=True)
    checked_chain(destination.parent, directory=True)
    # Neither tree may contain the other; existing destinations are never reused.
    if root == destination or root in destination.parents or destination in root.parents:
        raise ValidationError('Destination must be outside source tree')
    if os.path.lexists(destination):
        raise ValidationError('Destination already exists')
    names = read_manifest(root)
    payload = {}
    for name in names:
        path = root / name
        checked_chain(path)
        data = path.read_bytes()
        scan(name, data)
        payload[name] = data
    hashes = {name: digest(payload[name]) for name in names}
    # Atomic exclusive directory creation; no overwrite or cleanup on failure.
    destination.mkdir()
    for name in names:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        checked_chain(target.parent, directory=True)
        with target.open('xb') as handle:
            handle.write(payload[name])
    verify(root, destination, names, hashes)
    return {'files': [{'path': n, 'sha256': hashes[n]} for n in names],
            'verification': 'passed',
            'limitations': 'Pattern scan only; manual privacy, provenance and licensing review still required.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New directory outside the source tree; parent must exist')
    args = parser.parse_args()
    try:
        result = export(Path(__file__).absolute().parent, args.output)
    except (ValidationError, OSError, UnicodeError):
        # Do not print paths, OS diagnostics, file contents or candidate secrets.
        print('Snapshot rejected. Check manifest, privacy patterns, ordinary files and unused output path. Any partial output must not be published.', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
