#!/usr/bin/env python3
"""Run the local workspace-only composition inside Linux.

The UI socket grants its owner the listed capabilities, so the state directory
must be private. Projects run in bubblewrap without a separate Linux identity.
This composition has no model or sessions service and cannot run a chat agent.
It grants no deletion or shell capability. Failure to confine is a startup error.
"""

import argparse
import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path


def grant(endpoint, actions, resource, delegable=False):
    return dict(endpoint=endpoint, actions=actions, resource=resource, delegable=delegable)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', required=True, type=Path)
    parser.add_argument('--state-dir', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    state = args.state_dir.expanduser().absolute()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink() or state.stat().st_uid != os.getuid() or stat.S_IMODE(state.stat().st_mode) != 0o700:
        parser.error('state directory must be owned by the current user and have mode 0700')
    lock = os.open(state / 'workspace.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    if not stat.S_ISREG(os.fstat(lock).st_mode):
        parser.error('workspace lock must be a regular file')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('workspace is already running')
    os.set_inheritable(lock, True)
    for name in ('projects', 'home', 'sessions', 'ui'):
        directory = state / name
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
            parser.error(f'{directory} must be owned by the current user and have mode 0700')
    binaries = args.bin_dir.expanduser().resolve(strict=True)
    def executable(name):
        path = binaries / ('nucleus-' + name)
        if not path.is_file() or not os.access(path, os.X_OK):
            parser.error(f'missing executable: {path}')
        return str(path)

    def bind(host, guest, writable=False):
        return dict(host=str(host), guest=guest, writable=writable)

    programs = {
        'conversation': dict(executable=executable('conversation'), args=['--root', '/sessions'],
                             binds=[bind(state / 'sessions', '/sessions', True)]),
        'projects': dict(executable=executable('projects'),
                         args=['--root', '/projects', '--home', '/home', '--programs', '/programs'],
                         binds=[bind(state / 'projects', '/projects', True), bind(state / 'home', '/home', True),
                                bind(executable('project'), '/programs/nucleus-project')]),
        'ui': dict(executable=executable('ui'), args=['--socket', '/ui/ui.sock'],
                   binds=[bind(state / 'ui', '/ui', True)]),
    }
    config = dict(lifetime='resident', programs=programs, boot=[
        dict(program='conversation', provides='conversation', grants=[grant('kernel', ['provide'], ['endpoint', 'conversation'])]),
        dict(program='projects', provides='projects', grants=[
            grant('kernel', ['provide'], ['endpoint', 'projects']),
            grant('kernel', ['provide'], ['endpoint', 'files']),
            grant('kernel', ['create_endpoint'], ['endpoint'], True),
            grant('kernel', ['spawn_executable'], ['executable', 'programs'])]),
        dict(program='ui', grants=[
            grant('conversation', ['say', 'history', 'wait', 'list', 'ensure'], []),
            grant('projects', ['list', 'create'], ['projects']),
            grant('files', ['list', 'mkdir'], ['projects'])]),
    ])
    config_path = state / 'workspace-config.json'
    fd, tmp = tempfile.mkstemp(prefix='.workspace-config-', dir=state)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(config, f)
    os.replace(tmp, config_path)
    os.execv(str(binaries / 'nucleus'), [str(binaries / 'nucleus'), 'serve', str(config_path)])


if __name__ == '__main__':
    main()
