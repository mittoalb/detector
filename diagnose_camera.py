#!/usr/bin/env python3
"""
Diagnose what speeds-up tricks work on a connected camera.

Tries: full-frame, binning 2x2, binning 4x4, SubArray 2048x2048,
SubArray 1024x1024, SubArray 512x512.

Usage:
    LD_LIBRARY_PATH=/usr/local/hamamatsu_dcam/api:$LD_LIBRARY_PATH \
        python diagnose_camera.py [--camera <type>]
"""

import argparse
import logging
import time

from detectors.core import registry


def measure_fps(cam, n_frames: int = 30) -> float:
    cam.start_acquisition()
    cam.acquire_frame(timeout_ms=5000)  # warm up
    t0 = time.time()
    for _ in range(n_frames):
        cam.acquire_frame(timeout_ms=5000)
    elapsed = time.time() - t0
    cam.stop_acquisition()
    return n_frames / elapsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", default="hamamatsu.orca_fire_dcam",
                        help="Camera backend type")
    parser.add_argument("--exposure", type=float, default=0.001,
                        help="Exposure time in seconds")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s")

    registry.load_all()
    cam = registry.create(args.camera)
    cam.open()
    info = cam.get_info()
    sw, sh = info.sensor_width, info.sensor_height
    print(f"Camera: {info.vendor} {info.model}")
    print(f"Sensor: {sw} x {sh}\n")

    cam.set_param("ExposureTime", args.exposure)
    print(f"Exposure: {args.exposure*1000:.2f} ms\n")

    # Reset state
    try:
        cam.set_param("BinningHorizontal", 1)
    except Exception:
        pass

    # Full frame
    fps = measure_fps(cam)
    print(f"Full frame  ({sw}x{sh}):       {fps:6.1f} fps")

    # Binning 2x2
    try:
        cam.set_param("BinningHorizontal", 2)
        fps = measure_fps(cam)
        print(f"Binning 2x2  ({sw//2}x{sh//2}):     {fps:6.1f} fps")
        cam.set_param("BinningHorizontal", 1)
    except Exception as exc:
        print(f"Binning 2x2: {exc}")

    # Binning 4x4
    try:
        cam.set_param("BinningHorizontal", 4)
        fps = measure_fps(cam)
        print(f"Binning 4x4  ({sw//4}x{sh//4}):     {fps:6.1f} fps")
        cam.set_param("BinningHorizontal", 1)
    except Exception as exc:
        print(f"Binning 4x4: {exc}")

    # SubArray cropped readout
    if hasattr(cam, "set_subarray"):
        for size in [2048, 1024, 512]:
            try:
                cam.set_subarray(size, size)
                fps = measure_fps(cam)
                print(f"SubArray   ({size}x{size}):    {fps:6.1f} fps")
            except Exception as exc:
                print(f"SubArray {size}: {exc}")
        # Reset to full frame
        try:
            cam.set_subarray(sw, sh)
        except Exception:
            pass

    cam.close()


if __name__ == "__main__":
    main()
