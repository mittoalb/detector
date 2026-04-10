#!/usr/bin/env python3
"""Pre-flight check for ORCA Fire detector system."""

import sys
import os
from pathlib import Path


def check(name, func):
    try:
        result = func()
        print(f"  [OK]  {name}: {result}")
        return True
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        return False


def main():
    print("ORCA Fire Detector - Environment Check")
    print("=" * 50)
    ok = True

    # Python version
    print(f"\nPython: {sys.version}")

    # Required packages
    print("\n--- Required packages ---")
    ok &= check("numpy", lambda: __import__("numpy").__version__)
    ok &= check("PyQt5", lambda: __import__("PyQt5.QtCore").QtCore.QT_VERSION_STR)

    # Optional packages
    print("\n--- Optional packages ---")
    check("imageio (TIFF export)", lambda: __import__("imageio").__version__)
    has_harvesters = check("harvesters (camera)", lambda: __import__("harvesters").__version__)

    # GenTL producer
    print("\n--- GenTL producer ---")
    gentl_candidates = [
        "/opt/euresys/egrabber/lib/x86_64/coaxlink.cti",
        "/opt/euresys/egrabber/lib/coaxlink.cti",
        "/opt/euresys/GenTL/Producer/x86_64/coaxlink.cti",
    ]
    env_path = os.environ.get("EURESYS_GENTL_PRODUCER")
    if env_path:
        gentl_candidates.insert(0, env_path)

    gentl_found = None
    for path in gentl_candidates:
        if Path(path).exists():
            gentl_found = path
            break

    if gentl_found:
        print(f"  [OK]  GenTL producer: {gentl_found}")
    else:
        print("  [FAIL] GenTL producer: not found in standard paths")
        print("         Install Euresys eGrabber or set EURESYS_GENTL_PRODUCER env var")

    # Frame grabber and camera detection
    if has_harvesters and gentl_found:
        print("\n--- Frame grabber & camera detection ---")
        try:
            from harvesters.core import Harvester
            h = Harvester()
            h.add_file(gentl_found)
            h.update()

            # Show system/interface info if available
            try:
                for i, iface in enumerate(h.interface_info_list):
                    print(f"  [OK]  Interface {i}: {iface.id} ({iface.display_name})")
            except Exception:
                print(f"  [OK]  GenTL producer loaded: {gentl_found}")

            devices = h.device_info_list
            if devices:
                for i, d in enumerate(devices):
                    print(f"  [OK]  Camera {i}: {d.vendor} {d.model} (SN: {d.serial_number})")
            else:
                print("  [WARN] No cameras detected — frame grabber OK but no camera connected")
                print("         Connect camera and power it on, then re-run this check")

            h.reset()
        except Exception as e:
            print(f"  [FAIL] Enumeration failed: {e}")
    elif not has_harvesters:
        print("\n--- Frame grabber & camera detection ---")
        print("  [SKIP] harvesters not installed, cannot detect hardware")
    elif not gentl_found:
        print("\n--- Frame grabber & camera detection ---")
        print("  [SKIP] No GenTL producer (.cti) found, cannot detect hardware")

    # Summary
    print("\n" + "=" * 50)
    if ok and gentl_found:
        print("Ready to run: python orca/qt_detector.py")
    elif ok:
        print("Ready to run in SIMULATION mode: python orca/qt_detector.py")
        print("For real camera: install Euresys eGrabber + harvesters")
    else:
        print("Missing required packages. Install with:")
        print("  pip install numpy PyQt5")


if __name__ == "__main__":
    main()
