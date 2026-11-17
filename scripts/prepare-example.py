#!/usr/bin/env python3
"""Prepare the upstream static example for the offline LingBot benchmark."""
import argparse
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'output/official-example-input.npz')
    args = parser.parse_args()
    source = ROOT / 'vendor/lingbot-depth/examples/0'
    color = cv2.imread(str(source / 'rgb.png'))
    depth = cv2.imread(str(source / 'raw_depth.png'), cv2.IMREAD_UNCHANGED)
    if color is None or depth is None:
        raise RuntimeError('Missing official example; run git submodule update --init --recursive')
    rgb = cv2.cvtColor(color, cv2.COLOR_BGR2RGB)
    depth_m = depth.astype(np.float32) / 1000.0
    intrinsics = np.loadtxt(source / 'intrinsics.txt', dtype=np.float32)
    h, w = depth.shape
    intrinsics[0, :] /= w
    intrinsics[1, :] /= h
    args.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez(args.output, rgb=rgb, depth_m=depth_m,
             intrinsics_normalized=intrinsics)
    print(f'Prepared official static example: {args.output} ({w}x{h})')


if __name__ == '__main__':
    main()
