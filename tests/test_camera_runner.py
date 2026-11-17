"""Exercise reconnects with real child processes, never a camera or root."""
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class CameraReconnectTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='rs-retry-')
        self.root = Path(self.directory.name)
        self.socket_path = self.root / 'camera.sock'
        self.attempts = self.root / 'attempts.txt'
        self.child = self.root / 'reader.py'
        self.child.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "path = Path(sys.argv[1])\n"
            "attempt = int(path.read_text()) + 1 if path.exists() else 1\n"
            "path.write_text(str(attempt))\n"
            "raise SystemExit(1 if attempt == 1 else 0)\n"
        )
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        self.listener.listen(1)
        self.listener.settimeout(3)
        self.process = None

    def tearDown(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process.communicate(timeout=3)
        self.listener.close()
        self.directory.cleanup()

    def start_supervisor(self, retry_delay):
        self.process = subprocess.Popen(
            [sys.executable, '-c',
             'import sys; from camera_runner import supervise; '
             'raise SystemExit(supervise(sys.argv[1], '
             '[sys.executable, sys.argv[2], sys.argv[3]], float(sys.argv[4])))',
             str(self.socket_path), str(self.child), str(self.attempts), str(retry_delay)],
            cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    @staticmethod
    def read_exact(connection, size):
        data = bytearray()
        while len(data) < size:
            part = connection.recv(size - len(data))
            if not part:
                raise AssertionError('Supervisor disconnected before its status packet completed')
            data.extend(part)
        return bytes(data)

    def read_reconnect(self):
        connection, _ = self.listener.accept()
        with connection:
            connection.settimeout(3)
            header = self.read_exact(connection, 5)
            self.assertEqual(header[:1], b'S')
            payload = self.read_exact(connection, struct.unpack('!I', header[1:])[0])
        status = json.loads(payload)
        self.assertEqual(status['state'], 'reconnecting')
        self.assertEqual(status['fps'], 0)
        self.assertEqual(status['retry_count'], 1)
        return status

    def test_reader_failure_reports_reconnecting_then_starts_a_new_process(self):
        self.start_supervisor(retry_delay=0.05)
        self.read_reconnect()
        stdout, stderr = self.process.communicate(timeout=3)
        self.assertEqual(self.process.returncode, 0, stderr.decode())
        self.assertEqual(self.attempts.read_text(), '2')

    def test_removed_backend_socket_cancels_pending_reconnect(self):
        self.start_supervisor(retry_delay=0.5)
        self.read_reconnect()
        self.socket_path.unlink()
        stdout, stderr = self.process.communicate(timeout=3)
        self.assertEqual(self.process.returncode, 0, stderr.decode())
        self.assertEqual(self.attempts.read_text(), '1')

    def test_termination_interrupts_backoff_without_restarting_reader(self):
        self.start_supervisor(retry_delay=10)
        self.read_reconnect()
        self.process.terminate()
        stdout, stderr = self.process.communicate(timeout=3)
        self.assertEqual(self.process.returncode, 0, stderr.decode())
        self.assertEqual(self.attempts.read_text(), '1')


if __name__ == '__main__':
    unittest.main()
