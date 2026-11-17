"""LingBot-Depth inference adapter for aligned RealSense RGB-D frames.

The caller owns camera capture and should run this processor in a worker thread,
keeping only the newest pending frame. This module never substitutes a filter or
raw sensor depth for a failed neural-network prediction.

CLI input is an NPZ containing ``rgb`` (uint8 HWC, RGB order), ``depth_m``
(float32 HW, meters), and optional ``intrinsics_normalized`` (float32 3x3).
Camera intrinsics must describe the RGB image after depth-to-color alignment.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any

import numpy as np


DEFAULT_MODEL = "robbyant/lingbot-depth-pretrain-vitl-14-v0.5"
LOGGER = logging.getLogger(__name__)


class LingBotProcessor:
    """Load once and process frames serially; each instance has one model.

    On Apple Silicon ``auto`` prefers MPS. An MPS initialization or inference
    error falls back to CPU and is exposed in ``stats['fallback_reason']``.
    FP32 and PyTorch SDPA avoid the upstream CUDA/xFormers dependency. Disabling
    depth-token masking uses the upstream dense-token path, which may differ
    from the default CUDA model on missing-depth regions.
    """

    def __init__(
        self,
        model_path: str | Path = DEFAULT_MODEL,
        device: str = "auto",
        num_tokens: int = 1200,
        vendor_path: str | Path | None = None,
    ) -> None:
        if device not in {"auto", "mps", "cpu", "cuda"}:
            raise ValueError("device must be auto, mps, cpu, or cuda")
        if num_tokens < 16:
            raise ValueError("num_tokens must be at least 16")

        # This must be set before importing the upstream DINOv2 modules.
        os.environ["XFORMERS_DISABLED"] = "1"
        # PyTorch 2.6 lacks MPS antialiased bilinear resize. Let that one
        # operation run on CPU while the transformer remains on the GPU.
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        import torch

        source = (
            Path(vendor_path)
            if vendor_path is not None
            else Path(__file__).resolve().parent / "vendor" / "lingbot-depth"
        )
        if source.is_dir() and str(source.resolve()) not in sys.path:
            sys.path.insert(0, str(source.resolve()))
        try:
            from mdm.model.v2 import MDMModel
        except ImportError as error:
            raise RuntimeError(
                "LingBot-Depth is unavailable. Place the official repository at "
                f"{source} and install its Python runtime dependencies."
            ) from error

        self.torch = torch
        self.num_tokens = int(num_tokens)
        self.model_path = str(model_path)
        self.requested_device = device
        self.fallback_reason: str | None = None
        if device == "auto":
            device = (
                "mps" if torch.backends.mps.is_available()
                else "cuda" if torch.cuda.is_available()
                else "cpu"
            )
        if device == "mps" and not torch.backends.mps.is_available():
            self.fallback_reason = "MPS is unavailable; using CPU."
            LOGGER.warning(self.fallback_reason)
            device = "cpu"
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested, but no CUDA device is available.")
        self.device = torch.device(device)

        start = time.perf_counter()
        LOGGER.info("Loading LingBot-Depth %s on %s", self.model_path, self.device)
        self.model = MDMModel.from_pretrained(self.model_path).eval()
        self.model.enable_pytorch_native_sdpa()
        try:
            self.model.to(device=self.device, dtype=torch.float32)
        except RuntimeError as error:
            if self.device.type != "mps":
                raise
            self._fallback_to_cpu(error)
        self.load_ms = (time.perf_counter() - start) * 1000.0

    def _fallback_to_cpu(self, error: Exception) -> None:
        self.fallback_reason = f"MPS failed; using CPU: {type(error).__name__}: {error}"
        LOGGER.warning(self.fallback_reason)
        self.model.to(device="cpu", dtype=self.torch.float32)
        self.device = self.torch.device("cpu")
        # Best effort: a broken MPS command buffer must not prevent CPU use.
        try:
            self.torch.mps.empty_cache()
        except RuntimeError:
            pass

    def _synchronize(self) -> None:
        if self.device.type == "mps":
            self.torch.mps.synchronize()
        elif self.device.type == "cuda":
            self.torch.cuda.synchronize(self.device)

    @staticmethod
    def _validate_inputs(
        rgb: np.ndarray,
        depth_m: np.ndarray,
        intrinsics_normalized: np.ndarray | None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
        rgb = np.asarray(rgb)
        depth_m = np.asarray(depth_m)
        if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
            raise ValueError("rgb must be a uint8 HxWx3 image in RGB order")
        if depth_m.shape != rgb.shape[:2] or not np.issubdtype(depth_m.dtype, np.floating):
            raise ValueError("depth_m must be floating-point HxW meters, aligned to rgb")
        if min(rgb.shape[:2]) < 2:
            raise ValueError("RGB-D images must be at least 2x2 pixels")
        depth_m = np.array(depth_m, dtype=np.float32, order="C", copy=True)
        depth_m[~np.isfinite(depth_m) | (depth_m <= 0)] = 0
        if not np.any(depth_m > 0):
            raise ValueError("The frame has no valid measured depth")
        intrinsics = None
        if intrinsics_normalized is not None:
            intrinsics = np.asarray(intrinsics_normalized, dtype=np.float32)
            if intrinsics.shape != (3, 3) or not np.isfinite(intrinsics).all():
                raise ValueError("intrinsics_normalized must be a finite 3x3 matrix")
            if intrinsics[0, 0] <= 0 or intrinsics[1, 1] <= 0:
                raise ValueError("Normalized focal lengths must be positive")
            if not np.allclose(intrinsics[2], [0, 0, 1], atol=1e-6):
                raise ValueError("The final intrinsics row must be [0, 0, 1]")
            intrinsics = np.ascontiguousarray(intrinsics)
        return np.ascontiguousarray(rgb), depth_m, intrinsics

    def _infer_once(
        self,
        rgb: np.ndarray,
        depth_m: np.ndarray,
        intrinsics_normalized: np.ndarray | None,
    ) -> dict[str, Any]:
        torch = self.torch
        self._synchronize()
        start = time.perf_counter()
        image = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0)
        image = image.to(device=self.device, dtype=torch.float32).div_(255.0)
        depth = torch.from_numpy(depth_m).to(self.device).unsqueeze(0)
        intrinsics = (
            torch.from_numpy(intrinsics_normalized).to(self.device).unsqueeze(0)
            if intrinsics_normalized is not None else None
        )
        with torch.inference_mode():
            output = self.model.infer(
                image,
                depth_in=depth,
                intrinsics=intrinsics,
                num_tokens=self.num_tokens,
                use_fp16=False,
                enable_depth_mask=False,
            )
        predicted = output["depth"].squeeze(0).float().cpu().numpy()
        if predicted.shape != depth_m.shape:
            raise RuntimeError(f"Unexpected LingBot output shape {predicted.shape}")
        valid = np.isfinite(predicted) & (predicted > 0)
        if "mask" in output:
            valid &= output["mask"].squeeze(0).cpu().numpy().astype(bool)
        if not valid.any():
            raise RuntimeError("LingBot-Depth returned no finite positive depth values")
        # Match the RealSense convention: zero means invalid or model-masked.
        predicted = np.where(valid, predicted, 0).astype(np.float32)
        self._synchronize()
        inference_ms = (time.perf_counter() - start) * 1000.0
        return {
            "depth_m": predicted,
            "mask": valid,
            "stats": {
                "model": self.model_path,
                "device": str(self.device),
                "requested_device": self.requested_device,
                "tokens": self.num_tokens,
                "inference_ms": inference_ms,
                "inference_fps": 1000.0 / max(inference_ms, 1e-9),
                "load_ms": self.load_ms,
                "valid_fraction": float(valid.mean()),
                "input_valid_fraction": float((depth_m > 0).mean()),
                "fallback_reason": self.fallback_reason,
                "depth_token_masking": False,
                "mps_op_cpu_fallback_enabled": os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK") == "1",
            },
        }

    def process(
        self,
        rgb: np.ndarray,
        depth_m: np.ndarray,
        intrinsics_normalized: np.ndarray | None = None,
    ) -> dict[str, Any]:
        """Return model depth (meters), valid mask, and measured inference stats.

        Pass ``K[0, :] /= width`` and ``K[1, :] /= height`` before supplying
        intrinsics. Depth must first be aligned to RGB using RealSense rs.align.
        Input arrays are not changed. Do not call concurrently on one instance.
        """
        rgb, depth_m, intrinsics = self._validate_inputs(rgb, depth_m, intrinsics_normalized)
        start = time.perf_counter()
        try:
            result = self._infer_once(rgb, depth_m, intrinsics)
        except (RuntimeError, NotImplementedError) as error:
            if self.device.type != "mps":
                raise
            self._fallback_to_cpu(error)
            result = self._infer_once(rgb, depth_m, intrinsics)
        result["stats"]["total_ms"] = (time.perf_counter() - start) * 1000.0
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Input RGB-D NPZ")
    parser.add_argument("--output", type=Path, required=True, help="Output prediction NPZ")
    parser.add_argument("--model", "--model-path", default=DEFAULT_MODEL)
    parser.add_argument("--device", choices=["auto", "mps", "cpu", "cuda"], default="auto")
    parser.add_argument("--tokens", type=int, default=1200, help="Encoder token budget (default: 1200)")
    parser.add_argument("--runs", type=int, default=1, help="Timed inference repetitions")
    parser.add_argument("--warmup", type=int, default=0, help="Untimed warm-up repetitions")
    args = parser.parse_args()
    if args.runs < 1 or args.warmup < 0:
        parser.error("--runs must be positive and --warmup must be nonnegative")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        with np.load(args.input, allow_pickle=False) as source:
            rgb, depth_m = source["rgb"], source["depth_m"]
            intrinsics = source["intrinsics_normalized"] if "intrinsics_normalized" in source else None
        processor = LingBotProcessor(args.model, args.device, args.tokens)
        for _ in range(args.warmup):
            processor.process(rgb, depth_m, intrinsics)
        timings = []
        for _ in range(args.runs):
            result = processor.process(rgb, depth_m, intrinsics)
            timings.append(result["stats"]["inference_ms"])
        stats = result["stats"]
        stats["benchmark"] = {
            "runs": args.runs,
            "warmup": args.warmup,
            "timings_ms": timings,
            "mean_ms": float(np.mean(timings)),
            "median_ms": float(np.median(timings)),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        arrays = {
            "rgb": rgb,
            "depth_input_m": depth_m,
            "depth_m": result["depth_m"],
            "mask": result["mask"],
            "stats_json": np.array(json.dumps(stats)),
        }
        if intrinsics is not None:
            arrays["intrinsics_normalized"] = intrinsics
        with args.output.open("wb") as stream:
            np.savez_compressed(stream, **arrays)
        args.output.with_suffix(".json").write_text(json.dumps(stats, indent=2) + "\n")
        print(json.dumps(stats, indent=2))
        return 0
    except Exception:
        LOGGER.exception("LingBot inference failed; no substitute prediction was produced")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
