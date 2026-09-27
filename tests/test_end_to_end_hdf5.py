"""End-to-end integration test for layout-XML HDF5 mode.

Spins up in-process:
  - a fake tomoscan softioc (caproto) with a subset of TS: PVs
  - the real DetectorIOC with the simulator camera

Drives Capture, flips TS:HDF5Location per phase, opens the resulting
HDF5 file, and validates the layout matches synApps expectations —
/exchange/{data,data_white,data_dark}, /measurement/instrument/name,
/measurement/sample/name populated from the fake TS: PVs.

Slower than the unit tests (~15 s) and needs to bind CA ports; skipped
if either fails.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import random
import time
from pathlib import Path

import h5py
import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _run_fake_ts(port: int):
    os.environ["EPICS_CAS_INTF_ADDR_LIST"] = "127.0.0.1"
    os.environ["EPICS_CAS_BEACON_ADDR_LIST"] = "127.0.0.1"
    os.environ["EPICS_CAS_SERVER_PORT"] = str(port)
    from caproto.server import PVGroup, pvproperty, run

    class TS(PVGroup):
        HDF5Location = pvproperty(
            value="/exchange/data", dtype=str, max_length=64)
        NumAngles = pvproperty(value=750, dtype=int)
        RotationStart = pvproperty(value=0.0, dtype=float)
        RotationStop = pvproperty(value=180.0, dtype=float)
        NumFlatFields = pvproperty(value=8, dtype=int)
        NumDarkFields = pvproperty(value=4, dtype=int)
        SampleName = pvproperty(value="TestSample", dtype=str, max_length=64)
        UserLastName = pvproperty(value="Mittone", dtype=str, max_length=64)
        FlipStitch = pvproperty(value="No", dtype=str, max_length=32)
        FlatFieldMode = pvproperty(value="Manual", dtype=str, max_length=32)
        FlatFieldAxis = pvproperty(value="X", dtype=str, max_length=32)
        SampleInX = pvproperty(value=0.5, dtype=float)
        SampleOutX = pvproperty(value=5.0, dtype=float)
        SampleInY = pvproperty(value=0.0, dtype=float)
        SampleOutY = pvproperty(value=0.0, dtype=float)
        FlatExposureTime = pvproperty(value=0.1, dtype=float)
        DifferentFlatExposure = pvproperty(
            value="Same", dtype=str, max_length=32)
        SampleOutAngleEnable = pvproperty(
            value="No", dtype=str, max_length=32)
        SampleOutAngle = pvproperty(value=0.0, dtype=float)
        ReturnRotation = pvproperty(value="Yes", dtype=str, max_length=32)
        ScanType = pvproperty(value="fly", dtype=str, max_length=32)
        FlipRotation = pvproperty(value="No", dtype=str, max_length=32)
        UserEmail = pvproperty(value="a@b.c", dtype=str, max_length=64)
        UserInstitution = pvproperty(value="APS", dtype=str, max_length=64)
        UserBadge = pvproperty(value="", dtype=str, max_length=32)
        ProposalNumber = pvproperty(value="12345", dtype=str, max_length=32)
        ProposalTitle = pvproperty(value="Test", dtype=str, max_length=64)
        ESAFNumber = pvproperty(value="1", dtype=str, max_length=32)
        SampleDescription1 = pvproperty(value="", dtype=str, max_length=64)
        SampleDescription2 = pvproperty(value="", dtype=str, max_length=64)
        SampleDescription3 = pvproperty(value="", dtype=str, max_length=64)
        Testing = pvproperty(value=0, dtype=int)
        StartScan = pvproperty(value=0, dtype=int)
        Rotation = pvproperty(value=0.0, dtype=float)
        RotationStep = pvproperty(value=0.24, dtype=float)
        RotationSpeed = pvproperty(value=1.0, dtype=float)

    g = TS(prefix="TS:")
    run(g.pvdb, interfaces=["127.0.0.1"])


def _run_detector_ioc(port: int, xml_dir: str, prefix: str):
    os.environ["EPICS_CAS_INTF_ADDR_LIST"] = "127.0.0.1"
    os.environ["EPICS_CAS_BEACON_ADDR_LIST"] = "127.0.0.1"
    os.environ["EPICS_CAS_SERVER_PORT"] = str(port)
    from caproto.server import run
    from detectors.cameras.simulator import SimulatedCamera
    from detectors.server.ioc import DetectorIOC

    cam = SimulatedCamera(); cam.open()
    ioc = DetectorIOC(
        prefix=prefix,
        camera=cam,
        nd_attributes_macros={"TS": "TS:"},
        xml_search_paths=[xml_dir],
    )
    run(ioc.pvdb, interfaces=["127.0.0.1"])


@pytest.mark.skipif(
    not os.environ.get("RUN_HDF5_E2E"),
    reason="End-to-end CA test — flaky in shared CI environments. "
           "Set RUN_HDF5_E2E=1 to run explicitly.")
@pytest.mark.timeout(120)
def test_end_to_end_layout_hdf5(tmp_path):
    prefix = "TESTDET:"
    port = random.randint(30000, 40000)
    ts_port = port + 100

    ctx = mp.get_context("spawn")
    ts_proc = ctx.Process(target=_run_fake_ts, args=(ts_port,), daemon=True)
    det_proc = ctx.Process(
        target=_run_detector_ioc,
        args=(port, str(FIXTURE_DIR), prefix), daemon=True)
    ts_proc.start()
    det_proc.start()

    try:
        # Both servers on same host, different ports. The client below sees
        # both via EPICS_CA_ADDR_LIST.
        os.environ["EPICS_CA_ADDR_LIST"] = (
            f"127.0.0.1:{port} 127.0.0.1:{ts_port}")
        os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"

        from caproto.threading.client import Context, SharedBroadcaster
        broadcaster = SharedBroadcaster()
        client_ctx = Context(broadcaster=broadcaster)

        # Wait for the IOCs to boot
        time.sleep(4)

        def put(name, value):
            (pv,) = client_ctx.get_pvs(name)
            pv.wait_for_connection(timeout=5)
            pv.write(value, wait=True, timeout=5)

        def get_str(name):
            (pv,) = client_ctx.get_pvs(name)
            pv.wait_for_connection(timeout=5)
            r = pv.read(data_type="native")
            data = r.data
            if hasattr(data, "__len__") and len(data) == 1:
                data = data[0]
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace").rstrip("\x00")
            return str(data)

        # Sanity: PVs on both IOCs are reachable
        assert get_str(prefix + "cam1:Manufacturer_RBV") == "Simulator"
        assert get_str("TS:HDF5Location") == "/exchange/data"

        # Configure attribute XML + layout XML (basenames — resolved via
        # xml_search_paths passed to DetectorIOC).
        put(prefix + "cam1:NDAttributesFile", b"TomoScanDetectorAttributes.xml")
        put(prefix + "HDF1:XMLFileName", b"TomoScanLayout.xml")
        time.sleep(2)  # let NDAttribute subscriptions prime

        put(prefix + "HDF1:FilePath", str(tmp_path).encode())
        put(prefix + "HDF1:FileName", b"test_e2e")
        put(prefix + "HDF1:FileNumber", 1)
        put(prefix + "HDF1:NumCapture", 6)

        # Start capture
        put(prefix + "HDF1:Capture", "Capture")

        # Phase 1 — 2 flat fields
        put("TS:HDF5Location", b"/exchange/data_white")
        time.sleep(0.5)
        put(prefix + "cam1:NumImages", 2)
        put(prefix + "cam1:ImageMode", "Multiple")
        put(prefix + "cam1:Acquire", "Acquire")
        time.sleep(3)

        # Phase 2 — 2 projections
        put("TS:HDF5Location", b"/exchange/data")
        time.sleep(0.5)
        put(prefix + "cam1:NumImages", 2)
        put(prefix + "cam1:Acquire", "Acquire")
        time.sleep(3)

        # Phase 3 — 2 dark fields
        put("TS:HDF5Location", b"/exchange/data_dark")
        time.sleep(0.5)
        put(prefix + "cam1:NumImages", 2)
        put(prefix + "cam1:Acquire", "Acquire")
        time.sleep(5)

        # Wait for capture to finish (NumCapture reached triggers close)
        deadline = time.time() + 15
        while time.time() < deadline:
            state = get_str(prefix + "HDF1:Capture_RBV")
            if state in ("Done", "0"):
                break
            time.sleep(0.5)
        # Extra time for background fsync
        time.sleep(2)

        # Verify output
        files = sorted(tmp_path.glob("*.h5"))
        assert files, f"no HDF5 file in {tmp_path}"
        with h5py.File(str(files[0]), "r") as f:
            assert "exchange" in f
            assert "measurement" in f
            assert "defaults" in f
            # Frame splitting
            assert "/exchange/data" in f
            assert "/exchange/data_white" in f
            assert "/exchange/data_dark" in f
            # Structure
            assert "/measurement/instrument/name" in f
            assert "/measurement/sample/name" in f
            # NDAttribute pulled from fake TS:
            sname = f["/measurement/sample/name"][()]
            if isinstance(sname, bytes):
                sname = sname.decode()
            assert sname == "TestSample"
            # Legacy /defaults still there for tomoscan add_theta
            assert "/defaults/HDF5FrameLocation" in f
            assert "/defaults/NDArrayUniqueId" in f

    finally:
        try:
            broadcaster.disconnect()
        except Exception:
            pass
        for p in (ts_proc, det_proc):
            try:
                p.terminate(); p.join(timeout=5)
            except Exception:
                pass
