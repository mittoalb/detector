#!/usr/bin/env python3
"""
Camera-agnostic EPICS IOC for area detector cameras.

Supports multiple camera types via a registered backend system. New cameras
are added by creating a class in `detectors/cameras/` that implements
`detectors.core.base.BaseCamera`.

Usage:
    # List available camera backends
    python run_ioc.py --list-cameras

    # Auto-detect (try all backends)
    python run_ioc.py --prefix ORCA:

    # Specify backend
    python run_ioc.py --camera hamamatsu.orca_fire_dcam --prefix ORCA:
    python run_ioc.py --camera teledyne.oryx --prefix ORYX:
    python run_ioc.py --camera simulator --prefix SIM:

    # Show all served PVs
    python run_ioc.py --camera simulator --list-pvs

    # Launch with GUI
    python run_ioc.py --camera simulator --gui
"""

import argparse
import logging
import sys
import threading

from caproto.server import run as caproto_run

from detectors.core import registry


def main():
    parser = argparse.ArgumentParser(
        description="Multi-camera areaDetector EPICS IOC")
    parser.add_argument("--prefix", default="DET:",
                        help="EPICS PV prefix (default: DET:)")
    parser.add_argument("--camera",
                        help="Camera backend (e.g. hamamatsu.orca_fire_dcam). "
                             "If omitted, auto-detects.")
    parser.add_argument("--device-index", type=int, default=0,
                        help="Camera device index when multiple of same type")
    parser.add_argument("--pva-pv",
                        help="PVA NTNDArray PV name "
                             "(default: {prefix}image1:ArrayData)")
    parser.add_argument("--interfaces", nargs="+", default=["0.0.0.0"],
                        help="Network interfaces (default: 0.0.0.0)")
    parser.add_argument("--log-level",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        default="INFO")
    parser.add_argument("--list-cameras", action="store_true",
                        help="List available camera backends and exit")
    parser.add_argument("--list-pvs", action="store_true",
                        help="List served PVs and exit")
    parser.add_argument("--gui", action="store_true",
                        help="Launch the Qt GUI alongside the IOC")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger(__name__)

    # Load all available backends
    registry.load_all()

    if args.list_cameras:
        print("Available camera backends:")
        for ct in registry.list_types():
            cls = registry.get_class(ct)
            print(f"  {ct:40s} {cls.display_name}")
        return

    # Open camera
    camera = None
    if args.camera:
        try:
            camera = registry.create(args.camera, device_index=args.device_index)
            camera.open()
            logger.info("Opened: %s", camera.display_name)
        except Exception as exc:
            logger.error("Failed to open %s: %s", args.camera, exc)
            sys.exit(1)
    else:
        logger.info("Auto-detecting camera...")
        camera = registry.auto_detect()
        if camera is None:
            logger.warning("No camera detected — running in simulation mode")
            try:
                camera = registry.create("simulator", device_index=args.device_index)
                camera.open()
            except Exception as exc:
                logger.error("Cannot start simulator: %s", exc)
                sys.exit(1)

    # Build IOC
    from detectors.server.ioc import DetectorIOC
    ioc = DetectorIOC(prefix=args.prefix, camera=camera, pva_pv=args.pva_pv)

    if args.list_pvs:
        print(f"\nPVs served by {args.prefix} IOC:\n")
        for pv_name in sorted(ioc.pvdb.keys()):
            print(f"  {pv_name}")
        print(f"\nTotal: {len(ioc.pvdb)} PVs")
        camera.close()
        return

    # Run IOC + optional GUI
    if args.gui:
        logger.info("Starting IOC server in background thread")
        ioc_thread = threading.Thread(
            target=caproto_run, args=(ioc.pvdb,),
            kwargs={"interfaces": args.interfaces}, daemon=True)
        ioc_thread.start()

        logger.info("Starting Qt GUI")
        _run_gui(ioc)
    else:
        logger.info("Serving %d PVs on %s", len(ioc.pvdb), args.interfaces)
        try:
            caproto_run(ioc.pvdb, interfaces=args.interfaces)
        finally:
            camera.close()


def _run_gui(ioc):
    """Launch the Qt GUI bound to the IOC's camera."""
    from detectors.gui.qt_gui import DetectorGui

    try:
        from PyQt5 import QtWidgets
    except ImportError:
        from PySide6 import QtWidgets

    app = QtWidgets.QApplication(sys.argv)
    window = DetectorGui(ioc)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
