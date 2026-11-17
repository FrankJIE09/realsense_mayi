"""Camera-independent integration checks for the local RGB-D transport.

No camera is opened and no model is loaded. Packets follow the exact schema sent
by the hardware helper, including native depth units and calibrated intrinsics.
"""
import io
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request

import cv2
import numpy as np

import app
from lingbot_worker import LingBotProcessor


class PipelineTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="rgbd-test-")
        self.original_root = app.ROOT
        app.ROOT = Path(self.directory.name)
        self.state = app.LiveState(SimpleNamespace(no_ai=True, tokens=1200, external_camera=True))
        self.receiver = threading.Thread(target=self.state.receive_camera, daemon=True)
        self.receiver.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.state.close()
        self.receiver.join(timeout=2)
        app.ROOT = self.original_root
        self.directory.cleanup()

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(str(self.state.socket_path))
        self.clients.append(client)
        return client

    def wait_until(self, predicate, message):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(message)

    def send_frame(self, client, raw_units=1250, scale=0.001):
        # A red RGB frame catches accidental RGB/BGR swaps in browser JPEGs.
        rgb = np.zeros((480, 640, 3), dtype=np.uint8)
        rgb[:, :, 0] = 255
        depth = np.full((480, 640), raw_units, dtype=np.uint16)
        depth[0, 0] = 0
        intrinsic = np.array([[610 / 640, 0, 321 / 640],
                              [0, 612 / 480, 241 / 480],
                              [0, 0, 1]], dtype=np.float32)
        buffer = io.BytesIO()
        np.savez(buffer, rgb=rgb, depth=depth, intrinsics=intrinsic,
                 depth_scale=np.float32(scale), captured_at=time.time())
        payload = buffer.getvalue()
        header = b"F" + struct.pack("!I", len(payload))
        # Unix streams can split an application header across multiple reads.
        client.sendall(header[:2])
        client.sendall(header[2:] + payload)
        return rgb, depth, intrinsic

    def test_sensor_units_alignment_and_rgb_survive_transport(self):
        rgb, raw, intrinsics = self.send_frame(self.connect())
        self.wait_until(lambda: self.state.latest is not None, "No frame received")
        _, received_rgb, depth_m, received_k, _ = self.state.latest
        np.testing.assert_array_equal(received_rgb, rgb)
        np.testing.assert_allclose(received_k, intrinsics)
        self.assertAlmostEqual(float(depth_m[240, 320]), 1.25, places=5)
        self.assertEqual(float(depth_m[0, 0]), 0)
        self.assertEqual(raw[240, 320], 1250)
        decoded_bgr = cv2.imdecode(np.frombuffer(self.state.frames["rgb"], np.uint8), cv2.IMREAD_COLOR)
        self.assertGreater(int(decoded_bgr[240, 320, 2]), 245)
        self.assertLess(int(decoded_bgr[240, 320, 0]), 10)
        self.assertEqual(self.state.status()["center_depth_m"]["raw"], 1.25)

    def test_reconnect_after_malformed_camera_packet(self):
        broken = self.connect()
        broken.sendall(b"F" + struct.pack("!I", 5_000_001))
        self.wait_until(lambda: self.state.camera["state"] == "disconnected",
                        "Invalid packet was not rejected")
        broken.close()
        self.send_frame(self.connect(), raw_units=2000)
        self.wait_until(lambda: self.state.camera["state"] == "streaming",
                        "Receiver did not recover on a new helper connection")
        self.assertEqual(self.state.status()["center_depth_m"]["raw"], 2.0)

    def test_driver_crash_reason_survives_supervisor_disconnect(self):
        client = self.connect()
        message = 'RealSense 原生驱动崩溃（SIGSEGV）'
        payload = json.dumps(dict(state='error', error=message, fps=0)).encode()
        client.sendall(b'S' + struct.pack('!I', len(payload)) + payload)
        client.close()
        self.wait_until(lambda: self.state.camera.get('error') == message,
                        'The driver crash was not reported')
        # Receiving a new connection proves the old socket's EOF was handled.
        next_client = self.connect()
        payload = json.dumps(dict(supervisor_checked=True)).encode()
        next_client.sendall(b'S' + struct.pack('!I', len(payload)) + payload)
        self.wait_until(lambda: self.state.camera.get('supervisor_checked'),
                        'Receiver did not handle the next connection')
        status = self.state.status()['camera']
        self.assertEqual(status['state'], 'error')
        self.assertEqual(status['error'], message)

    def test_pixel_http_returns_meters_and_rejects_nonfinite_coordinates(self):
        self.send_frame(self.connect())
        self.wait_until(lambda: self.state.latest is not None, "No frame received")
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(base + "/api/pixel?x=0.5&y=0.5", timeout=2) as response:
                values = json.load(response)
            self.assertEqual(values["raw_m"], 1.25)
            self.assertIsNone(values["ai_m"])
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + "/api/pixel?x=nan&y=0.5", timeout=2)
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_reconnect_notice_survives_short_lived_status_connection(self):
        client = self.connect()
        message = '采集已中断，2秒后自动重新连接相机。'
        payload = json.dumps(dict(state='reconnecting', error=message, fps=0)).encode()
        client.sendall(b'S' + struct.pack('!I', len(payload)) + payload)
        client.close()
        self.wait_until(lambda: self.state.camera['state'] == 'reconnecting', 'Notice missing')
        next_client = self.connect()
        marker = json.dumps(dict(next_connection=True)).encode()
        next_client.sendall(b'S' + struct.pack('!I', len(marker)) + marker)
        self.wait_until(lambda: self.state.camera.get('next_connection'), 'EOF not handled')
        status = self.state.status()['camera']
        self.assertEqual(status['state'], 'reconnecting')
        self.assertEqual(status['error'], message)
        self.assertFalse(status['is_live'])

    def test_stopped_camera_cannot_serve_cached_images_or_depth_as_live(self):
        client = self.connect()
        self.send_frame(client)
        self.wait_until(lambda: self.state.latest is not None, 'No frame received')
        with self.state.lock:
            self.state.camera['fps'] = 30
            self.state.model.update(state='streaming', fps=4, frames=1,
                                    source_frame=1, inference_ms=250)
            self.state.ai_depth = self.state.latest[2].copy()
            self.state.frames['ai'] = self.state.frames['raw']
            self.state.ai_source = self.state.latest[4]
            self.state.last_ai = time.time()
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.make_handler(self.state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            for kind in ('rgb', 'raw', 'ai'):
                with urllib.request.urlopen(base + f'/frame/{kind}.jpg', timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers['X-Stream-Live'], '1')
                    self.assertEqual(response.headers['X-Frame-Id'], '1')
                    self.assertGreater(float(response.headers['X-Captured-At']), 0)
            payload = json.dumps(dict(state='error', error="Frame didn't arrive within 5000")).encode()
            client.sendall(b'S' + struct.pack('!I', len(payload)) + payload)
            self.wait_until(lambda: self.state.camera['state'] == 'error', 'Timeout not received')
            # Bytes remain cached, reproducing the original frozen-image bug.
            self.assertEqual(set(self.state.frames), {'rgb', 'raw', 'ai'})
            with urllib.request.urlopen(base + '/api/status', timeout=2) as response:
                status = json.load(response)
            self.assertFalse(status['camera']['is_live'])
            self.assertFalse(status['model']['is_live'])
            self.assertEqual(status['camera']['fps'], 0)
            self.assertEqual(status['model']['fps'], 0)
            self.assertEqual(status['model']['state'], 'waiting_camera')
            self.assertEqual(status['center_depth_m'], {'raw': None, 'ai': None})
            with urllib.request.urlopen(base + '/api/pixel?x=0.5&y=0.5', timeout=2) as response:
                values = json.load(response)
            self.assertIsNone(values['raw_m'])
            self.assertIsNone(values['ai_m'])
            for kind in ('rgb', 'raw', 'ai'):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(base + f'/frame/{kind}.jpg', timeout=2)
                self.assertEqual(error.exception.code, 503)
                self.assertEqual(error.exception.headers['X-Stream-Live'], '0')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_old_capture_is_not_live_even_if_received_recently(self):
        self.send_frame(self.connect())
        self.wait_until(lambda: self.state.latest is not None, 'No frame received')
        with self.state.lock:
            count, rgb, depth, k, _ = self.state.latest
            self.state.latest = count, rgb, depth, k, time.time() - 20
            self.state.last_camera = time.time()
        status = self.state.status()
        self.assertFalse(status['camera']['is_live'])
        self.assertEqual(status['camera']['state'], 'stale')
        self.assertEqual(status['model']['state'], 'disabled')
        self.assertIsNone(self.state.frame('rgb')[0])

    def test_ai_expiry_allows_slow_inference_but_rejects_old_results(self):
        self.send_frame(self.connect())
        self.wait_until(lambda: self.state.latest is not None, 'No frame received')
        with self.state.lock:
            self.state.model.update(state='processing', fps=0.25, frames=3,
                                    source_frame=1, inference_ms=4000)
            self.state.ai_depth = self.state.latest[2].copy()
            self.state.frames['ai'] = self.state.frames['raw']
            self.state.ai_source = time.time() - 8
            self.state.last_ai = time.time() - 4
        status = self.state.status()
        self.assertTrue(status['camera']['is_live'])
        self.assertTrue(status['model']['is_live'])
        self.assertEqual(status['model']['stale_after_s'], 9)
        self.assertIsNotNone(self.state.frame('ai')[0])
        with self.state.lock:
            self.state.ai_source = time.time() - 10
        status = self.state.status()
        self.assertTrue(status['camera']['is_live'])
        self.assertFalse(status['model']['is_live'])
        self.assertEqual(status['model']['state'], 'stale')
        self.assertEqual(status['model']['fps'], 0)
        self.assertIsNone(self.state.pixel()['ai_m'])
        self.assertIsNotNone(self.state.pixel()['raw_m'])
        self.assertIsNone(self.state.frame('ai')[0])


class ModelInputContractTests(unittest.TestCase):
    def test_invalid_measurements_are_removed_without_modifying_capture(self):
        rgb = np.zeros((4, 6, 3), dtype=np.uint8)
        depth = np.ones((4, 6), dtype=np.float32)
        depth[0, :3] = [np.nan, np.inf, -1]
        _, cleaned, _ = LingBotProcessor._validate_inputs(rgb, depth, np.eye(3))
        np.testing.assert_array_equal(cleaned[0, :3], [0, 0, 0])
        self.assertTrue(np.isnan(depth[0, 0]))
        self.assertTrue(np.isinf(depth[0, 1]))
        self.assertEqual(depth[0, 2], -1)
        with self.assertRaises(ValueError):
            LingBotProcessor._validate_inputs(rgb, np.zeros_like(depth), None)
        with self.assertRaises(ValueError):
            LingBotProcessor._validate_inputs(rgb, np.ones_like(depth, dtype=np.uint16), None)


if __name__ == "__main__":
    unittest.main()
