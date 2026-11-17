"""Sample macOS GPU utilization without privileges or per-request processes.

IORegistry's Device Utilization % describes the whole GPU, including other
applications. It is not the share attributable to the LingBot process.
"""
import math
import plistlib
import subprocess
import sys
import threading
import time


class GPUMonitor:
    def __init__(self, interval=1.0, stale_after=3.0):
        self.interval = interval
        self.stale_after = stale_after
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._sample_monotonic = None
        self._data = dict(state='starting', utilization_percent=None, name='GPU',
                          scope='system', sampled_at=None, age_s=None, error=None)
        if sys.platform != 'darwin':
            self._data.update(state='unavailable', error='当前平台不支持此 GPU 统计接口')

    @staticmethod
    def read_sample():
        result = subprocess.run(
            ['/usr/sbin/ioreg', '-a', '-r', '-d', '1', '-c', 'IOAccelerator'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2, check=True,
        )
        devices = plistlib.loads(result.stdout)
        if not isinstance(devices, list):
            raise ValueError('GPU 统计格式无效')
        for device in devices:
            if not isinstance(device, dict):
                continue
            statistics = device.get('PerformanceStatistics')
            if not isinstance(statistics, dict):
                continue
            value = statistics.get('Device Utilization %')
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(value) or not 0 <= value <= 100:
                continue
            name = device.get('model') or device.get('IORegistryEntryName') or 'GPU'
            if isinstance(name, bytes):
                name = name.decode('utf-8', errors='replace').rstrip('\x00')
            return str(name), round(float(value), 1)
        raise ValueError('系统未提供有效的 Device Utilization %')

    def sample_once(self):
        """Collect one actual sample; called only by the background sampler."""
        try:
            name, percent = self.read_sample()
            sampled_at, sampled_monotonic = time.time(), time.monotonic()
            with self._lock:
                self._sample_monotonic = sampled_monotonic
                self._data.update(state='ready', utilization_percent=percent,
                                  name=name, sampled_at=sampled_at, error=None)
        except Exception as exc:
            # A missing/unreadable metric is unknown, never an invented 0%.
            with self._lock:
                self._data.update(state='unavailable', utilization_percent=None,
                                  error=f'GPU 统计暂不可用：{type(exc).__name__}: {exc}')

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            self.sample_once()
            self._stop.wait(max(0, self.interval - (time.monotonic() - started)))

    def start(self):
        if sys.platform != 'darwin' or self._stop.is_set():
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='gpu-sampler', daemon=True)
            self._thread.start()

    def snapshot(self):
        """Return cached values only. Expired samples cannot appear current."""
        with self._lock:
            data = dict(self._data)
            age = (time.monotonic() - self._sample_monotonic
                   if self._sample_monotonic is not None else None)
        data['age_s'] = round(max(0, age), 2) if age is not None else None
        if data['state'] == 'ready' and (age is None or age > self.stale_after):
            data.update(state='unavailable', utilization_percent=None, error='GPU 统计已过期')
        return data

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.5)
