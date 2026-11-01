#!/usr/bin/env bash
# Build pugrec-gpu (the GPU build compositor, native/pugrec-gpu) inside a distrobox and copy it
# to ~/.cache/pugrec/bin. The host stays untouched (image-based PugPal: no compilers, no dnf).
# It links the host's own libraries at run time (Mesa EGL/GLES, libgbm, libva, ffmpeg 8.1 from
# negativo17), so the box gets the same Fedora and the same ffmpeg repo for the headers.
#   scripts/build-gpu.sh               build (reuses the box if it exists)
#   distrobox rm -f pugrec-build       remove the box afterwards (the binary stays)
set -euo pipefail

VERSION=1                           # bump with pugrec/edit/gpu.py's GPU_VERSION
BOX=pugrec-build
IMAGE=registry.fedoraproject.org/fedora-toolbox:44
here="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/pugrec"
out="$cache/bin/pugrec-gpu-$VERSION"
build="$cache/build/pugrec-gpu"

if ! distrobox list | grep -qw "$BOX"; then
    distrobox create --yes --name "$BOX" --image "$IMAGE"
fi
mkdir -p "$cache/bin" "$build"
cp "$here"/native/pugrec-gpu/* "$build/"

distrobox enter "$BOX" -- bash -euo pipefail -c "
    if ! rpm -q libavcodec-devel >/dev/null 2>&1; then
        sudo dnf config-manager addrepo --overwrite --from-repofile=https://negativo17.org/repos/fedora-multimedia.repo >/dev/null
        sudo dnf install -y --setopt=install_weak_deps=False gcc make pkgconf-pkg-config libglvnd-devel \
            mesa-libgbm-devel libdrm-devel libavcodec-devel libavformat-devel libavfilter-devel libavutil-devel >/dev/null
    fi
    make -C '$build' -s
"
install -m 755 "$build/pugrec-gpu" "$out"
if ldd "$out" | grep -q 'not found'; then
    ldd "$out" | grep 'not found' >&2
    echo "build-gpu: the host lacks a library pugrec-gpu needs (ffmpeg updated? rebuild the box)" >&2
    exit 1
fi
echo "built $out"
