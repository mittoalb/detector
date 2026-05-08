"""
Hamamatsu ORCA Fire (and other DCAM-supported cameras) via DCAM-API.

Requires DCAM-API Lite for Linux installed and a supported frame grabber
(Active Silicon FireBird for CXP, or USB3 for USB cameras).
"""

import ctypes
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np

from detectors.core.base import BaseCamera, CameraInfo
from detectors.core.registry import register

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DCAM property IDs and value enums
# ---------------------------------------------------------------------------

DCAM_IDPROP = {
    "ExposureTime": 0x001F0110,
    "TriggerSource": 0x00100110,
    "TriggerActive": 0x00100120,
    "TriggerMode": 0x00100210,
    "TriggerPolarity": 0x00100220,
    "TriggerDelay": 0x00100230,
    "TriggerTimes": 0x00100240,
    "Binning": 0x00401110,
    "SensorTemperature": 0x00200310,
    "SensorCooler": 0x00200320,
    "SensorTemperatureTarget": 0x00200330,
    "SensorCoolerStatus": 0x00200340,
    "SensorMode": 0x00400210,
    "ReadoutSpeed": 0x00400110,
    "ReadoutDirection": 0x00400130,
    "ShutterMode": 0x00400150,
    "SubarrayMode": 0x00402350,
    "SubarrayHPos": 0x00402110,
    "SubarrayHSize": 0x00402120,
    "SubarrayVPos": 0x00402310,
    "SubarrayVSize": 0x00402320,
    "ImageWidth": 0x00420110,
    "ImageHeight": 0x00420120,
    "ImageRowBytes": 0x00420130,
    "ImagePixelType": 0x00420270,
    "InternalFrameRate": 0x00420210,
}

ENUM_TO_DCAM = {
    "TriggerSource": {"Internal": 1, "External": 2, "Software": 3, "MasterPulse": 4},
    "TriggerActive": {"Edge": 1, "Level": 2, "SyncReadout": 3, "Point": 4},
    "TriggerPolarity": {"Negative": 1, "Positive": 2},
    "TriggerMode": {"Normal": 1, "Start": 6},
    "ShutterMode": {"Global": 1, "Rolling": 2},
    "SensorMode": {"Area": 1, "Lightsheet": 14, "SplitView": 17, "DualLightsheet": 12},
    "ReadoutSpeed": {"Slowest": 1, "Fastest": 2, "Fast": 2},
    "ReadoutDirection": {"Forward": 1, "Backward": 2, "Bidirectional": 3, "Reverse": 4},
    "SensorCooler": {"Off": 1, "On": 2, "Max": 4},
    "SensorCoolerStatus": {"Error": 0, "Off": 1, "Ready": 2, "Busy": 3, "Always": 4},
}

DCAM_TO_ENUM = {k: {n: s for s, n in v.items()} for k, v in ENUM_TO_DCAM.items()}

DCAM_PIXELTYPE_MONO16 = 0x00000002
DCAM_PIXELTYPE_MONO8 = 0x00000001
DCAMWAIT_CAPEVENT_FRAMEREADY = 0x0002
DCAMCAP_START_SEQUENCE = -1

DEFAULT_LIB_PATH = "/usr/local/hamamatsu_dcam/api/libdcamapi.so.4"


# ---------------------------------------------------------------------------
# ctypes structures
# ---------------------------------------------------------------------------

class DCAMAPI_INIT(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_int32), ("iDeviceCount", ctypes.c_int32),
        ("reserved", ctypes.c_int32), ("initoptionbytes", ctypes.c_int32),
        ("initoption", ctypes.c_void_p), ("guid", ctypes.c_void_p),
    ]


class DCAMDEV_OPEN(ctypes.Structure):
    _fields_ = [("size", ctypes.c_int32), ("index", ctypes.c_int32),
                ("hdcam", ctypes.c_void_p)]


