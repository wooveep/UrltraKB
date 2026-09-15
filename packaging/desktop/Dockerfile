# Both Debian targets use this same multi-architecture Debian 13 / Rust image.
FROM ghcr.io/astral-sh/uv:0.11.13@sha256:841c8e6fe30a8b07b4478d12d0c608cba6de66102d29d65d1cc423af86051563 AS uv
FROM rust:1.95.0-trixie@sha256:f49565f188ee00bc2a18dd418183f2c5f23ef7d6e691890517ed341a598f67c3
COPY --from=uv /uv /uvx /usr/local/bin/
# Freeze the Debian package universe as well as the base image.
RUN rm /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20260901T000000Z/ trixie main' > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 git ca-certificates pkg-config patchelf \
        libgl1 libegl1 libopengl0 libglib2.0-0t64 libdbus-1-3 libfontconfig1 \
        libx11-6 libx11-xcb1 libxext6 libxrender1 libxi6 libxkbcommon0 \
        libxkbcommon-x11-0 libxcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
        libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
        libxcb-shm0 libxcb-sync1 libxcb-xfixes0 libxcb-xkb1 \
    && rm -rf /var/lib/apt/lists/*
ENV QT_QPA_PLATFORM=offscreen PYTHONUTF8=1 CARGO_BUILD_JOBS=2
WORKDIR /workspace
