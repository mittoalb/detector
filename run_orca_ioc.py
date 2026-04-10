#!/usr/bin/env python3
"""
Run the ORCA Fire areaDetector-compatible EPICS IOC.

This starts a caproto PV server that mimics a standard areaDetector IOC,
allowing tomoscan and other EPICS clients to control the ORCA Fire camera
via harvesters/GenTL (no DCAM-SDK needed).

Usage:
    python run_orca_ioc.py                    # IOC only (headless)
    python run_orca_ioc.py --gui              # IOC + Qt parameter GUI
    python run_orca_ioc.py --prefix ORCA:     # custom PV prefix
    python run_orca_ioc.py --list-pvs         # show all PVs and exit
"""

import argparse
import logging
import sys
import threading

from caproto.server import run as caproto_run
from orca.epics_ad_server import OrcaFireIOC


def main():
    parser = argparse.ArgumentParser(
        description="ORCA Fire areaDetector-compatible EPICS IOC"
    )
    parser.add_argument(
        "--prefix", default="ORCA:",
        help="EPICS PV prefix (default: ORCA:)",
    )
    parser.add_argument(
        "--gentl-path",
        help="Path to Euresys GenTL producer (.cti file)",
    )
    parser.add_argument(
        "--device-index", type=int, default=0,
        help="Camera device index (default: 0)",
    )
    parser.add_argument(
        "--interfaces", nargs="+", default=["0.0.0.0"],
        help="Network interfaces to listen on (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--list-pvs", action="store_true",
        help="Print all served PVs and exit",
    )
    parser.add_argument(
        "--gui", action="store_true",
        help="Also launch the Qt parameter control GUI",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger = logging.getLogger(__name__)
    logger.info("Starting ORCA Fire IOC with prefix '%s'", args.prefix)

    ioc = OrcaFireIOC(
        prefix=args.prefix,
        gen_tl_producer_path=args.gentl_path,
        device_index=args.device_index,
    )

    if args.list_pvs:
        print(f"\nPVs served by {args.prefix} IOC:\n")
        for pv_name in sorted(ioc.pvdb.keys()):
            print(f"  {pv_name}")
        print(f"\nTotal: {len(ioc.pvdb)} PVs")
        return

    if args.gui:
        # Run caproto in a background thread, Qt GUI in the main thread
        logger.info("Starting IOC server in background thread")
        ioc_thread = threading.Thread(
            target=caproto_run,
            args=(ioc.pvdb,),
            kwargs={"interfaces": args.interfaces},
            daemon=True,
        )
        ioc_thread.start()

        logger.info("Starting Qt GUI (IOC running in background)")
        _run_gui(ioc)
    else:
        # Headless IOC only
        logger.info("Serving %d PVs on %s (no GUI)", len(ioc.pvdb), args.interfaces)
        logger.info("Use --gui to also launch the parameter control GUI")
        caproto_run(ioc.pvdb, interfaces=args.interfaces)


def _run_gui(ioc: OrcaFireIOC):
    """Launch the Qt GUI with a backend adapter wired to the IOC."""
    from orca.qt_detector import QtOrcaFireDetectorGui
    from orca.ioc_backend import IOCBackend

    try:
        from PyQt5 import QtWidgets
    except ImportError:
        from PySide6 import QtWidgets

    app = QtWidgets.QApplication(sys.argv)
    backend = IOCBackend(ioc)
    window = QtOrcaFireDetectorGui(backend)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
