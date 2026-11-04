import os
import sys
import time
import unittest
from sysmon.app import memory_bytes, percent, bytesize


class Metrics(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
