#!/usr/bin/env python3
"""Relay JSON lines to a UI Unix socket inside a Lima VM.

The socket's existing Unix permissions and UI capability checks remain authoritative.
Input and output are passed unchanged. On input EOF, wait for one answer per complete
request before closing: the UI discards outstanding answers on a half-close. A lost
connection with requests outstanding is an error; requests are never retried.
"""
import argparse
import subprocess
import sys

REMOTE = r'''
import os, selectors, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sys.argv[1])
sel = selectors.DefaultSelector()
sel.register(sys.stdin.buffer, selectors.EVENT_READ, 'input')
sel.register(s, selectors.EVENT_READ, 'socket')
pending = 0
input_open = True
partial = False
while True:
    for key, _ in sel.select():
        data = os.read(key.fileobj.fileno(), 65536)
        if not data:
            if key.data == 'input':
                sel.unregister(key.fileobj)
                input_open = False
                if partial:
                    sys.exit('incomplete request line')
                if not pending:
                    sys.exit(0)
                continue
            if pending:
                sys.exit('UI disconnected with unanswered requests')
            sys.exit(0 if not input_open else 1)
        if key.data == 'input':
            pending += data.count(b'\n')
            partial = not data.endswith(b'\n')
            s.sendall(data)
        else:
            view = memoryview(data)
            while view:
                view = view[os.write(sys.stdout.fileno(), view):]
            pending -= data.count(b'\n')
            if pending < 0:
                sys.exit('unexpected UI answer')
            if not input_open and not pending:
                sys.exit(0)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm', default='open-math')
    parser.add_argument('socket', help='absolute Unix socket path inside the VM')
    args = parser.parse_args()
    if not args.socket.startswith('/'):
        parser.error('socket path must be absolute')
    result = subprocess.run(['limactl', 'shell', args.vm, 'python3', '-u', '-c', REMOTE, args.socket], check=False)
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