class DCAMBUF_FRAME(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_int32), ("iKind", ctypes.c_int32),
        ("option", ctypes.c_int32), ("iFrame", ctypes.c_int32),
        ("buf", ctypes.c_void_p), ("rowbytes", ctypes.c_int32),
        ("type", ctypes.c_int32), ("width", ctypes.c_int32),
        ("height", ctypes.c_int32), ("left", ctypes.c_int32),
        ("top", ctypes.c_int32), ("timestamp_sec", ctypes.c_int32),
        ("timestamp_usec", ctypes.c_int32), ("framestamp", ctypes.c_int32),
        ("camerastamp", ctypes.c_int32),
    ]


class DCAMWAIT_OPEN(ctypes.Structure):
    _fields_ = [("size", ctypes.c_int32), ("supportevent", ctypes.c_int32),
                ("hwait", ctypes.c_void_p), ("hdcam", ctypes.c_void_p)]


class DCAMWAIT_START(ctypes.Structure):
    _fields_ = [("size", ctypes.c_int32), ("eventhappened", ctypes.c_int32),
                ("eventmask", ctypes.c_int32), ("timeout", ctypes.c_int32)]


class DCAMCAP_TRANSFERINFO(ctypes.Structure):
    _fields_ = [("size", ctypes.c_int32), ("iKind", ctypes.c_int32),
                ("nNewestFrameIndex", ctypes.c_int32),
                ("nFrameCount", ctypes.c_int32)]


# ---------------------------------------------------------------------------
# Module-level DCAM lifecycle (only one init/uninit pair per process)
# ---------------------------------------------------------------------------

_dcam_lib = None
_dcam_init_count = 0
_dcam_lock = threading.Lock()
_dcam_device_count = 0


def _dcam_init(lib_path: str = DEFAULT_LIB_PATH) -> int:
    """Initialize DCAM library (refcounted). Returns device count."""
    global _dcam_lib, _dcam_init_count, _dcam_device_count
    with _dcam_lock:
        if _dcam_lib is None:
            _dcam_lib = ctypes.CDLL(lib_path, mode=ctypes.RTLD_GLOBAL)
            _setup_signatures(_dcam_lib)
            state = DCAMAPI_INIT()
            state.size = ctypes.sizeof(DCAMAPI_INIT)
            err = _dcam_lib.dcamapi_init(ctypes.byref(state))
            if err < 0 or state.iDeviceCount == 0:
                _dcam_lib = None
                raise RuntimeError(
                    f"DCAM init failed (err=0x{err & 0xFFFFFFFF:08X}, "
                    f"devices={state.iDeviceCount})")
            _dcam_device_count = state.iDeviceCount
            logger.info("DCAM initialized, %d device(s) found", _dcam_device_count)
        _dcam_init_count += 1
        return _dcam_device_count


def _dcam_uninit() -> None:
    """Release DCAM library (refcounted)."""
    global _dcam_lib, _dcam_init_count
    with _dcam_lock:
        _dcam_init_count -= 1
        if _dcam_init_count <= 0 and _dcam_lib is not None:
            _dcam_lib.dcamapi_uninit()
            _dcam_lib = None
            _dcam_init_count = 0


