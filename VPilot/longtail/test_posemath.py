"""Verify the Python rotation matches the plugin's C++ `rotate()` term for term.

The C++ (ObjectDet/Functions.h) is transcribed literally below and compared
against posemath.rotation_matrix over random angles. If this test fails, every
pose in the dataset is wrong in a way that is invisible frame by frame.

Run:  python -m longtail.test_posemath      (from VPilot/)
   or python test_posemath.py               (from VPilot/longtail/)
"""
import math
import numpy as np

try:
    from . import posemath
except ImportError:
    import posemath


def cpp_rotate(a, theta):
    """Literal transcription of Functions.h::rotate (theta in RADIANS)."""
    a0, a1, a2 = a
    t0, t1, t2 = theta
    d0 = (math.cos(t2) * (math.cos(t1) * a0 + math.sin(t1) * (math.sin(t0) * a1 + math.cos(t0) * a2))
          - math.sin(t2) * (math.cos(t0) * a1 - math.sin(t0) * a2))
    d1 = (math.sin(t2) * (math.cos(t1) * a0 + math.sin(t1) * (math.sin(t0) * a1 + math.cos(t0) * a2))
          + math.cos(t2) * (math.cos(t0) * a1 - math.sin(t0) * a2))
    d2 = (-math.sin(t1) * a0
          + math.cos(t1) * (math.sin(t0) * a1 + math.cos(t0) * a2))
    return np.array([d0, d1, d2])


def test_rotation_matches_cpp():
    rng = np.random.default_rng(0)
    worst = 0.0
    for _ in range(2000):
        theta_deg = rng.uniform(-180, 180, 3)
        theta_rad = np.radians(theta_deg)
        r = posemath.rotation_matrix(theta_deg)
        for axis in (posemath.WORLD_EAST, posemath.WORLD_NORTH, posemath.WORLD_UP):
            worst = max(worst, float(np.abs(r @ axis - cpp_rotate(axis, theta_rad)).max()))
    assert worst < 1e-12, f"rotation mismatch, worst abs err {worst}"
    return worst


def test_basis_is_orthonormal():
    rng = np.random.default_rng(1)
    worst = 0.0
    for _ in range(500):
        right, up, fwd = posemath.camera_basis(rng.uniform(-180, 180, 3))
        m = np.stack([right, up, fwd])
        worst = max(worst, float(np.abs(m @ m.T - np.eye(3)).max()))
    assert worst < 1e-12, f"basis not orthonormal, worst {worst}"
    return worst


def test_projection_roundtrip():
    """A point placed at a known camera-frame offset must land where we say."""
    rng = np.random.default_rng(2)
    k = posemath.intrinsics(50.0, 1920, 1080, aspect_ratio=1920 / 1080)
    worst = 0.0
    for _ in range(500):
        pos = rng.uniform(-500, 500, 3)
        theta = rng.uniform(-90, 90, 3)
        right, up, fwd = posemath.camera_basis(theta)
        # Build a world point from a chosen camera-frame offset.
        depth = rng.uniform(5, 100)
        rx, ry = rng.uniform(-3, 3), rng.uniform(-3, 3)
        world = pos + fwd * depth + right * rx - up * ry
        uv, z = posemath.project(world, pos, theta, k)
        assert z[0] > 0
        expect_u = k[0, 0] * (rx / depth) + k[0, 2]
        expect_v = k[1, 1] * (ry / depth) + k[1, 2]
        worst = max(worst, abs(uv[0, 0] - expect_u), abs(uv[0, 1] - expect_v))
    assert worst < 1e-6, f"projection mismatch {worst}"
    return worst


def test_nonsquare_pixels():
    """DSR-style mismatch between buffer size and reported aspect must give fx != fy."""
    k = posemath.intrinsics(50.0, 3840, 2160, aspect_ratio=1.0)
    assert abs(k[0, 0] - k[1, 1]) > 1.0, "expected non-square pixels when aspect != W/H"
    k2 = posemath.intrinsics(50.0, 3840, 2160, aspect_ratio=3840 / 2160)
    assert abs(k2[0, 0] - k2[1, 1]) < 1e-9, "expected square pixels when aspect == W/H"
    return True


if __name__ == "__main__":
    print("rotate() vs C++      : worst abs err %.3e" % test_rotation_matches_cpp())
    print("basis orthonormality : worst abs err %.3e" % test_basis_is_orthonormal())
    print("projection roundtrip : worst abs err %.3e" % test_projection_roundtrip())
    test_nonsquare_pixels()
    print("non-square pixels    : OK")
    print("\nAll posemath tests passed.")
