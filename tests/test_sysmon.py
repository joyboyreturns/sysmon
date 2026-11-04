import os
import sys
import time
import unittest
from unittest.mock import patch
from sysmon.app import memory_bytes, percent, bytesize, core_kind, cpu_in_list, cpu_identity


class Metrics(unittest.TestCase):
    def test_core_class_detection(self):
        intel = {'vendor_id': 'GenuineIntel', 'cpu family': '6', 'model': '165'}
        with patch('sysmon.app.read', return_value=''), patch('sysmon.app.cpuid', return_value=None):
            self.assertIn('no P/E split', core_kind(intel, '0'))
            self.assertIn('AMD core (class unreported)', core_kind({'vendor_id': 'AuthenticAMD'}, '0'))
            self.assertIn('class unreported', core_kind(dict(intel, model='999'), '0'))
        def pmus(path, fallback=''):
            return '0-7' if '/cpu_core/' in path else '8-15'
        with patch('sysmon.app.read', side_effect=pmus):
            self.assertIn('P-core', core_kind(intel, '2'))
            self.assertIn('E-core', core_kind(intel, '9'))

    def test_amd_cpuid_classes(self):
        amd = {'vendor_id': 'AuthenticAMD'}
        for code, label in [(0, 'Performance'), (1, 'compact'), (2, 'Low-power')]:
            def regs(cpu, leaf):
                return (0x80000026, 0, 0, 0) if leaf == 0x80000000 else (1 << 30, (code << 28) | 2, 0, 0)
            with patch('sysmon.app.cpuid', side_effect=regs):
                self.assertIn(label, core_kind(amd, '2'))
        # A supported leaf without the heterogeneous-core flag is insufficient.
        with patch('sysmon.app.cpuid', side_effect=[(0x80000026, 0, 0, 0), (0, 2, 0, 0)]):
            self.assertIn('unreported', core_kind(amd, '0'))

    def test_intel_cpuid_fallback(self):
        for code, label in [(0x40, 'P-core'), (0x20, 'E-core')]:
            with patch('sysmon.app.read', return_value=''), patch('sysmon.app.cpuid',
                      side_effect=[(0x1a, 0, 0, 0), (code << 24, 0, 0, 0)]):
                self.assertIn(label, core_kind({'vendor_id': 'GenuineIntel'}, '0'))

    def test_arm_riscv_and_unknown_identity(self):
        arm = {'CPU implementer': '0x41', 'CPU part': '0xd05'}
        self.assertIn('Cortex-A55', core_kind(arm, '0'))
        self.assertEqual(cpu_identity(arm)[0], 'Arm')
        self.assertIn('Qualcomm', core_kind({'CPU implementer': '0x51', 'CPU part': '0xfff'}, '0'))
        self.assertIn('0xfff', core_kind({'CPU implementer': '0x99', 'CPU part': '0xfff'}, '0'))
        self.assertIn('RISC-V', core_kind({'uarch': 'sifive,u74-mc'}, '0'))
        self.assertEqual(cpu_identity({'cpu': 'POWER10', 'vendor': 'IBM'}), ('IBM', 'POWER10'))
        self.assertIn('Unknown', core_kind({}, '0'))

    def test_cpu_range_membership(self):
        self.assertTrue(cpu_in_list(12, '0-3,8,10-15'))
        self.assertFalse(cpu_in_list(4, '0-3,8,10-15'))
        self.assertFalse(cpu_in_list(0, ''))

    def test_docker_memory_sorting_uses_bytes(self):
        self.assertGreater(memory_bytes('1.5GiB / 16GiB'), memory_bytes('950MiB / 16GiB'))
        self.assertEqual(memory_bytes('512MiB / 16GiB'), 512 * 1024**2)
        self.assertEqual(memory_bytes('1GB / 8GB'), 10**9)
        self.assertEqual(memory_bytes('--'), 0)

    def test_multicore_cpu_is_not_clamped(self):
        self.assertEqual(percent('245.5%'), 245.5)
        self.assertEqual(percent('--'), 0)

    def test_byte_units(self):
        self.assertEqual(bytesize(1024**3), '1.0 GiB')


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux /proc and PTY required')
class LiveTerminal(unittest.TestCase):
    def test_navigation_resize_and_restore(self):
        import fcntl
        import pty
        import select
        import struct
        import subprocess
        import termios
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 120, 0, 0))
        proc = subprocess.Popen([sys.executable, '-m', 'sysmon', '.2'], stdin=slave,
                                stdout=slave, stderr=slave, env=dict(os.environ, TERM='xterm-256color'))
        os.close(slave)
        output = bytearray()
        def collect(seconds):
            until = time.monotonic() + seconds
            part = bytearray()
            while time.monotonic() < until:
                if select.select([master], [], [], .05)[0]:
                    try:
                        part.extend(os.read(master, 65536))
                    except OSError:
                        break
            output.extend(part)
            return part
        try:
            collect(1)
            for key, expected in [(b'2', b'CPU HARDWARE'), (b'3', b'MEMORY'),
                                  (b'4', b'DOCKER'), (b'5', b'PROCESSES'),
                                  (b'6', b'FILESYSTEMS'), (b'?', b'KEYBOARD')]:
                os.write(master, key)
                self.assertIn(expected, collect(.4))
            os.write(master, b'?4/zzzz-no-match\n')
            self.assertIn(b'No matching containers', collect(.4))
            os.write(master, b'\x1b')
            collect(.5)
            os.write(master, b'4m\n\n\x1b[B\x1b[A')
            collect(.4)
            os.write(master, b' ')
            self.assertIn(b'PAUSED', collect(.3))
            os.write(master, b' 1')
            for rows, cols in [(20, 50), (24, 80), (45, 160)]:
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
                os.kill(proc.pid, __import__('signal').SIGWINCH)
                collect(.3)
            os.write(master, b'q')
            proc.wait(timeout=3)
            collect(.1)
            self.assertEqual(proc.returncode, 0, output.decode(errors='replace'))
            self.assertNotIn(b'Traceback', output)
            self.assertIn(b'\x1b[?1049l', output)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)

