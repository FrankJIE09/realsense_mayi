#!/usr/bin/env python3
"""Small RealSense USB reader. On macOS only this process needs sudo.

Sends aligned RGB + raw depth to the local, unprivileged app via a Unix socket.
No network listener, model loading, recording, or firmware changes here.
"""
import argparse
import io
import json
import os
import signal
import socket
import struct
import sys
import threading
import time

import numpy as np
import pyrealsense2 as rs


def send(sock, kind, payload):
    sock.sendall(kind + struct.pack('!I', len(payload)) + payload)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--socket', required=True)
    p.add_argument('--fps', type=int, default=30, choices=[15, 30])
    args = p.parse_args()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(args.socket)
    pipeline = rs.pipeline()
    started = False

    def status(**kw):
        print(json.dumps(kw, ensure_ascii=False), flush=True)
        send(sock, b'S', json.dumps(kw).encode())

    def stop(_sig, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        status(state='connecting', error=None)
        config = rs.config()
        config.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, args.fps)
        config.enable_stream(rs.stream.color, 640, 480, rs.format.rgb8, args.fps)
        profile = pipeline.start(config)
        started = True
        device = profile.get_device()
        depth_scale = device.first_depth_sensor().get_depth_scale()
        align = rs.align(rs.stream.color)
        status(state='streaming', name=device.get_info(rs.camera_info.name),
               serial=device.get_info(rs.camera_info.serial_number),
               depth_scale=depth_scale, error=None)
        while True:
            # A timeout restarts the pipeline via the supervisor. Continuing
            # to wait on a stalled RSUSB pipeline leaves it stuck indefinitely.
            frames = pipeline.wait_for_frames(5000)
            frames = align.process(frames)
            depth, color = frames.get_depth_frame(), frames.get_color_frame()
            if not depth or not color:
                continue
            captured_at = time.time()
            intr = color.profile.as_video_stream_profile().get_intrinsics()
            k = np.array([[intr.fx / intr.width, 0, intr.ppx / intr.width],
                          [0, intr.fy / intr.height, intr.ppy / intr.height],
                          [0, 0, 1]], dtype=np.float32)
            data = io.BytesIO()
            np.savez(data, rgb=np.asanyarray(color.get_data()),
                     depth=np.asanyarray(depth.get_data()), intrinsics=k,
                     depth_scale=np.float32(depth_scale), captured_at=captured_at)
            send(sock, b'F', data.getvalue())
    except (KeyboardInterrupt, BrokenPipeError, ConnectionResetError):
        pass
    except Exception as exc:
        try:
            status(state='error', error=str(exc))
        except OSError:
            pass
        return 1
    finally:
        if started:
            # Some USB disconnects hang native shutdown. Bound cleanup so the
            # supervisor can reopen the device instead of leaving a dead reader.
            cleanup = threading.Thread(target=pipeline.stop, daemon=True)
            cleanup.start()
            cleanup.join(timeout=3)
            if cleanup.is_alive():
                print('RealSense USB shutdown timed out; restarting reader.', file=sys.stderr, flush=True)
                os._exit(2)
        sock.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
