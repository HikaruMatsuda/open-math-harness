"""Regression tests for the Mac-side start/stop helper.

These cover the decisions that broke in practice and need no VM: how a unit is
classified, how the reverse forward is closed, and the URL and path handling.
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import local_chat_start


class ShellPath(unittest.TestCase):
    def test_expands_home_but_quotes_everything_else(self):
        self.assertEqual(local_chat_start.shell_path('~/open-math-run/chat'), '~/open-math-run/chat')
        self.assertEqual(local_chat_start.shell_path('~/a b'), "~/'a b'")
        self.assertEqual(local_chat_start.shell_path('/tmp/a b'), "'/tmp/a b'")
        self.assertEqual(local_chat_start.shell_path('/tmp/x'), '/tmp/x')


class Loopback(unittest.TestCase):
    def test_accepts_loopback_with_explicit_port(self):
        self.assertEqual(local_chat_start.loopback('http://127.0.0.1:10100/v1'), ('127.0.0.1', 10100))
        self.assertEqual(local_chat_start.loopback('https://localhost:8443/v1'), ('localhost', 8443))

    def test_rejects_anything_the_tunnel_cannot_expose(self):
        for url in ['http://127.0.0.1/v1', 'http://192.168.1.5:10100/v1',
                    'http://example.com:10100/v1', 'file://127.0.0.1:10100/v1']:
            with self.assertRaises(ValueError, msg=url):
                local_chat_start.loopback(url)


class CloseTunnel(unittest.TestCase):
    def test_cancels_the_forward_on_the_shared_connection(self):
        calls = []

        def record(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')

        with patch.object(local_chat_start, 'ssh_config', return_value=Path('/tmp/ssh.config')), \
                patch.object(local_chat_start.subprocess, 'run', side_effect=record):
            local_chat_start.close_tunnel('open-math', '127.0.0.1', 10100)

        cancels = [c for c in calls if 'ssh' in c[0] and '-O' in c]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0][cancels[0].index('-O') + 1], 'cancel')
        forward = cancels[0][cancels[0].index('-R') + 1]
        self.assertEqual(forward, '127.0.0.1:10100:127.0.0.1:10100')
        self.assertEqual(cancels[0][-1], 'lima-open-math')

    def test_still_kills_a_standalone_forward_without_a_config(self):
        calls = []

        def record(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')

        with patch.object(local_chat_start, 'ssh_config', return_value=None), \
                patch.object(local_chat_start.subprocess, 'run', side_effect=record):
            local_chat_start.close_tunnel('open-math', '127.0.0.1', 10100)

        self.assertFalse([c for c in calls if c[0] == 'ssh'])
        self.assertTrue([c for c in calls if c[0] == 'pkill'])


class UnitInstalled(unittest.TestCase):
    def test_a_unit_file_survives_a_restart_and_a_transient_unit_does_not(self):
        installed = subprocess.CompletedProcess([], 0, 'open-math-chat-boot.service enabled\n', '')
        transient = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(local_chat_start, 'vm_run', return_value=installed):
            self.assertTrue(local_chat_start.unit_installed('open-math', 'open-math-chat-boot'))
        with patch.object(local_chat_start, 'vm_run', return_value=transient):
            self.assertFalse(local_chat_start.unit_installed('open-math', 'open-math-chat-boot'))

    def test_a_failing_query_is_not_installed(self):
        failed = subprocess.CompletedProcess([], 1, 'open-math-chat-boot.service\n', 'no such unit')
        with patch.object(local_chat_start, 'vm_run', return_value=failed):
            self.assertFalse(local_chat_start.unit_installed('open-math', 'open-math-chat-boot'))


if __name__ == '__main__':
    unittest.main()