def _setup_signatures(lib):
    lib.dcamapi_init.restype = ctypes.c_int32
    lib.dcamapi_init.argtypes = [ctypes.POINTER(DCAMAPI_INIT)]
    lib.dcamapi_uninit.restype = ctypes.c_int32
    lib.dcamdev_open.restype = ctypes.c_int32
    lib.dcamdev_open.argtypes = [ctypes.POINTER(DCAMDEV_OPEN)]
    lib.dcamdev_close.restype = ctypes.c_int32
    lib.dcamdev_close.argtypes = [ctypes.c_void_p]
    lib.dcamprop_getvalue.restype = ctypes.c_int32
    lib.dcamprop_getvalue.argtypes = [ctypes.c_void_p, ctypes.c_int32,
                                       ctypes.POINTER(ctypes.c_double)]
    lib.dcamprop_setvalue.restype = ctypes.c_int32
    lib.dcamprop_setvalue.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_double]
    lib.dcambuf_alloc.restype = ctypes.c_int32
    lib.dcambuf_alloc.argtypes = [ctypes.c_void_p, ctypes.c_int32]
    lib.dcambuf_release.restype = ctypes.c_int32
    lib.dcambuf_release.argtypes = [ctypes.c_void_p, ctypes.c_int32]
    lib.dcambuf_lockframe.restype = ctypes.c_int32
    lib.dcambuf_lockframe.argtypes = [ctypes.c_void_p, ctypes.POINTER(DCAMBUF_FRAME)]
    lib.dcamcap_start.restype = ctypes.c_int32
    lib.dcamcap_start.argtypes = [ctypes.c_void_p, ctypes.c_int32]
    lib.dcamcap_stop.restype = ctypes.c_int32
    lib.dcamcap_stop.argtypes = [ctypes.c_void_p]
    lib.dcamcap_firetrigger.restype = ctypes.c_int32
    lib.dcamcap_firetrigger.argtypes = [ctypes.c_void_p, ctypes.c_int32]
    lib.dcamcap_transferinfo.restype = ctypes.c_int32
    lib.dcamcap_transferinfo.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(DCAMCAP_TRANSFERINFO)]
    lib.dcamwait_open.restype = ctypes.c_int32
    lib.dcamwait_open.argtypes = [ctypes.POINTER(DCAMWAIT_OPEN)]
    lib.dcamwait_close.restype = ctypes.c_int32
    lib.dcamwait_close.argtypes = [ctypes.c_void_p]
    lib.dcamwait_start.restype = ctypes.c_int32
    lib.dcamwait_start.argtypes = [ctypes.c_void_p, ctypes.POINTER(DCAMWAIT_START)]
    lib.dcamwait_abort.restype = ctypes.c_int32
    lib.dcamwait_abort.argtypes = [ctypes.c_void_p]


# ---------------------------------------------------------------------------
# OrcaFireDCAM camera backend
# ---------------------------------------------------------------------------

