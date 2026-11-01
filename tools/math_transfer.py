#!/usr/bin/env python3
"""Transfer a file through the UI without overwriting an existing destination.

Uploads are staged by the project service and published on the final chunk.
Downloads are checked for a changing revision and published locally only when
complete. An uncertain request is never retried automatically.
"""
import argparse
import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

CHUNK = 256 * 1024
MAX_FILE = 512 * 1024 * 1024


def call(args, request):
    client = Path(__file__).with_name('math_client.py')
    cmd = [sys.executable, str(client), '--vm', args.vm, '--socket', args.socket, 'request', '-']
    result = subprocess.run(cmd, input=json.dumps(request, ensure_ascii=False), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f'UI request failed; outcome may be unknown: {result.stderr or result.stdout}')
    return json.loads(result.stdout)['ok']


def upload(args):
    with os.fdopen(os.open(args.local, os.O_RDONLY | os.O_NONBLOCK), 'rb') as f:
        original = os.fstat(f.fileno())
        if not stat.S_ISREG(original.st_mode) or original.st_size > MAX_FILE:
            raise ValueError('source must be a regular file no larger than 512 MiB')
        offset = 0
        while True:
            data = f.read(min(CHUNK, original.st_size - offset))
            if len(data) != min(CHUNK, original.st_size - offset):
                raise ValueError('source changed while reading')
            last = offset + len(data) == original.st_size
            if last:
                now = os.fstat(f.fileno())
                if (now.st_size, now.st_mtime_ns, now.st_ctime_ns) != (original.st_size, original.st_mtime_ns, original.st_ctime_ns):
                    raise ValueError('source changed while reading')
            response = call(args, {'op': 'files.upload', 'dir': args.dir, 'path': args.remote,
                                   'offset': offset, 'data_base64': base64.b64encode(data).decode('ascii'),
                                   'last': last})
            offset += len(data)
            if response.get('conflict') or response.get('size') != offset:
                raise ValueError('upload was rejected or returned an unexpected offset')
            if last:
                break
    print(f'Uploaded {offset} bytes.')


def download(args):
    destination = Path(args.local)
    fd, name = tempfile.mkstemp(prefix='.math-download-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            offset = 0
            version = None
            while True:
                chunk = call(args, {'op': 'files.download', 'dir': args.dir, 'path': args.remote, 'offset': offset})
                current = (chunk['revision'], chunk['size'])
                if not isinstance(current[0], str) or not isinstance(current[1], int) or not 0 <= current[1] <= MAX_FILE:
                    raise ValueError('invalid download metadata')
                if version is None:
                    version = current
                elif version != current:
                    raise ValueError('remote file changed while downloading')
                data = base64.b64decode(chunk['data_base64'], validate=True)
                if len(data) > CHUNK or offset + len(data) > current[1]:
                    raise ValueError('invalid download chunk')
                f.write(data)
                offset += len(data)
                next_offset = chunk['next_offset']
                if next_offset is None:
                    if offset != current[1]:
                        raise ValueError('incomplete download')
                    break
                if next_offset != offset or not data or offset >= current[1]:
                    raise ValueError('invalid download offset')
        os.link(name, destination)  # atomic, fails if the destination exists
    finally:
        os.unlink(name)
    print(f'Downloaded {offset} bytes.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm', default='open-math')
    parser.add_argument('--socket', required=True)
    parser.add_argument('operation', choices=['upload', 'download'])
    parser.add_argument('dir', help='project directory ID')
    parser.add_argument('remote', help='path inside the project')
    parser.add_argument('local', help='local source or destination')
    args = parser.parse_args()
    try:
        (upload if args.operation == 'upload' else download)(args)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f'transfer failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
