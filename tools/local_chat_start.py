#!/usr/bin/env python3
"""Start the interactive local Open Math composition and print its UI socket.

It runs on the Mac and does three things, each only when it is not already done:
opens a reverse tunnel for the model gateway onto the VM's loopback, copies the
composition script into the VM, and starts the composition there in its own
session. The socket path it prints is what `math_client.py --socket` takes.

The gateway URL must be a loopback address on the Mac: the tunnel exposes it to
the VM's loopback only, never to the VM's network interfaces.
"""

import argparse
import json
import shlex
import subprocess
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent


def shell_path(path):
    """A shell word for `path`, leaving a leading `~` unquoted so the VM's shell expands it."""
    if path.startswith('~/'):
        return '~/' + shlex.quote(path[2:])
    return shlex.quote(path)


def ssh_config(vm):
    config = Path.home() / '.lima' / vm / 'ssh.config'
    return config if config.is_file() else None


def vm_run(vm, script, check=True):
    return subprocess.run(['limactl', 'shell', vm, 'bash', '-lc', script],
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def loopback(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or parsed.port is None:
        raise ValueError('--model-base-url must be an http(s) URL with an explicit port')
    if parsed.hostname not in ('127.0.0.1', 'localhost'):
        raise ValueError('--model-base-url must be a loopback address on the Mac')
    return parsed.hostname, parsed.port


def gateway_reachable(vm, url):
    result = vm_run(vm, f'curl -sS -m 5 -o /dev/null -w "%{{http_code}}" {shlex.quote(url + "/models")}',
                    check=False)
    return result.returncode == 0 and result.stdout.strip() in ('200', '401', '403')


def open_tunnel(vm, host, port):
    config = ssh_config(vm)
    if config is None:
        raise SystemExit(f'no Lima ssh config for {vm}; is the VM started?')
    forward = f'{host}:{port}:{host}:{port}'
    result = subprocess.run(['ssh', '-F', str(config), '-f', '-N', '-R', forward, f'lima-{vm}'],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise SystemExit(f'tunnel failed: {result.stderr.strip() or result.returncode}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm', default='open-math')
    parser.add_argument('--state-dir', default='~/open-math-run/chat', help='state directory inside the VM')
    parser.add_argument('--model-base-url', default='http://127.0.0.1:10100/v1')
    parser.add_argument('--model', default='opencode-go/deepseek-v4.1-flash')
    parser.add_argument('--repo', default='~/open-math-harness', help='repository checkout inside the VM')
    parser.add_argument('--bin-dir', default='target/debug', help='built programs inside the VM checkout')
    parser.add_argument('--unit', default='open-math-chat', help='systemd --user unit that keeps the composition')
    parser.add_argument('--memory-quiet-ms', default='300000',
                        help='how long a session must be quiet before its memories are extracted')
    parser.add_argument('--status', action='store_true', help='report what is running; start nothing')
    parser.add_argument('--stop', action='store_true', help='stop the composition and the tunnel; start nothing')
    args = parser.parse_args()
    try:
        host, port = loopback(args.model_base_url)
    except ValueError as exc:
        parser.error(str(exc))
    repo = args.repo
    state = args.state_dir
    socket_path = f'{state.rstrip("/")}/ui/ui.sock'
    home = vm_run(args.vm, 'printf %s "$HOME"', check=False).stdout.strip()
    def absolute(path):
        # systemd-run does not expand `~` in --working-directory, so send absolute paths.
        return home + path[1:] if home and path.startswith('~/') else path

    shown_socket = absolute(socket_path)
    repo = absolute(repo)
    state = absolute(state)
    state_key = state[2:] if state.startswith('~/') else state
    # The bracket keeps this checker's own command line from matching the pattern.
    process_pattern = f'{state_key.rstrip("/")}/chat[-]config.json'
    status = vm_run(args.vm, f'if pgrep -f {shlex.quote(process_pattern)} > /dev/null; '
                             f'then echo up; else echo down; fi', check=False)
    running = status.stdout.strip() == 'up'
    unit = args.unit
    unit_state = vm_run(args.vm, f'systemctl --user is-active {shlex.quote(unit)}', check=False).stdout.strip()
    if args.stop:
        vm_run(args.vm, f'systemctl --user stop {shlex.quote(unit)}', check=False)
        subprocess.run(['pkill', '-f', f'[-]R {host}:{port}:{host}:{port}'], check=False)
        print(json.dumps({'running': False, 'unit': unit}, indent=2))
        return 0
    if args.status:
        print(json.dumps({'running': running, 'socket': shown_socket,
                          'unit': unit, 'unit_active': unit_state,
                          'gateway_reachable': gateway_reachable(args.vm, args.model_base_url)}, indent=2))
        return 0
    if not gateway_reachable(args.vm, args.model_base_url):
        open_tunnel(args.vm, host, port)
        if not gateway_reachable(args.vm, args.model_base_url):
            print('model gateway is still unreachable inside the VM after opening the tunnel', file=sys.stderr)
            return 1
    script = HERE / 'local_chat.py'
    if not script.is_file():
        print(f'missing {script}', file=sys.stderr)
        return 1
    source = script.read_text(encoding='utf-8')
    subprocess.run(['limactl', 'shell', args.vm, 'bash', '-lc',
                    f'cd {shlex.quote(repo)} && cat > tools/local_chat.py'], input=source, text=True, check=True)
    if not running:
        arguments = ' '.join([
            '/usr/bin/python3 tools/local_chat.py',
            '--bin-dir', shlex.quote(args.bin_dir),
            '--state-dir', shlex.quote(state),
            '--model-base-url', shlex.quote(args.model_base_url),
            '--model', shlex.quote(args.model),
            '--memory-quiet-ms', shlex.quote(args.memory_quiet_ms),
        ])
        command = (f'cd {shlex.quote(repo)} && systemd-run --user --unit={shlex.quote(unit)} --collect '
                   f'--working-directory={shlex.quote(repo)} {arguments}')
        started = vm_run(args.vm, command, check=False)
        if started.returncode != 0:
            print((started.stderr or started.stdout).strip(), file=sys.stderr)
            return 1
        for _ in range(30):
            status = vm_run(args.vm, f'if pgrep -f {shlex.quote(process_pattern)} > /dev/null; '
                                     f'then echo up; else echo down; fi', check=False)
            if status.stdout.strip() == 'up':
                running = True
                break
            subprocess.run(['sleep', '0.5'], check=False)
        if not running:
            log = vm_run(args.vm, f'journalctl --user -u {shlex.quote(unit)} -n 20 --no-pager', check=False)
            print(log.stdout.strip() or 'the composition did not open its socket', file=sys.stderr)
            return 1
    print(json.dumps({'running': True, 'socket': shown_socket, 'model': args.model}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
