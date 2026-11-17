#!/usr/bin/env python3
"""Restart a failed USB reader and report reconnect/crash state to the preview."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import threading
import time


def report(socket_path, message, state='error', **metadata):
    payload = json.dumps(dict(state=state, error=message, fps=0, **metadata)).encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(socket_path)
            connection.sendall(b'S' + struct.pack('!I', len(payload)) + payload)
    except OSError:
        pass
    print(message, file=sys.stderr, flush=True)


def supervise(socket_path, command, retry_delay=2.0):
    child = None
    stopping = threading.Event()

    def stop(_signal, _frame):
        stopping.set()
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    attempts = 0
    native_crashes = 0
    while not stopping.is_set() and Path(socket_path).exists():
        started = time.monotonic()
        child = subprocess.Popen(command)
        code = child.wait()
        if stopping.is_set() or code == 0:
            return 0
        if not Path(socket_path).exists():
            return 0
        if time.monotonic() - started > 30:
            attempts = native_crashes = 0
        attempts += 1
        if code in [-signal.SIGSEGV, -signal.SIGABRT]:
            native_crashes += 1
            if native_crashes >= 3:
                report(socket_path, 'RealSense 原生驱动连续崩溃，自动重连已停止。请检查驱动与 USB 连接。')
                return 128 - code
        delay = min(retry_delay * attempts, 15.0)
        report(socket_path, f'采集已中断，{delay:g} 秒后自动重新连接相机。',
               state='reconnecting', retry_count=attempts)
        stopping.wait(delay)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--fps', type=int, choices=[15, 30], default=30)
    args = parser.parse_args()
    lock_path = str(Path(args.socket).with_suffix('.lock'))
    lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('相机采集已经在运行，请直接查看预览页面。', flush=True)
            return 0
        command = [sys.executable, '-u', str(Path(__file__).with_name('camera_capture.py')),
                   '--socket', args.socket, '--fps', str(args.fps)]
        return supervise(args.socket, command)
    finally:
        os.close(lock)


if __name__ == '__main__':
    sys.exit(main())
