#!/bin/bash
# Rebuild the tested Apple Silicon SDK wheel without installing it into .venv.
set -euo pipefail
SDK_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SDK_PROJECT_ROOT"
SDK_PYTHON="$SDK_PROJECT_ROOT/.venv/bin/python"
if [[ ! -x "$SDK_PYTHON" ]]; then
  printf 'Create .venv with Python 3.12 before running this build.\n' >&2
  exit 1
fi
export PATH="$SDK_PROJECT_ROOT/.venv/bin:$PATH"
"$SDK_PYTHON" -m pip install cmake==4.4.5 ninja==1.13.2 wheel==0.48.0 build==1.6.1 delocate==0.13.0 hatchling==1.33.0
mkdir -p vendor build
if [[ ! -d vendor/librealsense/.git ]]; then
  git clone --depth 1 --branch v2.59.2 https://github.com/realsenseai/librealsense.git vendor/librealsense
fi
if [[ "$(git -C vendor/librealsense rev-parse HEAD)" != 'e535e2445cfd00c31f67761ec13e0f99cb31c2dc' ]]; then
  printf 'Expected official librealsense v2.59.2 commit e535e2445cfd00c31f67761ec13e0f99cb31c2dc.\n' >&2
  exit 1
fi
if [[ ! -f vendor/libusb/libusb-1.0.29/configure ]]; then
  "$SDK_PYTHON" - <<'PY'
import io, pathlib, tarfile, urllib.request
destination = pathlib.Path('vendor/libusb')
destination.mkdir(parents=True, exist_ok=True)
url = 'https://github.com/libusb/libusb/releases/download/v1.0.29/libusb-1.0.29.tar.bz2'
payload = urllib.request.urlopen(url, timeout=60).read()
with tarfile.open(fileobj=io.BytesIO(payload), mode='r:bz2') as source:
    source.extractall(destination, filter='data')
PY
fi
(
  cd vendor/libusb/libusb-1.0.29
  ./configure --prefix="$SDK_PROJECT_ROOT/build/local-deps" --enable-shared --disable-static --disable-dependency-tracking
  make -j6
  make install
)
cmake -S vendor/librealsense -B build/librealsense-2.59.2 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES=arm64 \
  -DCMAKE_OSX_DEPLOYMENT_TARGET=15.0 \
  -DBUILD_PYTHON_BINDINGS=ON \
  -DBUILD_SHARED_LIBS=ON \
  -DBUILD_EXAMPLES=OFF \
  -DBUILD_GRAPHICAL_EXAMPLES=OFF \
  -DBUILD_TOOLS=OFF \
  -DBUILD_UNIT_TESTS=OFF \
  -DBUILD_WITH_OPENMP=OFF \
  -DBUILD_WITH_DDS=OFF \
  -DBUILD_LEGACY_PYBACKEND=OFF \
  -DCHECK_FOR_UPDATES=OFF \
  -DENABLE_AI_ASSISTANT=OFF \
  -DENABLE_STATS=OFF \
  -DHWM_OVER_XU=OFF \
  -DFORCE_RSUSB_BACKEND=ON \
  -DPython_EXECUTABLE="$SDK_PYTHON" \
  -DPYTHON_EXECUTABLE="$SDK_PYTHON" \
  -DLIBUSB_INC="$SDK_PROJECT_ROOT/build/local-deps/include/libusb-1.0" \
  -DLIBUSB_LIB="$SDK_PROJECT_ROOT/build/local-deps/lib/libusb-1.0.0.dylib" \
  -DCMAKE_INSTALL_PREFIX="$SDK_PROJECT_ROOT/build/local-deps"
cmake --build build/librealsense-2.59.2 --target pyrealsense2 -j6
"$SDK_PYTHON" - <<'PY'
from pathlib import Path
import shutil
source = Path('vendor/librealsense/wrappers/python')
stage = Path('build/sdk-wheel-staging')
package = stage / 'pyrealsense2'
package.mkdir(parents=True, exist_ok=True)
for name in ['pyproject.toml', 'hatch_build.py', 'README.rst']:
    shutil.copy2(source / name, stage / name)
shutil.copy2(source / 'pyrealsense2/__init__.py', package / '__init__.py')
shutil.copy2(Path('vendor/librealsense/LICENSE'), stage / 'LICENSE')
(package / '_version.py').write_text('__version__ = "2.59.2"\n')
project = stage / 'pyproject.toml'
project.write_text(project.read_text().replace('name = "pyrealsense2"', 'name = "pyrealsense2-macosx"'))
modules = list(Path('build/librealsense-2.59.2/Release').glob('pyrealsense2.cpython-*-darwin.so'))
if len(modules) != 1:
    raise RuntimeError(f'Expected one Python extension, found {modules}')
shutil.copy2(modules[0], package / modules[0].name)
PY
"$SDK_PYTHON" -m build --wheel --no-isolation --outdir build/sdk-wheel-unrepaired build/sdk-wheel-staging
delocate-wheel -v --require-archs arm64 -w build/wheels build/sdk-wheel-unrepaired/pyrealsense2_macosx-2.59.2-*.whl
printf '\nSDK wheel is in build/wheels. It has NOT been installed into .venv.\n'
