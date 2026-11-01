#!/usr/bin/env python3
"""Send one UI operation through the Lima bridge; UI capabilities remain authoritative.

Requests are never retried: after a connection failure a mutation may have completed.
The response is printed as JSON so errors and pagination cursors remain visible.

Usage, where `<socket>` is the path `local_chat_start.py` printed
(`~/open-math-run/chat/ui/ui.sock` in the VM):

  CLIENT="python3 tools/math_client.py --socket <socket>"

  $CLIENT --project demo --session chat1 say "2+2 を計算して"   # writes a turn, starts a
                                                              # stopped session first
  $CLIENT --project demo --session chat1 wait                  # waits for the reply
  $CLIENT --project demo --session chat1 history               # user / assistant / tool / model_call
  $CLIENT projects | create-project notes | files <dir> [path] | mkdir <dir> <name>
  $CLIENT --project notes diffs | diff repo | files notes src
  $CLIENT approvals                       # pending approvals, each with its digest
  $CLIENT decide <id> <digest> [--deny]   # the digest must be the one just listed

`say` starts a stopped session and never restarts a running one, so a turn in progress is
not interrupted. `start` and `stop` act on a session explicitly. Everything except `fs
remove`, which always asks, is available as `fs`, `shell` (bubblewrap, working directory
the project's `repo/`), `skills`, `memory`, `web`, `jobs`, `diffs`, `lean` (frozen
problems and the resident REPL), `approvals` and `pdf`. A `rm` run through `shell` does
not pass the approval guard, the same limit the deployed composition has.
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
    commands.add_parser('diffs', help="list the project repository's uncommitted changes source")
    diff = commands.add_parser('diff', help='list the changed files of a project repository')
    diff.add_argument('repository')
    diff.add_argument('--after', help='pagination cursor')
    approvals = commands.add_parser('approvals', help='list requests waiting for a decision')
    approvals.add_argument('--after', type=int, help='pagination cursor')
    approvals.add_argument('--limit', type=int, help='page size')
    decide = commands.add_parser('decide', help='approve or deny one waiting request')
    decide.add_argument('approval', type=int, help='request id from `approvals`')
    decide.add_argument('digest', help='the digest the listing showed')
    decide.add_argument('--deny', action='store_true', help='refuse the request instead of approving it')
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
    elif args.command == 'diffs':
        request = {'op': 'diffs.repositories', 'project': args.project}
    elif args.command == 'diff':
        request = {'op': 'diffs.changes', 'project': args.project, 'repository': args.repository}
        if args.after is not None:
            request['after'] = args.after
    elif args.command == 'approvals':
        request = {'op': 'approvals.list'}
        if args.after is not None:
            request['after'] = args.after
        if args.limit is not None:
            request['limit'] = args.limit
    elif args.command == 'decide':
        request = {'op': 'approvals.decide', 'approval': args.approval,
                   'digest': args.digest, 'allow': not args.deny}
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
    if args.command == 'say':
        started, failure = ensure_started(bridge, args)
        if failure is not None:
            print(json.dumps(failure, ensure_ascii=False, indent=2))
            return 1
        if started:
            print('session was not live; started it first', file=sys.stderr)
    response, failure = send(bridge, args, request)
    if failure is not None:
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 1 if 'error' in response or 'ok' not in response else 0


def send(bridge, args, request):
    """Send one request; return (response, failure) and never retry a mutation."""
    result = subprocess.run([sys.executable, str(bridge), '--vm', args.vm, args.socket],
                            input=json.dumps(request, ensure_ascii=False) + '\n', text=True,
                            stdout=subprocess.PIPE, check=False)
    if result.returncode:
        return None, {'error': {'code': 'bridge_failed',
                                'message': f'bridge exited with status {result.returncode}'}}
    try:
        response = json.loads(result.stdout)
        if response.get('id') != request['id']:
            raise ValueError('response ID does not match request')
    except (ValueError, AttributeError) as exc:
        return None, {'error': {'code': 'invalid_response', 'message': str(exc)}}
    return response, None


def ensure_started(bridge, args):
    """Start the session only when the UI does not list it as live.

    Returns (started, failure). A session that is already live is left alone so
    that saying something cannot restart the process answering an earlier turn.
    """
    listed, failure = send(bridge, args, {'op': 'sessions.list', 'id': 1})
    if failure is not None:
        return False, failure
    if 'error' in listed or 'ok' not in listed:
        return False, listed
    for session in listed['ok'].get('sessions', []):
        if session.get('name') == args.session and session.get('project') == args.project:
            return False, None
    started, failure = send(bridge, args, {'op': 'sessions.create', 'id': 1,
                                           'project': args.project, 'session': args.session})
    if failure is not None:
        return False, failure
    if 'error' in started or 'ok' not in started:
        return False, started
    return True, None


if __name__ == '__main__':
    sys.exit(main())
