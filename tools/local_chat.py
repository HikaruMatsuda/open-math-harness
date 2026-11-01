#!/usr/bin/env python3
"""Run the interactive local composition inside Linux.

It adds to the workspace-only composition a `model` service and the `sessions`
service that starts one chat agent per session, so a window can hold a real
conversation, and the agent may read and write `work/` through `fs`. Projects,
file management and history are the same services the workspace composition
uses. Shell, approvals, skills, memory and web are not part of it: the agent
has no such capability and no process it starts can reach one.

The model adapter is the existing OpenRouter-shaped one; it may reach only the
base URL it is given. Which model it may run is the sessions grant, and the
agent asks for exactly that id. The state directory must be private: the UI
socket grants its owner the listed capabilities.
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


def provider_of(model_id):
    provider, _, rest = model_id.partition('/')
    if not provider or not rest:
        raise ValueError('--model must be a provider/model id')
    return provider


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise ValueError(f'{path} must be owned by the current user and have mode 0700')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', required=True, type=Path)
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--model-base-url', required=True,
                        help='OpenAI-compatible base URL reachable from inside Linux, e.g. http://127.0.0.1:10100/v1')
    parser.add_argument('--model', default='opencode-go/deepseek-v4.1-flash',
                        help='provider/model id the sessions grant authorizes')
    parser.add_argument('--model-key-file', type=Path,
                        help='host file holding the bearer key; default a local placeholder, '
                             'for a gateway that ignores the header')
    parser.add_argument('--max-steps', default='40')
    args = parser.parse_args()
    provider = provider_of(args.model)
    os.umask(0o077)
    state = args.state_dir.expanduser().absolute()
    try:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_dir(state)
    except ValueError as exc:
        parser.error(str(exc))
    lock = os.open(state / 'workspace.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    if not stat.S_ISREG(os.fstat(lock).st_mode):
        parser.error('workspace lock must be a regular file')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('workspace is already running')
    os.set_inheritable(lock, True)
    try:
        for name in ('projects', 'home', 'sessions', 'ui', 'data', 'data/work', 'logs', 'secrets'):
            private_dir(state / name)
    except ValueError as exc:
        parser.error(str(exc))
    binaries = args.bin_dir.expanduser().resolve(strict=True)

    def executable(name):
        path = binaries / ('nucleus-' + name)
        if not path.is_file() or not os.access(path, os.X_OK):
            parser.error(f'missing executable: {path}')
        return str(path)

    def bind(host, guest, writable=False):
        return dict(host=str(host), guest=guest, writable=writable)

    key = state / 'secrets' / 'key'
    if args.model_key_file is not None:
        text = args.model_key_file.expanduser().read_text(encoding='utf-8').strip()
        if not text:
            parser.error('--model-key-file is empty')
    else:
        text = 'local-gateway'
    fd = os.open(key, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    if stat.S_IMODE(key.stat().st_mode) != 0o600:
        parser.error('key file must have mode 0600')

    programs = {
        'conversation': dict(executable=executable('conversation'), args=['--root', '/sessions'],
                             binds=[bind(state / 'sessions', '/sessions', True)]),
        'projects': dict(executable=executable('projects'),
                         args=['--root', '/projects', '--home', '/home', '--programs', '/programs'],
                         binds=[bind(state / 'projects', '/projects', True), bind(state / 'home', '/home', True),
                                bind(executable('project'), '/programs/nucleus-project')]),
        'fs': dict(executable=executable('fs'),
                   args=['--root', '/data', '--access-log', '/log/access.jsonl'],
                   binds=[bind(state / 'data', '/data', True), bind(state / 'logs', '/log', True)]),
        'model': dict(executable=executable('openrouter'),
                      args=['--key-file', '/secrets/key', '--base-url', args.model_base_url,
                            '--max-timeout-ms', '300000'],
                      binds=[bind(state / 'secrets', '/secrets')], network=True),
        'chat': dict(executable=executable('chat-agent'),
                     args=['--model', args.model, '--max-steps', args.max_steps]),
        'sessions': dict(executable=executable('chat-agent'),
                         args=['--child-executable', '/programs/nucleus-chat-agent',
                               '--child-arg', '--model', '--child-arg', args.model,
                               '--child-arg', '--max-steps', '--child-arg', args.max_steps],
                         binds=[bind(binaries, '/programs')]),
        'ui': dict(executable=executable('ui'), args=['--socket', '/ui/ui.sock'],
                   binds=[bind(state / 'ui', '/ui', True)]),
    }
    config = dict(lifetime='resident', programs=programs, boot=[
        dict(program='conversation', provides='conversation',
             grants=[grant('kernel', ['provide'], ['endpoint', 'conversation'])]),
        dict(program='projects', provides='projects', grants=[
            grant('kernel', ['provide'], ['endpoint', 'projects']),
            grant('kernel', ['provide'], ['endpoint', 'files']),
            grant('kernel', ['create_endpoint'], ['endpoint'], True),
            grant('kernel', ['spawn_executable'], ['executable', 'programs'])]),
        dict(program='fs', provides='fs',
             grants=[grant('kernel', ['provide'], ['endpoint', 'fs'])]),
        dict(program='model', provides='model',
             grants=[grant('kernel', ['provide'], ['endpoint', 'model'])]),
        dict(program='sessions', provides='sessions', grants=[
            grant('kernel', ['provide'], ['endpoint', 'sessions']),
            grant('kernel', ['spawn_executable'], ['executable', 'programs']),
            grant('conversation', ['receive', 'post', 'history', 'remind', 'reminders', 'cancel_reminder'], [],
                  True),
            grant('conversation', ['clear_reminders', 'list'], []),
            grant('model', ['complete', 'start'], [provider], True),
            grant('fs', ['read', 'list', 'create', 'append'], ['work'], True)]),
        dict(program='ui', grants=[
            grant('conversation', ['say', 'history', 'wait', 'list', 'ensure'], []),
            grant('sessions', ['create', 'stop', 'list'], []),
            grant('projects', ['list', 'create'], ['projects']),
            grant('files', ['list', 'mkdir', 'upload', 'download'], ['projects'])]),
    ])
    config_path = state / 'chat-config.json'
    fd, tmp = tempfile.mkstemp(prefix='.chat-config-', dir=state)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(config, f)
    os.replace(tmp, config_path)
    os.execv(str(binaries / 'nucleus'), [str(binaries / 'nucleus'), 'serve', str(config_path)])


if __name__ == '__main__':
    main()
