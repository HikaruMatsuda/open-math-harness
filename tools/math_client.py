#!/usr/bin/env python3
"""Send one UI operation through the Lima bridge; UI capabilities remain authoritative.

Requests are never retried: after a connection failure a mutation may have completed.
The response is printed as JSON so errors and pagination cursors remain visible.
"""
import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm', default='open-math')
    parser.add_argument('--socket', required=True, help='absolute UI socket path in the VM')
    parser.add_argument('--project', default='default')
    parser.add_argument('--session', default='default')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('projects')
    create = commands.add_parser('create-project')
    create.add_argument('name')
    files = commands.add_parser('files', help='list files in a project directory')
    files.add_argument('dir', help='directory ID returned by projects')
    files.add_argument('path', nargs='?', default='')
    files.add_argument('--after', help='pagination cursor')
    mkdir = commands.add_parser('mkdir', help='create a project directory')
    mkdir.add_argument('dir', help='directory ID returned by projects')
    mkdir.add_argument('path')
    commands.add_parser('sessions')
    commands.add_parser('live-sessions')
    start = commands.add_parser('start')
    start.add_argument('--model')
    commands.add_parser('stop')
    commands.add_parser('history')
    wait = commands.add_parser('wait')
    wait.add_argument('--after-seq', type=int)
    say = commands.add_parser('say')
    say.add_argument('text')
    raw = commands.add_parser('request', help='send an explicit JSON UI request')
    raw.add_argument('json', help="JSON object, or '-' to read one object from stdin")
    args = parser.parse_args()
    if args.command == 'projects':
        request = {'op': 'projects.list'}
    elif args.command == 'create-project':
        request = {'op': 'projects.create', 'name': args.name}
    elif args.command == 'files':
        request = {'op': 'files.list', 'dir': args.dir, 'path': args.path}
        if args.after is not None:
            request['after'] = args.after
    elif args.command == 'mkdir':
        request = {'op': 'files.mkdir', 'dir': args.dir, 'path': args.path}
    elif args.command == 'sessions':
        request = {'op': 'conversation.list', 'project': args.project}
    elif args.command == 'live-sessions':
        request = {'op': 'sessions.list'}
    elif args.command in ('start', 'stop'):
        request = {'op': 'sessions.create' if args.command == 'start' else 'sessions.stop',
                   'project': args.project, 'session': args.session}
        if args.command == 'start' and args.model:
            request['model'] = args.model
    elif args.command == 'history':
        request = {'op': 'conversation.history', 'project': args.project, 'session': args.session}
    elif args.command == 'wait':
        request = {'op': 'conversation.wait', 'project': args.project, 'session': args.session}
        if args.after_seq is not None:
            request['after_seq'] = args.after_seq
    elif args.command == 'say':
        request = {'op': 'conversation.say', 'project': args.project, 'session': args.session,
                   'message_id': uuid.uuid4().hex, 'text': args.text}
    else:
        try:
            request = json.load(sys.stdin) if args.json == '-' else json.loads(args.json)
            if not isinstance(request, dict):
                raise ValueError('request must be a JSON object')
        except ValueError as exc:
            parser.error(str(exc))
    request.setdefault('id', 1)
    bridge = Path(__file__).with_name('lima_ui_bridge.py')
    result = subprocess.run([sys.executable, str(bridge), '--vm', args.vm, args.socket],
                            input=json.dumps(request, ensure_ascii=False) + '\n', text=True,
                            stdout=subprocess.PIPE, check=False)
    if result.returncode:
        return result.returncode
    try:
        response = json.loads(result.stdout)
        if response.get('id') != request['id']:
            raise ValueError('response ID does not match request')
    except (ValueError, AttributeError) as exc:
        print(f'invalid UI response: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 1 if 'error' in response or 'ok' not in response else 0


if __name__ == '__main__':
    sys.exit(main())
