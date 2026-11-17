# Local RealSense macOS wheel build

Built on 2026-11-17 for the attached D435i, MacBook Air M4, macOS 15 / arm64, Python 3.12.14. No root commands and no system installation were used.

## Result

`build/wheels/pyrealsense2_macosx-2.59.2-cp312-cp312-macosx_15_0_arm64.whl`

The wheel bundles librealsense **2.59.2** and libusb **1.0.29**. It does not require Homebrew or the build directories at runtime. `delocate-wheel` copied both shared libraries into `pyrealsense2/.dylibs/` and rewrote references to use `@loader_path`.

Source: official tag `v2.59.2`, commit `e535e2445cfd00c31f67761ec13e0f99cb31c2dc`, <https://github.com/realsenseai/librealsense>.

libusb source: <https://github.com/libusb/libusb/releases/download/v1.0.29/libusb-1.0.29.tar.bz2>.

## Rebuild

From the project root, run:

```bash
bash scripts/build-realsense-macos.sh
```

The script records the commands used for this successful build. CMake/Ninja were installed in `.venv`; libusb's release tarball contains its generated configure script, so Homebrew/autoconf were unnecessary. libusb was configured with a project-local prefix and built with `make -j6`. librealsense was configured with the Ninja generator, Apple Silicon only, macOS deployment target 15.0, Python bindings enabled, RSUSB backend enabled and HWM-over-XU disabled. GUI, tools, tests, DDS, OpenMP, legacy pybackend, update checks, AI assistant and stats were disabled. The actual build output is `build/librealsense-build.log`.

No C/C++ source patches were needed. Packaging-only changes were made in `build/sdk-wheel-staging`: upstream project name `pyrealsense2` was changed to `pyrealsense2-macosx` so the wheel replaces the existing community package cleanly; `_version.py` was generated with 2.59.2; upstream package init, license and extension were copied to staging. `hatchling` inferred the platform wheel tag. `delocate-wheel --require-archs arm64` bundled native dependencies.

## Validation performed

The built wheel was installed with `pip --target build/sdk-wheel-test`, leaving the application's existing SDK untouched. With `PYTHONPATH=build/sdk-wheel-test`, Python successfully imported the built wheel and reported `rs.__version__ == '2.59.2'`, `rs.__full_version__ == '2.59.2.0'`. The module exposed `rs.config` and `rs.align`.

This isolated build validation did not open or enumerate the camera. Hardware capture is validated separately by the main task after installing the wheel.

## Install into the application

```bash
.venv/bin/python -m pip install --no-deps --force-reinstall \
  build/wheels/pyrealsense2_macosx-2.59.2-cp312-cp312-macosx_15_0_arm64.whl
.venv/bin/python -c 'import pyrealsense2 as rs; print(rs.__version__, rs.__file__)'
```

Restart Python processes that imported the old extension before testing capture. This does not flash firmware or change OS security settings.
