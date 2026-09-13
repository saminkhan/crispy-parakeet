#!/usr/bin/env bash
# Fetch the build dependencies for the `longtail` branch.
#
# The longtail build captures RGB + camera pose only. It excludes
# ObjectDet/ObjectDetection.cpp and ObjectDet/LiDAR.cpp behind LONGTAIL_SLIM,
# which drops OpenCV, Boost and GTAVisionNative outright -- so unlike upstream
# you need three header/source deps, not five-plus-a-sublibrary.
#
#   Eigen   header-only, used by Functions.h / DataExport.cpp / Scenario.cpp
#   cppzmq  header-only C++ binding (zmq.hpp)
#   libzmq  compiled directly into DeepGTAV.asi, so nothing has to ship beside it
#
# All three are gitignored. Run this once, then build (see LONGTAIL_SETUP.md §4).
set -euo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T"

echo "==> Eigen 3.4.0"
curl -fsSL -o eigen.tar.gz https://gitlab.com/libeigen/eigen/-/archive/3.4.0/eigen-3.4.0.tar.gz
tar xzf eigen.tar.gz
# NOTE: the directory is named 3.3.7 because that is the include path baked into
# DeepGTAV.vcxproj upstream. The contents are 3.4.0; Eigen is header-only here.
rm -rf "$R/eigen-3.3.7"; mkdir -p "$R/eigen-3.3.7"
cp -r eigen-3.4.0/Eigen eigen-3.4.0/unsupported "$R/eigen-3.3.7/"

echo "==> cppzmq 4.7.1"
curl -fsSL -o cppzmq.tar.gz https://github.com/zeromq/cppzmq/archive/refs/tags/v4.7.1.tar.gz
tar xzf cppzmq.tar.gz
rm -rf "$R/cppzmq-4.7.1"; mkdir -p "$R/cppzmq-4.7.1"
cp cppzmq-4.7.1/zmq.hpp cppzmq-4.7.1/zmq_addon.hpp "$R/cppzmq-4.7.1/"

echo "==> libzmq 4.3.5"
curl -fsSL -o libzmq.tar.gz https://github.com/zeromq/libzmq/archive/refs/tags/v4.3.5.tar.gz
tar xzf libzmq.tar.gz
rm -rf "$R/libzmq-4.3.5"; mkdir -p "$R/libzmq-4.3.5/builds/deprecated-msvc"
cp -r libzmq-4.3.5/include libzmq-4.3.5/src "$R/libzmq-4.3.5/"

# libzmq is normally configured by cmake, which generates platform.hpp. We compile
# it straight into the .asi, so there is no cmake run; upstream's MSVC platform.hpp
# defines only ZMQ_HAVE_WINDOWS and leaves the rest to their own .vcxproj options.
# Write the macros cmake would otherwise have produced.
cat > "$R/libzmq-4.3.5/builds/deprecated-msvc/platform.hpp" <<'PLATFORM'
#ifndef __PLATFORM_HPP_INCLUDED__
#define __PLATFORM_HPP_INCLUDED__

#define ZMQ_HAVE_WINDOWS

#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601        // Windows 7+; needed for the socket API surface
#endif

// I/O thread poller: select is the classic libzmq choice on Windows and the one
// its own MSVC project files enable. Exactly one of these may be defined.
#define ZMQ_IOTHREAD_POLLER_USE_SELECT
#define ZMQ_POLL_BASED_ON_SELECT

// Condition variables: STL11 uses std::condition_variable, which VS2022 has.
#define ZMQ_USE_CV_IMPL_STL11

#define ZMQ_CACHELINE_SIZE 64

#endif
PLATFORM

echo
echo "Done:"
echo "  eigen-3.3.7   $(find "$R/eigen-3.3.7" -type f | wc -l) files (contents are 3.4.0)"
echo "  cppzmq-4.7.1  $(find "$R/cppzmq-4.7.1" -type f | wc -l) files"
echo "  libzmq-4.3.5  $(find "$R/libzmq-4.3.5" -type f | wc -l) files"
echo
echo "Next: LONGTAIL_SETUP.md section 4 (build)."
