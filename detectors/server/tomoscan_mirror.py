"""Standalone subprocess: mirror tomoscan's FrameType and HDF5Location PVs
into our detector IOC's cam1:FrameType, so the HDF5 plugin's simple-mode
router creates /exchange/data, /exchange/data_white, /exchange/data_dark.

Uses pyepics — proven-working shell CA client — deliberately in a
SEPARATE process from the IOC. In-process CA subscriptions from within
the caproto server proved unreliable at 32-ID.

Invocation:
    python -m detectors.server.tomoscan_mirror \
        --ts-prefix 32id:TomoScan: \
        --det-prefix 32idbSP1:

Started automatically by run_ioc.py when --ts-prefix is set. Runs until
its parent dies (SIGTERM/SIGKILL on IOC shutdown; daemonized).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from epics import PV

logger = logging.getLogger(__name__)


# Tomoscan writes exactly these three values into its FrameType softioc PV.
# The camera IOC's cam1:FrameType enum accepts the same three, so a
# 1:1 pass-through is safe.
_ALLOWED = {"Projection", "FlatField", "DarkField"}


def _make_forwarder(target_pv: PV, name: str):
    def _cb(pvname=None, value=None, char_value=None, **kw):
        # pyepics gives ENUM as index+char; use char_value if present.
        v = char_value if char_value else value
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace").rstrip("\x00")
        elif not isinstance(v, str):
            v = str(v)
        v = v.strip()
        if v not in _ALLOWED:
            logger.debug("mirror: %s value %r not in %s, skipping",
                         name, v, _ALLOWED)
            return
        try:
            target_pv.put(v, wait=False)
            logger.info("mirror: %s -> %s = %s",
                        name, target_pv.pvname, v)
        except Exception:
            logger.exception("mirror put failed for %s = %s",
                             target_pv.pvname, v)
    return _cb


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Mirror tomoscan FrameType into detector cam1:FrameType")
    p.add_argument("--ts-prefix", required=True,
                   help="TomoScan softioc PV prefix (e.g. 32id:TomoScan:)")
    p.add_argument("--det-prefix", required=True,
                   help="Detector IOC prefix (e.g. 32idbSP1:)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")

    src_name = args.ts_prefix + "FrameType"
    dst_name = args.det_prefix + "cam1:FrameType"

    logger.info("tomoscan_mirror: %s -> %s (starting)", src_name, dst_name)

    # Never exit on missing PVs — the mirror should keep trying so
    # scientists can start the detector IOC, the tomoscan softioc, and
    # the mirror in ANY order without silent failures. Log status every
    # ~30 s while things aren't up; forward as soon as they are.
    dst = PV(dst_name)
    src = PV(src_name, callback=_make_forwarder(dst, "FrameType"),
             auto_monitor=True)

    last_status_t = 0.0
    primed = False
    while True:
        now = time.time()
        both_up = dst.connected and src.connected
        if both_up and not primed:
            logger.info("tomoscan_mirror: both PVs up. Forwarding on updates.")
            # Prime destination with current source value so we don't
            # wait for the first FrameType change to sync state.
            try:
                v0 = src.get(as_string=True)
                if v0:
                    v0 = v0.strip()
                    if v0 in _ALLOWED:
                        dst.put(v0, wait=False)
                        logger.info(
                            "tomoscan_mirror: initial forward %s -> %s = %s",
                            src_name, dst_name, v0)
            except Exception:
                logger.exception("tomoscan_mirror: initial forward failed")
            primed = True
        elif not both_up:
            primed = False    # if we lose either side, re-prime later
            if now - last_status_t > 30:
                logger.warning(
                    "tomoscan_mirror: waiting — src(%s)=%s, dst(%s)=%s",
                    src_name, "up" if src.connected else "down",
                    dst_name, "up" if dst.connected else "down")
                last_status_t = now
        time.sleep(1.0)


if __name__ == "__main__":
    main()
