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
import os
import sys
import threading

from caproto.server import run as caproto_run

from detectors.core import registry


from detectors.core.log_util import install_color_logging as _install_color_logging


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
    parser.add_argument("--serial",
                        help="Camera serial number (Spinnaker/Oryx). "
                             "Takes precedence over --device-index.")
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
    parser.add_argument("--gui-only", action="store_true",
                        help="Launch ONLY the Qt GUI against an already-"
                             "running IOC at --prefix (no camera/IOC "
                             "startup). Use this to reopen the GUI after "
                             "closing its window.")
    # NDAttributes / Layout-XML HDF5 mode: tomoscan's DetectorAttributes.xml
    # references PVs on OTHER soft IOCs via macros. --ts-prefix names the
    # tomoscan softioc's prefix so $(TS)... in the attributes XML resolves
    # correctly. Extend this pattern for other macros (--txm-prefix etc.)
    # if the XML references more IOCs.
    parser.add_argument("--ts-prefix",
                        default=os.environ.get("TS_PREFIX", "32id:TomoScan:"),
                        help="TomoScan softioc PV prefix; substituted for "
                             "$(TS) in the NDAttributes XML "
                             "(default: %(default)s; env: TS_PREFIX).")
    parser.add_argument("--xml-search-path", action="append", default=[],
                        help="Directory to search when tomoscan puts an "
                             "NDAttributes / layout XML basename. May be "
                             "given multiple times. Also honors env "
                             "AREA_DETECTOR_ATTRIBUTES_PATH.")
    args = parser.parse_args()

    # --gui-only: just spawn the GUI subprocess and wait, no IOC.
    # Handy for reattaching after closing the window without kicking the
    # running IOC (which still serves PVs, PVA frames, and IPC).
    if args.gui_only:
        _install_color_logging(level=getattr(logging, args.log_level))
        import subprocess
        logger = logging.getLogger(__name__)
        pkg_root = os.path.dirname(os.path.abspath(__file__))
        env = os.environ.copy()
        env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
        logger.info("Attaching GUI to running IOC at prefix %s", args.prefix)
        proc = subprocess.Popen(
            [sys.executable, "-m", "detectors.gui.qt_gui",
             "--prefix", args.prefix,
             "--log-level", args.log_level],
            env=env)
        proc.wait()
        return

    _install_color_logging(level=getattr(logging, args.log_level))
    # Silence caproto's per-connection chatter so real errors are visible
    for name in ("caproto", "caproto.ctx", "caproto.ch", "caproto.bcast",
                 "caproto.client", "caproto.circuit"):
        logging.getLogger(name).setLevel(logging.WARNING)
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
            extra = {"serial": args.serial} if args.serial else {}
            camera = registry.create(args.camera,
                                     device_index=args.device_index, **extra)
            camera.open()
            logger.info("Opened: %s", camera.display_name)
        except Exception as exc:
            logger.error("Failed to open %s: %s", args.camera, exc)
            sys.exit(1)
    else:
        logger.info("Auto-detecting camera...")
        camera = registry.auto_detect()
        if camera is None:
            logger.error(
                "No real camera detected. Refusing to silently fall back to "
                "the simulator — that has caused fake data to be written to "
                "HDF5 in the past. If you actually want the simulator, run "
                "with `--camera simulator`. To see why auto-detect failed, "
                "rerun with `--log-level DEBUG`.")
            sys.exit(2)

    # Build IOC
    from detectors.server.ioc import DetectorIOC
    ioc = DetectorIOC(
        prefix=args.prefix,
        camera=camera,
        pva_pv=args.pva_pv,
        nd_attributes_macros={"TS": args.ts_prefix},
        xml_search_paths=args.xml_search_path,
    )

    if args.list_pvs:
        print(f"\nPVs served by {args.prefix} IOC:\n")
        for pv_name in sorted(ioc.pvdb.keys()):
            print(f"  {pv_name}")
        print(f"\nTotal: {len(ioc.pvdb)} PVs")
        camera.close()
        return

    # Spawn subprocess helpers (GUI + tomoscan_mirror). All use pyepics
    # from a separate process so they don't have to share the caproto
    # server's CA state — which in this environment proved unreliable
    # for in-process CA client work.
    import subprocess
    pkg_root = os.path.dirname(os.path.abspath(__file__))
    env = os.environ.copy()
    env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
    subprocs = []

    # Tomoscan mirror: forwards $(TS)FrameType -> $(DET)cam1:FrameType so
    # the HDF5 plugin's per-frame routing lands frames in the correct
    # /exchange/data* dataset (data, data_white, data_dark). Always
    # spawned when --ts-prefix is set; harmless if tomoscan isn't up
    # (mirror exits with a clear log message).
    if args.ts_prefix:
        logger.info("Launching tomoscan_mirror subprocess "
                    "(TS=%s DET=%s)", args.ts_prefix, args.prefix)
        mirror_proc = subprocess.Popen(
            [sys.executable, "-m", "detectors.server.tomoscan_mirror",
             "--ts-prefix", args.ts_prefix,
             "--det-prefix", args.prefix,
             "--log-level", args.log_level],
            env=env)
        subprocs.append(("tomoscan_mirror", mirror_proc))

    if args.gui:
        # GUI runs as a subprocess and connects to the IOC via CA/PVA.
        # Qt in the SAME process as the camera driver breaks PVCAM's DMA
        # signalling (Kinetix); process isolation avoids that entirely,
        # and is uniform across all camera backends.
        logger.info("Launching Qt GUI as subprocess (--prefix %s)",
                    args.prefix)
        gui_proc = subprocess.Popen(
            [sys.executable, "-m", "detectors.gui.qt_gui",
             "--prefix", args.prefix,
             "--log-level", args.log_level],
            env=env)
        subprocs.append(("gui", gui_proc))

    logger.info("Serving %d PVs on %s", len(ioc.pvdb), args.interfaces)
    try:
        caproto_run(ioc.pvdb, interfaces=args.interfaces)
    finally:
        for name, proc in subprocs:
            try:
                proc.terminate()
            except Exception:
                logger.debug("terminate() failed for %s", name, exc_info=True)
        camera.close()


def _run_gui(ioc):
    """Launch the Qt GUI bound to the IOC's camera.

    Runs in a background thread so the main thread stays available for
    PVCAM. Qt permits this on Linux/X11 as long as all widgets are
    created inside this thread (which they are).
    """
    from detectors.gui.qt_gui import DetectorGui

    try:
        from PyQt5 import QtWidgets, QtCore
    except ImportError:
        from PySide6 import QtWidgets, QtCore

    app = QtWidgets.QApplication(sys.argv)
    window = DetectorGui(ioc)
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