class DockerPolling(unittest.TestCase):
    def monitor(self):
        from sysmon.app import Monitor
        m = object.__new__(Monitor)
        m.containers = [{'Name': 'last-good', 'MemPerc': '5%'}]
        m.docker_updated = 123
        m.docker_status = 'previous sample'
        m.docker_metadata = {}
        m.docker_metadata_updated = None
        return m

    def poll(self, monitor, effects):
        with patch('sysmon.app.subprocess.run', side_effect=effects), patch('sysmon.app.time.sleep', side_effect=StopIteration), patch('sysmon.app.time.monotonic', return_value=456):
            with self.assertRaises(StopIteration):
                monitor.docker_loop()

    def stats(self, rows):
        import subprocess, json
        return subprocess.CompletedProcess([], 0, '\n'.join(json.dumps(r) for r in rows), '')

    def test_timeout_preserves_last_successful_sample(self):
        import subprocess
        m = self.monitor()
        self.poll(m, [subprocess.TimeoutExpired(['docker', 'stats'], 30)])
        self.assertEqual(m.containers[0]['Name'], 'last-good')
        self.assertEqual(m.docker_updated, 123)
        self.assertIn('last sample', m.docker_status)

    def test_optional_metadata_timeout_keeps_fresh_stats(self):
        import subprocess
        m = self.monitor()
        self.poll(m, [self.stats([{'Name': 'fresh', 'MemPerc': '3%'}]),
                      subprocess.TimeoutExpired(['docker', 'ps'], 5)])
        self.assertEqual(m.containers[0]['Name'], 'fresh')
        self.assertGreater(m.docker_updated, 123)

    def test_unavailable_percentage_does_not_drop_other_containers(self):
        m = self.monitor()
        self.poll(m, [self.stats([{'Name': 'running', 'MemPerc': '2%'},
                                 {'Name': 'stopped', 'MemPerc': '--'}]), self.stats([])])
        self.assertEqual({r['Name'] for r in m.containers}, {'running', 'stopped'})

    def test_daemon_error_keeps_data_and_explains_retry(self):
        import subprocess
        m = self.monitor()
        self.poll(m, [subprocess.CompletedProcess([], 1, '', 'Cannot connect to the Docker daemon')])
        self.assertEqual(m.containers[0]['Name'], 'last-good')
        self.assertIn('retrying', m.docker_status)

    def test_successful_empty_response_clears_obsolete_containers(self):
        m = self.monitor()
        self.poll(m, [self.stats([]), self.stats([])])
        self.assertEqual(m.containers, [])
        self.assertEqual(m.docker_updated, 456)
        self.assertIn('0 containers', m.docker_status)

    def test_invalid_response_retains_last_sample(self):
        import subprocess
        m = self.monitor()
        self.poll(m, [subprocess.CompletedProcess([], 0, 'not JSON', '')])
        self.assertEqual(m.containers[0]['Name'], 'last-good')
        self.assertIn('Invalid Docker stats', m.docker_status)

    def test_first_timeout_does_not_claim_cached_data(self):
        import subprocess
        m = self.monitor()
        m.containers, m.docker_updated = [], None
        self.poll(m, [subprocess.TimeoutExpired(['docker', 'stats'], 30)])
        self.assertIn('timed out', m.docker_status)
        self.assertNotIn('last sample', m.docker_status)


if __name__ == '__main__':
    unittest.main()
