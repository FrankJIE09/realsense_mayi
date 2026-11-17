#!/usr/bin/env python3
"""Local RealSense / LingBot-Depth viewer. Camera and AI run independently."""
import argparse
import io
import json
import math
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from gpu_monitor import GPUMonitor

ROOT = Path(__file__).resolve().parent
NEAR, FAR = 0.2, 4.0


def colored_depth(depth):
    valid = np.isfinite(depth) & (depth > 0)
    values = np.nan_to_num(depth, nan=0, posinf=0, neginf=0)
    scaled = np.clip((values - NEAR) / (FAR - NEAR) * 255, 0, 255).astype(np.uint8)
    image = cv2.applyColorMap(scaled, cv2.COLORMAP_TURBO)
    image[~valid] = 0
    return image


def jpeg(image):
    ok, buf = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 84])
    if not ok:
        raise RuntimeError('JPEG encoding failed')
    return buf.tobytes()


def depth_value(depth, x=0.5, y=0.5):
    if depth is None:
        return None
    h, w = depth.shape
    value = float(depth[min(h-1, int(y*h)), min(w-1, int(x*w))])
    return round(value, 4) if math.isfinite(value) and value > 0 else None


class LiveState:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Condition()
        self.stopped = threading.Event()
        self.gpu = GPUMonitor()
        self.camera = dict(state='waiting', error=None, name='RealSense D435i', fps=0, frames=0)
        self.model = dict(state='loading' if not args.no_ai else 'disabled', error=None,
                          device=None, tokens=args.tokens, inference_ms=None, fps=0, frames=0)
        self.frames = {}
        self.latest = None
        self.ai_depth = None
        self.last_camera = self.last_ai = self.ai_source = None
        self.helper = None
        self.socket_path = ROOT / 'runtime' / 'camera.sock'
        self.socket_path.parent.mkdir(exist_ok=True)
        # An existing app owns the path until its listener stops.
        if self.socket_path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(self.socket_path))
            except OSError:
                self.socket_path.unlink()
            else:
                raise RuntimeError('另一个实时预览正在运行，请先关闭它。')
            finally:
                probe.close()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.listener.listen(2)
        self.listener.settimeout(1)

    def start(self):
        self.gpu.start()
        threading.Thread(target=self.receive_camera, daemon=True).start()
        if not self.args.no_ai:
            threading.Thread(target=self.run_model, daemon=True).start()
        # macOS libusb requires a privileged USB reader. Launch it separately
        # through start-camera.command; keep this HTTP/model process unprivileged.
        if not self.args.external_camera and (sys.platform != 'darwin' or os.geteuid() == 0):
            self.helper = subprocess.Popen([sys.executable, str(ROOT / 'camera_runner.py'),
                                            '--socket', str(self.socket_path)])
            threading.Thread(target=self.watch_helper, daemon=True).start()

    def watch_helper(self):
        code = self.helper.wait()
        with self.lock:
            if code and not self.stopped.is_set() and self.camera['frames'] == 0 and self.camera['state'] != 'error':
                self.camera.update(state='error', error=(
                    f'无法打开 RealSense USB（进程退出码 {code}）。'
                    '请运行 start-camera.command，终端会显示具体原因。'))

    @staticmethod
    def read_exact(conn, size):
        chunks = bytearray()
        while len(chunks) < size:
            part = conn.recv(size - len(chunks))
            if not part:
                raise EOFError('Camera disconnected')
            chunks.extend(part)
        return bytes(chunks)

    def receive_camera(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                with conn:
                    conn.settimeout(20)
                    previous = None
                    while not self.stopped.is_set():
                        header = self.read_exact(conn, 5)
                        size = struct.unpack('!I', header[1:])[0]
                        if size > 5_000_000:
                            raise ValueError('Invalid camera packet size')
                        payload = self.read_exact(conn, size)
                        if header[:1] == b'S':
                            with self.lock:
                                self.camera.update(json.loads(payload))
                                if self.camera['state'] != 'streaming':
                                    self.camera['fps'] = 0
                            continue
                        if header[:1] != b'F':
                            raise ValueError('Invalid camera packet type')
                        with np.load(io.BytesIO(payload), allow_pickle=False) as data:
                            rgb = data['rgb'].copy()
                            depth = data['depth'].astype(np.float32) * float(data['depth_scale'])
                            intrinsics = data['intrinsics'].copy()
                            captured_at = float(data['captured_at'])
                        if rgb.shape != (480, 640, 3) or depth.shape != (480, 640):
                            raise ValueError('Unexpected camera frame dimensions')
                        frames = {'rgb': jpeg(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)),
                                  'raw': jpeg(colored_depth(depth))}
                        now = time.time()
                        with self.lock:
                            count = self.camera['frames'] + 1
                            self.latest = (count, rgb, depth, intrinsics, captured_at)
                            self.frames.update(frames)
                            self.last_camera = now
                            if previous is not None:
                                fps = 1 / max(now - previous, 1e-6)
                                self.camera['fps'] = self.camera['fps'] * 0.9 + fps * 0.1
                            self.camera.update(state='streaming', error=None, frames=count)
                            self.lock.notify_all()
                        previous = now
            except Exception as exc:
                with self.lock:
                    if self.camera['state'] not in ('error', 'reconnecting'):
                        self.camera.update(state='disconnected', error=str(exc), fps=0)
                    else:
                        self.camera['fps'] = 0

    def run_model(self):
        try:
            from lingbot_worker import LingBotProcessor
            processor = LingBotProcessor(model_path=self.args.model, device=self.args.device,
                                         num_tokens=self.args.tokens)
            with self.lock:
                self.model.update(state='waiting', device=str(processor.device))
            seen = -1
            while not self.stopped.is_set():
                with self.lock:
                    self.lock.wait_for(lambda: self.stopped.is_set() or
                                       (self.latest is not None and self.latest[0] != seen), timeout=1)
                    if self.stopped.is_set():
                        break
                    if self.latest is None or self.latest[0] == seen:
                        continue
                    seen, rgb, depth, intrinsics, captured_at = self.latest
                    if not self._freshness(time.time())[0]:
                        self.model.update(state='waiting_camera', fps=0)
                        continue
                    self.model['state'] = 'processing'
                    processor.num_tokens = self.args.tokens
                began = time.perf_counter()
                if not np.any(depth > 0):
                    with self.lock:
                        self.model.update(state='waiting_depth', error='当前帧无有效深度，等待下一帧')
                    continue
                result = processor.process(rgb, depth, intrinsics)
                refined = result['depth_m']
                image = jpeg(colored_depth(refined))
                duration = time.perf_counter() - began
                with self.lock:
                    self.ai_depth = refined
                    self.frames['ai'] = image
                    self.last_ai = time.time()
                    self.ai_source = captured_at
                    self.model.update(result['stats'])
                    self.model.update(state='streaming', frames=self.model['frames'] + 1,
                                      fps=1 / duration, source_frame=seen, error=None)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            with self.lock:
                self.model.update(state='error', error=f'{type(exc).__name__}: {exc}')

    def _freshness(self, now):
        """Return stream validity while holding self.lock; cached bytes alone aren't live."""
        camera_live = bool(
            self.camera['state'] == 'streaming' and self.latest is not None
            and self.last_camera is not None
            and 0 <= now - self.last_camera <= 3
            and 0 <= now - self.latest[4] <= 3
        )
        inference_ms = self.model.get('inference_ms')
        latency = float(inference_ms) / 1000 if inference_ms is not None else 0
        if not math.isfinite(latency) or latency < 0:
            latency = 0
        # Keep a previous AI result visible while a slow CPU inference runs,
        # but expire it if processing stops or its camera input disappears.
        ai_limit = max(3.0, 2 * latency + 1)
        model_live = bool(
            camera_live and self.model['state'] in ('processing', 'streaming')
            and self.last_ai is not None and self.ai_source is not None
            and 0 <= now - self.last_ai <= ai_limit
            and 0 <= now - self.ai_source <= ai_limit
        )
        return camera_live, model_live, ai_limit

    def pixel(self, x=0.5, y=0.5):
        now = time.time()
        with self.lock:
            camera_live, model_live, _ = self._freshness(now)
            return dict(
                raw_m=depth_value(self.latest[2], x, y) if camera_live else None,
                ai_m=depth_value(self.ai_depth, x, y) if model_live else None,
                ai_age_s=now-self.ai_source if self.ai_source is not None else None,
                raw_is_live=camera_live, ai_is_live=model_live,
            )

    def frame(self, kind):
        now = time.time()
        with self.lock:
            camera_live, model_live, _ = self._freshness(now)
            live = model_live if kind == 'ai' else camera_live
            frame = self.frames.get(kind) if live else None
            if frame is None:
                return None, {'X-Stream-Live': '0', 'Retry-After': '1'}
            source_id = self.model.get('source_frame') if kind == 'ai' else self.latest[0]
            frame_id = self.model['frames'] if kind == 'ai' else self.latest[0]
            captured_at = self.ai_source if kind == 'ai' else self.latest[4]
            return frame, {
                'X-Stream-Live': '1', 'X-Frame-Id': str(frame_id),
                'X-Source-Frame-Id': str(source_id),
                'X-Captured-At': f'{captured_at:.6f}',
                'X-Frame-Age-S': f'{max(0, now-captured_at):.3f}',
            }

    def status(self):
        now = time.time()
        with self.lock:
            camera, model = dict(self.camera), dict(self.model)
            camera_live, model_live, ai_limit = self._freshness(now)
            model['tokens'] = self.args.tokens
            camera['is_live'] = camera_live
            camera['fps'] = round(camera['fps'], 1) if camera_live else 0
            camera['last_frame_age_s'] = round(now-self.last_camera, 2) if self.last_camera else None
            camera['captured_at'] = self.latest[4] if self.latest else None
            camera['source_age_s'] = round(now-self.latest[4], 2) if self.latest else None
            model['is_live'] = model_live
            model['stale_after_s'] = round(ai_limit, 2)
            model['last_result_age_s'] = round(now-self.last_ai, 2) if self.last_ai else None
            model['source_age_s'] = round(now-self.ai_source, 2) if self.ai_source else None
            model['fps'] = round(model['fps'], 2) if model_live else 0
            if not camera_live and self.last_camera and camera['state'] == 'streaming':
                camera.update(state='stale', error='超过 3 秒没有收到新画面', fps=0)
            if model['state'] not in ('loading', 'disabled', 'error'):
                if not camera_live:
                    model.update(state='waiting_camera', error=None)
                elif not model_live and self.last_ai is not None and model['state'] != 'waiting_depth':
                    model.update(state='stale', error='AI 结果已过期，等待新结果')
            return dict(camera=camera, model=model, gpu=self.gpu.snapshot(), depth_range_m=[NEAR, FAR],
                        center_depth_m=dict(raw=depth_value(self.latest[2]) if camera_live else None,
                                            ai=depth_value(self.ai_depth) if model_live else None))

    def close(self):
        self.stopped.set()
        self.gpu.close()
        with self.lock:
            self.lock.notify_all()
        self.listener.close()
        if self.helper and self.helper.poll() is None:
            self.helper.terminate()
        self.socket_path.unlink(missing_ok=True)


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, data, mime, status=200, headers=None):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            uri = urlparse(self.path)
            if uri.path == '/':
                self.reply((ROOT / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            elif uri.path == '/api/status':
                self.reply(json.dumps(state.status()).encode(), 'application/json')
            elif uri.path == '/api/pixel':
                try:
                    query = parse_qs(uri.query)
                    x, y = float(query['x'][0]), float(query['y'][0])
                    if not (0 <= x <= 1 and 0 <= y <= 1):
                        raise ValueError()
                except (KeyError, ValueError):
                    self.reply(b'Invalid coordinates', 'text/plain', 400)
                    return
                values = state.pixel(x, y)
                self.reply(json.dumps(values).encode(), 'application/json')
            elif uri.path in ['/frame/rgb.jpg', '/frame/raw.jpg', '/frame/ai.jpg']:
                key = uri.path.split('/')[-1].split('.')[0]
                frame, headers = state.frame(key)
                if frame:
                    self.reply(frame, 'image/jpeg', headers=headers)
                else:
                    self.reply(b'Waiting for fresh live frame', 'text/plain', 503, headers=headers)
            else:
                self.reply(b'Not found', 'text/plain', 404)

        def do_POST(self):
            if self.path != '/api/config':
                self.reply(b'Not found', 'text/plain', 404)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length < 1024 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('Expected JSON')
                config = json.loads(self.rfile.read(length))
                tokens = config['tokens']
                if type(tokens) is not int or tokens not in [256, 512, 1200]:
                    raise ValueError('Unsupported mode')
            except (ValueError, KeyError, TypeError):
                self.reply(b'Invalid mode', 'text/plain', 400)
                return
            with state.lock:
                state.args.tokens = tokens
            self.reply(json.dumps(dict(tokens=tokens)).encode(), 'application/json')
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--tokens', type=int, default=256,
                        help='256 fast preview, 512 balanced, 1200 detail (official minimum)')
    parser.add_argument('--device', choices=['auto', 'mps', 'cpu', 'cuda'], default='auto')
    parser.add_argument('--model', default=str(ROOT / 'models/lingbot-depth-v0.5/model.pt'))
    parser.add_argument('--external-camera', action='store_true')
    parser.add_argument('--no-ai', action='store_true')
    parser.add_argument('--open', action='store_true', help='Open the local preview in the default browser')
    args = parser.parse_args()
    if args.tokens < 64:
        parser.error('--tokens must be at least 64')
    state = LiveState(args)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(state))
    state.start()
    print(f'实时预览 http://127.0.0.1:{args.port}', flush=True)
    print('若 macOS USB 权限不足，请运行 start-camera.command', flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{args.port}')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.close()
        server.server_close()


if __name__ == '__main__':
    main()
