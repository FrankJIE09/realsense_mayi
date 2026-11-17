#!/usr/bin/env python3
"""Opt-in hardware check: pause/resume the USB reader, then test its restart.

Requires the local preview to be running and sudo on macOS. Never called by
unit tests. Only accepts a PID whose command is this project's USB reader.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
URL = 'http://127.0.0.1:8765'


def status():
    return json.load(urllib.request.urlopen(URL + '/api/status', timeout=3))


def processes():
    rows = subprocess.check_output(['/bin/ps', '-axo', 'pid,ppid,command'], text=True).splitlines()[1:]
    found = {}
    for row in rows:
        pid, parent, command = row.strip().split(None, 2)
        if str(ROOT / 'camera_capture.py') in command:
            found[int(pid)] = int(parent)
    return found


def await_live(min_frames, timeout=40):
    until = time.monotonic() + timeout
    states = set()
    while time.monotonic() < until:
        data = status()
        states.add(data['camera']['state'])
        if data['camera']['is_live'] and data['model']['is_live'] and data['camera']['frames'] > min_frames + 5:
            return data, sorted(states)
        time.sleep(0.2)
    raise RuntimeError(f'Camera did not recover within {timeout}s; states={states}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    args = parser.parse_args()
    readers = processes()
    if args.pid not in readers:
        raise RuntimeError('PID is not this project\'s camera_capture.py')
    parent = readers[args.pid]
    before = status()
    assert before['camera']['is_live'] and before['model']['is_live'], 'Start with live camera and AI'
    os.kill(args.pid, signal.SIGSTOP)
    print('Paused the USB reader; checking stale-frame rejection.', flush=True)
    try:
        time.sleep(4.5)
        stale = status()
        assert not stale['camera']['is_live'] and not stale['model']['is_live']
        assert stale['camera']['fps'] == 0 and stale['model']['fps'] == 0
        assert all(v is None for v in stale['center_depth_m'].values())
        for kind in ['rgb', 'raw', 'ai']:
            try:
                urllib.request.urlopen(URL + '/frame/' + kind + '.jpg', timeout=3)
            except urllib.error.HTTPError as exc:
                assert exc.code == 503
            else:
                raise AssertionError(f'{kind} incorrectly served a stale image')
        print('PASS: all old frames rejected (503), rates zero, distances cleared.', flush=True)
        time.sleep(3.5)
    finally:
        os.kill(args.pid, signal.SIGCONT)
    resumed, states = await_live(before['camera']['frames'])
    print('PASS: fresh camera and AI frames resumed.', flush=True)
    active = [pid for pid, ppid in processes().items() if ppid == parent]
    assert len(active) == 1
    victim = active[0]
    os.kill(victim, signal.SIGKILL)
    recovered, states = await_live(resumed['camera']['frames'])
    replacement = [pid for pid, ppid in processes().items() if ppid == parent]
    assert len(replacement) == 1 and replacement[0] != victim
    print(json.dumps(dict(result='PASS', recovered_reader_pid=replacement[0],
                          observed_states=states, camera_frames=recovered['camera']['frames'],
                          ai_frames=recovered['model']['frames']), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
