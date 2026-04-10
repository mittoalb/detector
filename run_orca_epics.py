#!/usr/bin/env python3
"""
Runner script for ORCA Fire EPICS AreaDetector server.

This script starts a caproto-based EPICS server that provides areaDetector-like
PV access to an ORCA Fire camera via harvesters/GenTL.

Usage:
    python run_orca_epics.py [--prefix PREFIX] [--gentl-path PATH] [--device-index N]

The server will create PVs with the given prefix, e.g.:
    ORCA:Acquire
    ORCA:ExposureTime
    ORCA:ArrayData
    etc.

Use pystream or another NTNDArray viewer to view the image stream:
    pystream ORCA:
"""

import argparse
import logging
import sys
from pathlib import Path

# Add the orca package to the path
sys.path.insert(0, str(Path(__file__).parent))

from orca.harvesters_epics_area_detector import OrcaFireAreaDetector

try:
    from caproto.server import run
except ImportError:
    print("caproto is required. Install with: pip install caproto")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="ORCA Fire EPICS AreaDetector Server")
    parser.add_argument(
        "--prefix",
        default="ORCA:",
        help="EPICS PV prefix (default: ORCA:)"
    )
    parser.add_argument(
        "--gentl-path",
        help="Path to Euresys GenTL producer library"
    )
    parser.add_argument(
        "--device-index",
        type=int,
        default=0,
        help="Camera device index (default: 0)"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)"
    )

    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level))
    logger = logging.getLogger(__name__)

    try:
        logger.info("Starting ORCA Fire EPICS server with prefix '%s'", args.prefix)
        logger.info("GenTL path: %s", args.gentl_path or "auto-detect")
        logger.info("Device index: %d", args.device_index)

        # Create the PV group
        pv_group = OrcaFireAreaDetector(
            prefix=args.prefix,
            gen_tl_producer_path=args.gentl_path,
            device_index=args.device_index
        )

        # Start the server
        logger.info("EPICS server starting... Connect with pystream: pystream %s", args.prefix)
        run(pv_group.pvdb, interfaces=["127.255.255.255"])

    except Exception as e:
        logger.error("Failed to start server: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()