@register
class OrcaFireDCAM(BaseCamera):
    """Hamamatsu ORCA Fire (C16240) via DCAM-API."""

    camera_type = "hamamatsu.orca_fire_dcam"
    display_name = "Hamamatsu ORCA Fire (DCAM)"

    NUM_BUFFERS = 10

    def __init__(self, device_index: int = 0,
                 lib_path: str = DEFAULT_LIB_PATH, **kwargs):
        super().__init__(device_index=device_index, **kwargs)
        self._lib_path = lib_path
        self._hdcam: Optional[int] = None
        self._hwait: Optional[int] = None

    def open(self) -> None:
        if self._hdcam is not None:
            return
        _dcam_init(self._lib_path)

        dev = DCAMDEV_OPEN()
        dev.size = ctypes.sizeof(DCAMDEV_OPEN)
        dev.index = self.device_index
        err = _dcam_lib.dcamdev_open(ctypes.byref(dev))
        if err < 0 or not dev.hdcam:
            _dcam_uninit()
            raise RuntimeError(
                f"Failed to open camera (err=0x{err & 0xFFFFFFFF:08X})")
        self._hdcam = dev.hdcam

        wait = DCAMWAIT_OPEN()
        wait.size = ctypes.sizeof(DCAMWAIT_OPEN)
        wait.hdcam = self._hdcam
        if _dcam_lib.dcamwait_open(ctypes.byref(wait)) >= 0:
            self._hwait = wait.hwait

        # Try DCAM properties for sensor dimensions, but they're unreliable
        # on the C16240-20UP (return 1). Acquire one frame to get true size.
        w, h = 0, 0
        try:
            w, h = self._probe_frame_size()
        except Exception as exc:
            logger.warning("Could not probe frame size: %s", exc)

        if w == 0 or h == 0:
            w, h = 4432, 2368  # Known ORCA Fire C16240-20UP active area

        self._info = CameraInfo(
            vendor="Hamamatsu", model="C16240-20UP",
            sensor_width=w, sensor_height=h,
            bits_per_pixel=16, pixel_size_um=4.6,
            max_frame_rate=115.0,
            supported_binning=[1, 2, 4],
            supported_trigger_modes=["Internal", "External", "Software"],
            has_temperature=True, has_cooler=False,
            has_subarray=True,
        )
        logger.info("Opened %s (device %d)", self.display_name, self.device_index)

    def _probe_frame_size(self) -> tuple:
        """Acquire one frame to determine actual width and height."""
        _dcam_lib.dcambuf_alloc(self._hdcam, 1)
        try:
            err = _dcam_lib.dcamcap_start(self._hdcam, DCAMCAP_START_SEQUENCE)
            if err < 0:
                return 0, 0
            try:
                if self._hwait:
                    ws = DCAMWAIT_START()
                    ws.size = ctypes.sizeof(DCAMWAIT_START)
                    ws.eventmask = DCAMWAIT_CAPEVENT_FRAMEREADY
                    ws.timeout = 5000
                    if _dcam_lib.dcamwait_start(self._hwait, ctypes.byref(ws)) < 0:
                        return 0, 0
                info = DCAMCAP_TRANSFERINFO()
                info.size = ctypes.sizeof(DCAMCAP_TRANSFERINFO)
                _dcam_lib.dcamcap_transferinfo(self._hdcam, ctypes.byref(info))
                frame = DCAMBUF_FRAME()
                frame.size = ctypes.sizeof(DCAMBUF_FRAME)
                frame.iFrame = info.nNewestFrameIndex
                if _dcam_lib.dcambuf_lockframe(self._hdcam, ctypes.byref(frame)) < 0:
                    return 0, 0
                return frame.width, frame.height
            finally:
                _dcam_lib.dcamcap_stop(self._hdcam)
        finally:
            _dcam_lib.dcambuf_release(self._hdcam, 0)

    def close(self) -> None:
        if self._is_acquiring:
            try:
                self.stop_acquisition()
            except Exception:
                pass
        if self._hwait:
            _dcam_lib.dcamwait_close(self._hwait)
            self._hwait = None
        if self._hdcam:
            _dcam_lib.dcambuf_release(self._hdcam, 0)
            _dcam_lib.dcamdev_close(self._hdcam)
            self._hdcam = None
        self._info = None
        _dcam_uninit()

    # --- Property access ---

    def _get_dcam(self, prop_id: int) -> float:
        val = ctypes.c_double()
        err = _dcam_lib.dcamprop_getvalue(self._hdcam, prop_id, ctypes.byref(val))
        if err < 0:
            raise RuntimeError(
                f"dcamprop_getvalue 0x{prop_id:08X} failed: 0x{err & 0xFFFFFFFF:08X}")
        return val.value

    def _set_dcam(self, prop_id: int, value: float) -> None:
        err = _dcam_lib.dcamprop_setvalue(self._hdcam, prop_id, ctypes.c_double(value))
        if err < 0:
            raise RuntimeError(
                f"dcamprop_setvalue 0x{prop_id:08X}={value} failed: "
                f"0x{err & 0xFFFFFFFF:08X}")

    def list_params(self) -> List[str]:
        # Return params that this camera actually supports
        candidates = list(DCAM_IDPROP.keys()) + ["BinningHorizontal", "BinningVertical",
                                                   "Width", "Height", "TriggerMode"]
        supported = []
        for name in candidates:
            try:
                self.get_param(name)
                supported.append(name)
            except Exception:
                pass
        return supported

    def set_subarray(self, hsize: int, vsize: int,
                     hpos: Optional[int] = None,
                     vpos: Optional[int] = None) -> None:
        """Set a centered (or specified) SubArray (ROI) for cropped readout.

        Cropped readout is the only way to exceed the camera's full-frame
        max fps on the ORCA Fire. Smaller hsize/vsize → higher fps.

        Args:
            hsize: width in pixels (must be a multiple supported by camera)
            vsize: height in pixels
            hpos: top-left x (default: centered on sensor)
            vpos: top-left y (default: centered on sensor)
        """
        was_acquiring = self._is_acquiring
        if was_acquiring:
            self.stop_acquisition()
        try:
            sw = self._info.sensor_width
            sh = self._info.sensor_height
            if hpos is None:
                hpos = max(0, (sw - hsize) // 2)
            if vpos is None:
                vpos = max(0, (sh - vsize) // 2)
            # Disable subarray to allow changing dimensions, then re-enable
            try:
                self._set_dcam(DCAM_IDPROP["SubarrayMode"], 1)  # OFF
            except Exception:
                pass
            self._set_dcam(DCAM_IDPROP["SubarrayHPos"], float(hpos))
            self._set_dcam(DCAM_IDPROP["SubarrayVPos"], float(vpos))
            self._set_dcam(DCAM_IDPROP["SubarrayHSize"], float(hsize))
            self._set_dcam(DCAM_IDPROP["SubarrayVSize"], float(vsize))
            self._set_dcam(DCAM_IDPROP["SubarrayMode"], 2)  # ON
            logger.info("SubArray: %dx%d at (%d, %d)", hsize, vsize, hpos, vpos)
        finally:
            if was_acquiring:
                self.start_acquisition()

    def get_param(self, name: str) -> Any:
        # Standard parameter aliases
        if name in ("BinningHorizontal", "BinningVertical"):
            val = self._get_dcam(DCAM_IDPROP["Binning"])
            return int(val)
        if name == "Width":
            return int(self._get_dcam(DCAM_IDPROP["ImageWidth"]))
        if name == "Height":
            return int(self._get_dcam(DCAM_IDPROP["ImageHeight"]))
        if name == "AcquisitionFrameRate":
            return self._get_dcam(DCAM_IDPROP["InternalFrameRate"])
        if name == "TriggerMode":
            # "On" if external, "Off" otherwise
            src = int(self._get_dcam(DCAM_IDPROP["TriggerSource"]))
            return "On" if src == ENUM_TO_DCAM["TriggerSource"]["External"] else "Off"

        prop_id = DCAM_IDPROP.get(name)
        if prop_id is None:
            raise KeyError(f"Unknown param: {name}")
        val = self._get_dcam(prop_id)
        # Convert numeric to enum string if applicable
        if name in DCAM_TO_ENUM:
            return DCAM_TO_ENUM[name].get(int(val), str(int(val)))
        return val

    def set_param(self, name: str, value: Any) -> None:
        # Standard aliases
        # SubArray dimension changes need acquisition stopped + mode toggle
        if name in ("SubarrayHSize", "SubarrayVSize",
                    "SubarrayHPos", "SubarrayVPos"):
            was_acquiring = self._is_acquiring
            if was_acquiring:
                self.stop_acquisition()
            try:
                # Disable subarray so dimensions can be edited
                try:
                    self._set_dcam(DCAM_IDPROP["SubarrayMode"], 1)  # OFF
                except Exception:
                    pass
                self._set_dcam(DCAM_IDPROP[name], float(int(value)))
                self._set_dcam(DCAM_IDPROP["SubarrayMode"], 2)  # ON
                logger.info("%s = %s (SubArray ON)", name, int(value))
            finally:
                if was_acquiring:
                    self.start_acquisition()
            return

        if name in ("BinningHorizontal", "BinningVertical"):
            # Note: on the ORCA Fire C16240 family, DCAM 'Binning' is
            # digital (post-readout pixel sum) — it shrinks the image
            # but does NOT speed up the sensor. To actually gain fps,
            # use SubarrayHSize / SubarrayVSize (cropped readout).
            was_acquiring = self._is_acquiring
            if was_acquiring:
                self.stop_acquisition()
            try:
                self._set_dcam(DCAM_IDPROP["Binning"], float(int(value)))
                actual = self._get_dcam(DCAM_IDPROP["Binning"])
                logger.info("Binning set to %s, readback=%s", value, actual)
            finally:
                if was_acquiring:
                    self.start_acquisition()
            return
        if name == "TriggerMode":
            if str(value) == "Off":
                self._set_dcam(DCAM_IDPROP["TriggerSource"],
                                ENUM_TO_DCAM["TriggerSource"]["Internal"])
            else:
                self._set_dcam(DCAM_IDPROP["TriggerSource"],
                                ENUM_TO_DCAM["TriggerSource"]["External"])
            return

        prop_id = DCAM_IDPROP.get(name)
        if prop_id is None:
            raise KeyError(f"Unknown param: {name}")

        # Convert enum string to numeric
        if name in ENUM_TO_DCAM and isinstance(value, str):
            num_val = ENUM_TO_DCAM[name].get(value)
            if num_val is None:
                raise ValueError(
                    f"Invalid value '{value}' for {name}. "
                    f"Allowed: {list(ENUM_TO_DCAM[name].keys())}")
            value = num_val

        self._set_dcam(prop_id, float(value))

    # --- Acquisition ---

    def start_acquisition(self) -> None:
        if self._is_acquiring:
            return
        _dcam_lib.dcambuf_alloc(self._hdcam, self.NUM_BUFFERS)
        err = _dcam_lib.dcamcap_start(self._hdcam, DCAMCAP_START_SEQUENCE)
        if err < 0:
            raise RuntimeError(f"dcamcap_start failed: 0x{err & 0xFFFFFFFF:08X}")
        self._is_acquiring = True

    def stop_acquisition(self) -> None:
        if not self._is_acquiring:
            return
        _dcam_lib.dcamcap_stop(self._hdcam)
        _dcam_lib.dcambuf_release(self._hdcam, 0)
        self._is_acquiring = False

    def acquire_frame(self, timeout_ms: int = 5000) -> np.ndarray:
        if not self._is_acquiring:
            raise RuntimeError("Acquisition not running")

        if self._hwait:
            ws = DCAMWAIT_START()
            ws.size = ctypes.sizeof(DCAMWAIT_START)
            ws.eventmask = DCAMWAIT_CAPEVENT_FRAMEREADY
            ws.timeout = timeout_ms
            err = _dcam_lib.dcamwait_start(self._hwait, ctypes.byref(ws))
            if err < 0:
                raise RuntimeError(f"dcamwait_start failed: 0x{err & 0xFFFFFFFF:08X}")

        info = DCAMCAP_TRANSFERINFO()
        info.size = ctypes.sizeof(DCAMCAP_TRANSFERINFO)
        _dcam_lib.dcamcap_transferinfo(self._hdcam, ctypes.byref(info))

        frame = DCAMBUF_FRAME()
        frame.size = ctypes.sizeof(DCAMBUF_FRAME)
        frame.iFrame = info.nNewestFrameIndex
        err = _dcam_lib.dcambuf_lockframe(self._hdcam, ctypes.byref(frame))
        if err < 0:
            raise RuntimeError(f"dcambuf_lockframe failed: 0x{err & 0xFFFFFFFF:08X}")

        if frame.type == DCAM_PIXELTYPE_MONO16:
            nbytes = frame.rowbytes * frame.height
            buf = (ctypes.c_uint8 * nbytes).from_address(frame.buf)
            arr = np.frombuffer(buf, dtype=np.uint16).reshape(
                (frame.height, frame.rowbytes // 2))
            return arr[:, :frame.width].copy()
        else:
            nbytes = frame.rowbytes * frame.height
            buf = (ctypes.c_uint8 * nbytes).from_address(frame.buf)
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(
                (frame.height, frame.rowbytes))
            return arr[:, :frame.width].copy()

    def software_trigger(self) -> None:
        err = _dcam_lib.dcamcap_firetrigger(self._hdcam, 0)
        if err < 0:
            raise RuntimeError(f"dcamcap_firetrigger failed: 0x{err & 0xFFFFFFFF:08X}")
