"""
DCAM-API wrapper for Hamamatsu ORCA Fire camera control.

Uses ctypes to call libdcamapi.so directly. Provides full camera control
including trigger, binning, temperature, and ROI — features not available
through the CXP GenICam interface.

Requires:
    - DCAM-API Lite for Linux installed at /usr/local/hamamatsu_dcam/
    - Active Silicon FireBird driver (kernel modules: aslenum, aslcxp, asldma)
    - LD_LIBRARY_PATH includes /usr/local/hamamatsu_dcam/api
"""

import ctypes
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DCAM property IDs (from dcamprop.h / hamamatsu Python package)
# ---------------------------------------------------------------------------

DCAM_IDPROP_EXPOSURETIME = 0x001F0110
DCAM_IDPROP_TRIGGERSOURCE = 0x00100110
DCAM_IDPROP_TRIGGERACTIVE = 0x00100120
DCAM_IDPROP_TRIGGER_MODE = 0x00100210
DCAM_IDPROP_TRIGGERPOLARITY = 0x00100220
DCAM_IDPROP_TRIGGERDELAY = 0x00100230
DCAM_IDPROP_TRIGGERTIMES = 0x00100240
DCAM_IDPROP_BINNING = 0x00401110
DCAM_IDPROP_SENSORTEMPERATURE = 0x00200310
DCAM_IDPROP_SENSORCOOLER = 0x00200320
DCAM_IDPROP_SENSORTEMPERATURETARGET = 0x00200330
DCAM_IDPROP_SENSORCOOLERSTATUS = 0x00200340
DCAM_IDPROP_SENSORMODE = 0x00400210
DCAM_IDPROP_READOUTSPEED = 0x00400110
DCAM_IDPROP_READOUT_DIRECTION = 0x00400130
DCAM_IDPROP_SHUTTER_MODE = 0x00400150
DCAM_IDPROP_SUBARRAYMODE = 0x00402350
DCAM_IDPROP_SUBARRAYHPOS = 0x00402110
DCAM_IDPROP_SUBARRAYHSIZE = 0x00402120
DCAM_IDPROP_SUBARRAYVPOS = 0x00402310
DCAM_IDPROP_SUBARRAYVSIZE = 0x00402320
DCAM_IDPROP_IMAGE_WIDTH = 0x00420110
DCAM_IDPROP_IMAGE_HEIGHT = 0x00420120
DCAM_IDPROP_IMAGE_ROWBYTES = 0x00420130
DCAM_IDPROP_IMAGE_PIXELTYPE = 0x00420270
DCAM_IDPROP_INTERNALFRAMERATE = 0x00420210

# DCAM property values
DCAMPROP_TRIGGERSOURCE__INTERNAL = 1
DCAMPROP_TRIGGERSOURCE__EXTERNAL = 2
DCAMPROP_TRIGGERSOURCE__SOFTWARE = 3
DCAMPROP_TRIGGERACTIVE__EDGE = 1
DCAMPROP_TRIGGERACTIVE__LEVEL = 2
DCAMPROP_TRIGGERACTIVE__SYNCREADOUT = 3
DCAMPROP_TRIGGERPOLARITY__NEGATIVE = 1
DCAMPROP_TRIGGERPOLARITY__POSITIVE = 2
DCAMPROP_TRIGGER_MODE__NORMAL = 1
DCAMPROP_TRIGGER_MODE__START = 6
DCAMPROP_SENSORMODE__AREA = 1
DCAMPROP_SHUTTER_MODE__GLOBAL = 1
DCAMPROP_SHUTTER_MODE__ROLLING = 2
DCAMPROP_SENSORCOOLERSTATUS__OFF = 1
DCAMPROP_SENSORCOOLERSTATUS__READY = 2
DCAMPROP_SENSORCOOLERSTATUS__BUSY = 3
DCAMPROP_SENSORCOOLERSTATUS__ERROR = 5
DCAMPROP_BINNING__1 = 1
DCAMPROP_BINNING__2 = 2
DCAMPROP_BINNING__4 = 4

# DCAM pixel types
DCAM_PIXELTYPE_MONO16 = 0x00000002
DCAM_PIXELTYPE_MONO8 = 0x00000001

# DCAM wait events
DCAMWAIT_CAPEVENT_FRAMEREADY = 0x0002

# DCAM capture modes
DCAMCAP_START_SEQUENCE = -1
DCAMCAP_START_SNAP = 0

# Default DCAM library path
DCAM_LIB_PATH = "/usr/local/hamamatsu_dcam/api/libdcamapi.so.4"


# ---------------------------------------------------------------------------
# ctypes structures
# ---------------------------------------------------------------------------

class DCAMAPI_INIT(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_int32),
        ("iDeviceCount", ctypes.c_int32),
        ("reserved", ctypes.c_int32),
        ("initoptionbytes", ctypes.c_int32),
        ("initoption", ctypes.c_void_p),
        ("guid", ctypes.c_void_p),
    ]


class DCAMDEV_OPEN(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_int32),
        ("index", ctypes.c_int32),
        ("hdcam", ctypes.c_void_p),
    ]


class DCAMBUF_FRAME(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_int32),
        ("iKind", ctypes.c_int32),
        ("option", ctypes.c_int32),
        ("iFrame", ctypes.c_int32),
        ("buf", ctypes.c_void_p),
        ("rowbytes", ctypes.c_int32),
        ("type", ctypes.c_int32),
        ("width", ctypes.c_int32),
        ("height", ctypes.c_int32),
        ("left", ctypes.c_int32),
        ("top", ctypes.c_int32),
        ("timestamp_sec", ctypes.c_int32),
        ("timestamp_usec", ctypes.c_int32),
        ("framestamp", ctypes.c_int32),
        ("camerastamp", ctypes.c_int32),
    ]


class DCAMWAIT_OPEN(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_int32),
        ("supportevent", ctypes.c_int32),
        ("hwait", ctypes.c_void_p),
        ("hdcam", ctypes.c_void_p),
    ]


class DCAMWAIT_START(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_int32),
        ("eventhappened", ctypes.c_int32),
        ("eventmask", ctypes.c_int32),
        ("timeout", ctypes.c_int32),
    ]


class DCAMCAP_TRANSFERINFO(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_int32),
        ("iKind", ctypes.c_int32),
        ("nNewestFrameIndex", ctypes.c_int32),
        ("nFrameCount", ctypes.c_int32),
    ]


# ---------------------------------------------------------------------------
# Map between GUI parameter names and DCAM property IDs
# ---------------------------------------------------------------------------

PARAM_TO_DCAM = {
    "ExposureTime": DCAM_IDPROP_EXPOSURETIME,
    "TriggerSource": DCAM_IDPROP_TRIGGERSOURCE,
    "TriggerActive": DCAM_IDPROP_TRIGGERACTIVE,
    "TriggerMode": DCAM_IDPROP_TRIGGER_MODE,
    "TriggerPolarity": DCAM_IDPROP_TRIGGERPOLARITY,
    "TriggerDelay": DCAM_IDPROP_TRIGGERDELAY,
    "TriggerTimes": DCAM_IDPROP_TRIGGERTIMES,
    "BinningHorizontal": DCAM_IDPROP_BINNING,
    "BinningVertical": DCAM_IDPROP_BINNING,
    "SensorTemperature": DCAM_IDPROP_SENSORTEMPERATURE,
    "SensorCooler": DCAM_IDPROP_SENSORCOOLER,
    "SensorTemperatureTarget": DCAM_IDPROP_SENSORTEMPERATURETARGET,
    "SensorCoolerStatus": DCAM_IDPROP_SENSORCOOLERSTATUS,
    "SensorMode": DCAM_IDPROP_SENSORMODE,
    "ReadoutSpeed": DCAM_IDPROP_READOUTSPEED,
    "ReadoutDirection": DCAM_IDPROP_READOUT_DIRECTION,
    "ShutterMode": DCAM_IDPROP_SHUTTER_MODE,
    "AcquisitionFrameRate": DCAM_IDPROP_INTERNALFRAMERATE,
}

# Map GUI enum strings to DCAM numeric values
ENUM_TO_DCAM = {
    "TriggerSource": {
        "Internal": DCAMPROP_TRIGGERSOURCE__INTERNAL,
        "External": DCAMPROP_TRIGGERSOURCE__EXTERNAL,
        "Software": DCAMPROP_TRIGGERSOURCE__SOFTWARE,
    },
    "TriggerActive": {
        "Edge": DCAMPROP_TRIGGERACTIVE__EDGE,
        "Level": DCAMPROP_TRIGGERACTIVE__LEVEL,
        "SyncReadout": DCAMPROP_TRIGGERACTIVE__SYNCREADOUT,
    },
    "TriggerPolarity": {
        "Negative": DCAMPROP_TRIGGERPOLARITY__NEGATIVE,
        "Positive": DCAMPROP_TRIGGERPOLARITY__POSITIVE,
    },
    "TriggerMode": {
        "Off": 0,  # special: sets TriggerSource to Internal
        "On": 1,   # special: sets TriggerSource to External
    },
}

# Reverse map: DCAM numeric values to GUI enum strings
DCAM_TO_ENUM = {
    "TriggerSource": {v: k for k, v in ENUM_TO_DCAM["TriggerSource"].items()},
    "TriggerActive": {v: k for k, v in ENUM_TO_DCAM["TriggerActive"].items()},
    "TriggerPolarity": {v: k for k, v in ENUM_TO_DCAM["TriggerPolarity"].items()},
}


# ---------------------------------------------------------------------------
# DCAMAcquirer
# ---------------------------------------------------------------------------

class DCAMAcquirer:
    """ORCA Fire camera control via DCAM-API.

    Provides full camera control including trigger, binning, temperature,
    and frame acquisition through the Active Silicon FireBird frame grabber.
    """

    NUM_BUFFERS = 10

    def __init__(self, device_index: int = 0, lib_path: str = DCAM_LIB_PATH):
        self._lib = ctypes.CDLL(lib_path, mode=ctypes.RTLD_GLOBAL)
        self._device_index = device_index
        self._hdcam = None
        self._hwait = None
        self._is_acquiring = False
        self._frame_callbacks: List[Callable] = []
        self._stream_thread: Optional[threading.Thread] = None
        self._stream_event = threading.Event()
        self._frame_counter = 0
        self._lock = threading.Lock()

        # Set up function signatures
        self._lib.dcamapi_init.restype = ctypes.c_int32
        self._lib.dcamapi_init.argtypes = [ctypes.POINTER(DCAMAPI_INIT)]
        self._lib.dcamapi_uninit.restype = ctypes.c_int32
        self._lib.dcamdev_open.restype = ctypes.c_int32
        self._lib.dcamdev_open.argtypes = [ctypes.POINTER(DCAMDEV_OPEN)]
        self._lib.dcamdev_close.restype = ctypes.c_int32
        self._lib.dcamdev_close.argtypes = [ctypes.c_void_p]
        self._lib.dcamprop_getvalue.restype = ctypes.c_int32
        self._lib.dcamprop_getvalue.argtypes = [
            ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_double)]
        self._lib.dcamprop_setvalue.restype = ctypes.c_int32
        self._lib.dcamprop_setvalue.argtypes = [
            ctypes.c_void_p, ctypes.c_int32, ctypes.c_double]
        self._lib.dcambuf_alloc.restype = ctypes.c_int32
        self._lib.dcambuf_alloc.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        self._lib.dcambuf_release.restype = ctypes.c_int32
        self._lib.dcambuf_release.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        self._lib.dcambuf_lockframe.restype = ctypes.c_int32
        self._lib.dcambuf_lockframe.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(DCAMBUF_FRAME)]
        self._lib.dcamcap_start.restype = ctypes.c_int32
        self._lib.dcamcap_start.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        self._lib.dcamcap_stop.restype = ctypes.c_int32
        self._lib.dcamcap_stop.argtypes = [ctypes.c_void_p]
        self._lib.dcamcap_firetrigger.restype = ctypes.c_int32
        self._lib.dcamcap_firetrigger.argtypes = [
            ctypes.c_void_p, ctypes.c_int32]
        self._lib.dcamcap_transferinfo.restype = ctypes.c_int32
        self._lib.dcamcap_transferinfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(DCAMCAP_TRANSFERINFO)]
        self._lib.dcamwait_open.restype = ctypes.c_int32
        self._lib.dcamwait_open.argtypes = [ctypes.POINTER(DCAMWAIT_OPEN)]
        self._lib.dcamwait_close.restype = ctypes.c_int32
        self._lib.dcamwait_close.argtypes = [ctypes.c_void_p]
        self._lib.dcamwait_start.restype = ctypes.c_int32
        self._lib.dcamwait_start.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(DCAMWAIT_START)]
        self._lib.dcamwait_abort.restype = ctypes.c_int32
        self._lib.dcamwait_abort.argtypes = [ctypes.c_void_p]

        # Initialize DCAM API
        state = DCAMAPI_INIT()
        state.size = ctypes.sizeof(DCAMAPI_INIT)
        err = self._lib.dcamapi_init(ctypes.byref(state))
        if err < 0 or state.iDeviceCount == 0:
            raise RuntimeError(
                f"DCAM init failed (err=0x{err & 0xFFFFFFFF:08X}, "
                f"devices={state.iDeviceCount})")
        logger.info("DCAM initialized, %d device(s) found", state.iDeviceCount)

    def open(self) -> None:
        """Open the camera device."""
        if self._hdcam is not None:
            return

        dev = DCAMDEV_OPEN()
        dev.size = ctypes.sizeof(DCAMDEV_OPEN)
        dev.index = self._device_index
        err = self._lib.dcamdev_open(ctypes.byref(dev))
        if err < 0 or not dev.hdcam:
            raise RuntimeError(f"Failed to open camera (err=0x{err & 0xFFFFFFFF:08X})")
        self._hdcam = dev.hdcam

        # Open wait handle
        wait = DCAMWAIT_OPEN()
        wait.size = ctypes.sizeof(DCAMWAIT_OPEN)
        wait.hdcam = self._hdcam
        err = self._lib.dcamwait_open(ctypes.byref(wait))
        if err < 0:
            logger.warning("Failed to open wait handle: 0x%08X", err & 0xFFFFFFFF)
        else:
            self._hwait = wait.hwait

        logger.info("Camera opened (device %d)", self._device_index)

    def close(self) -> None:
        """Close the camera and release resources."""
        if self._is_acquiring:
            self.stop_acquisition()
        if self._hwait:
            self._lib.dcamwait_close(self._hwait)
            self._hwait = None
        if self._hdcam:
            self._lib.dcambuf_release(self._hdcam, 0)
            self._lib.dcamdev_close(self._hdcam)
            self._hdcam = None
        self._lib.dcamapi_uninit()
        logger.info("Camera closed")

    # -- Property access --

    def get_property(self, prop_id: int) -> float:
        """Read a DCAM property value."""
        val = ctypes.c_double()
        err = self._lib.dcamprop_getvalue(self._hdcam, prop_id, ctypes.byref(val))
        if err < 0:
            raise RuntimeError(
                f"Failed to read property 0x{prop_id:08X} "
                f"(err=0x{err & 0xFFFFFFFF:08X})")
        return val.value

    def set_property(self, prop_id: int, value: float) -> None:
        """Write a DCAM property value."""
        err = self._lib.dcamprop_setvalue(self._hdcam, prop_id, ctypes.c_double(value))
        if err < 0:
            raise RuntimeError(
                f"Failed to set property 0x{prop_id:08X} to {value} "
                f"(err=0x{err & 0xFFFFFFFF:08X})")

    def get_param(self, name: str) -> Any:
        """Read a camera parameter by GUI name."""
        prop_id = PARAM_TO_DCAM.get(name)
        if prop_id is None:
            raise KeyError(f"Unknown parameter: {name}")
        try:
            val = self.get_property(prop_id)
        except RuntimeError:
            return None
        # Convert numeric to enum string if applicable
        if name in DCAM_TO_ENUM:
            return DCAM_TO_ENUM[name].get(int(val), str(int(val)))
        return val

    def set_param(self, name: str, value: Any) -> None:
        """Write a camera parameter by GUI name."""
        prop_id = PARAM_TO_DCAM.get(name)
        if prop_id is None:
            raise KeyError(f"Unknown parameter: {name}")

        # Handle special TriggerMode (Off/On maps to TriggerSource)
        if name == "TriggerMode":
            if str(value) == "Off":
                self.set_property(DCAM_IDPROP_TRIGGERSOURCE,
                                  DCAMPROP_TRIGGERSOURCE__INTERNAL)
            else:
                self.set_property(DCAM_IDPROP_TRIGGERSOURCE,
                                  DCAMPROP_TRIGGERSOURCE__EXTERNAL)
            return

        # Convert enum string to numeric
        if name in ENUM_TO_DCAM and isinstance(value, str):
            dcam_val = ENUM_TO_DCAM[name].get(value)
            if dcam_val is None:
                raise ValueError(f"Invalid value '{value}' for {name}")
            value = dcam_val

        self.set_property(prop_id, float(value))

    def describe_device(self) -> Dict[str, str]:
        """Return device information."""
        return {
            "vendor": "Hamamatsu",
            "model": "C16240-20UP",
            "serial_number": "",
        }

    # -- Acquisition --

    def start_acquisition(self) -> None:
        """Start continuous acquisition."""
        if self._is_acquiring:
            return
        self._lib.dcambuf_alloc(self._hdcam, self.NUM_BUFFERS)
        err = self._lib.dcamcap_start(self._hdcam, DCAMCAP_START_SEQUENCE)
        if err < 0:
            raise RuntimeError(f"Failed to start capture: 0x{err & 0xFFFFFFFF:08X}")
        self._is_acquiring = True

    def stop_acquisition(self) -> None:
        """Stop acquisition."""
        if not self._is_acquiring:
            return
        self._lib.dcamcap_stop(self._hdcam)
        self._lib.dcambuf_release(self._hdcam, 0)
        self._is_acquiring = False

    def acquire_frame(self, timeout_ms: int = 5000) -> np.ndarray:
        """Wait for and return a single frame."""
        if not self._is_acquiring:
            raise RuntimeError("Acquisition not running")

        # Wait for frame
        if self._hwait:
            ws = DCAMWAIT_START()
            ws.size = ctypes.sizeof(DCAMWAIT_START)
            ws.eventmask = DCAMWAIT_CAPEVENT_FRAMEREADY
            ws.timeout = timeout_ms
            err = self._lib.dcamwait_start(self._hwait, ctypes.byref(ws))
            if err < 0:
                raise RuntimeError(f"Wait failed: 0x{err & 0xFFFFFFFF:08X}")

        # Get transfer info for newest frame index
        info = DCAMCAP_TRANSFERINFO()
        info.size = ctypes.sizeof(DCAMCAP_TRANSFERINFO)
        self._lib.dcamcap_transferinfo(self._hdcam, ctypes.byref(info))

        # Lock and copy frame
        frame = DCAMBUF_FRAME()
        frame.size = ctypes.sizeof(DCAMBUF_FRAME)
        frame.iFrame = info.nNewestFrameIndex
        err = self._lib.dcambuf_lockframe(self._hdcam, ctypes.byref(frame))
        if err < 0:
            raise RuntimeError(f"Lock frame failed: 0x{err & 0xFFFFFFFF:08X}")

        # Copy data
        if frame.type == DCAM_PIXELTYPE_MONO16:
            nbytes = frame.rowbytes * frame.height
            buf = (ctypes.c_uint8 * nbytes).from_address(frame.buf)
            arr = np.frombuffer(buf, dtype=np.uint16).reshape(
                (frame.height, frame.rowbytes // 2))
            # Trim to actual width (rowbytes may include padding)
            return arr[:, :frame.width].copy()
        else:
            nbytes = frame.rowbytes * frame.height
            buf = (ctypes.c_uint8 * nbytes).from_address(frame.buf)
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(
                (frame.height, frame.rowbytes))
            return arr[:, :frame.width].copy()

    def software_trigger(self) -> None:
        """Fire a software trigger."""
        err = self._lib.dcamcap_firetrigger(self._hdcam, 0)
        if err < 0:
            raise RuntimeError(f"Fire trigger failed: 0x{err & 0xFFFFFFFF:08X}")

    # -- Streaming --

    def register_frame_callback(self, callback: Callable) -> None:
        self._frame_callbacks.append(callback)

    def unregister_frame_callback(self, callback: Callable) -> None:
        self._frame_callbacks.remove(callback)

    def start_stream(self, timeout_ms: int = 5000) -> None:
        """Start acquisition and stream frames to callbacks."""
        self.start_acquisition()
        if self._stream_thread is not None and self._stream_thread.is_alive():
            return
        self._stream_event.set()
        self._stream_thread = threading.Thread(
            target=self._stream_loop, args=(timeout_ms,), daemon=True)
        self._stream_thread.start()

    def stop_stream(self) -> None:
        """Stop streaming and acquisition."""
        self._stream_event.clear()
        if self._hwait:
            self._lib.dcamwait_abort(self._hwait)
        if self._stream_thread is not None:
            self._stream_thread.join(timeout=3.0)
            self._stream_thread = None
        self.stop_acquisition()

    def _stream_loop(self, timeout_ms: int) -> None:
        while self._stream_event.is_set() and self._is_acquiring:
            try:
                frame = self.acquire_frame(timeout_ms=timeout_ms)
                self._frame_counter += 1
                metadata = {
                    "timestamp": time.time(),
                    "frame_number": self._frame_counter,
                }
            except Exception:
                time.sleep(0.01)
                continue
            for cb in self._frame_callbacks:
                try:
                    cb(frame, metadata)
                except Exception as exc:
                    logger.error("Frame callback error: %s", exc)

    # -- Convenience methods matching OrcaFireAcquirer interface --

    def configure(self, config: Dict[str, float]) -> None:
        """Configure camera parameters (OrcaFireAcquirer compatible)."""
        for name, value in config.items():
            if name == "ExposureTime":
                self.set_property(DCAM_IDPROP_EXPOSURETIME, float(value))
            else:
                logger.debug("configure: ignoring %s (use set_param)", name)

    def configure_stream(self, config: Dict[str, object]) -> None:
        """Configure stream parameters (binning via DCAM)."""
        if "BinningMethod" in config:
            method = config["BinningMethod"]
            bin_map = {"Disable": 1, "Mean_2x2": 2, "Sum_2x2": 2,
                       "Mean_4x4": 4, "Sum_4x4": 4}
            binval = bin_map.get(method, 1)
            self.set_property(DCAM_IDPROP_BINNING, float(binval))

    def set_trigger_mode(self, mode: str) -> None:
        """Set trigger mode (OrcaFireAcquirer compatible)."""
        self.set_param("TriggerMode", mode)
        logger.info("Trigger mode: %s", mode)

    def configure_external_trigger(self, source: str = "TTLIO11",
                                   activation: str = "RisingEdge") -> None:
        """Configure external trigger (OrcaFireAcquirer compatible)."""
        self.set_property(DCAM_IDPROP_TRIGGERSOURCE,
                          DCAMPROP_TRIGGERSOURCE__EXTERNAL)
        if activation == "FallingEdge":
            self.set_property(DCAM_IDPROP_TRIGGERPOLARITY,
                              DCAMPROP_TRIGGERPOLARITY__NEGATIVE)
        else:
            self.set_property(DCAM_IDPROP_TRIGGERPOLARITY,
                              DCAMPROP_TRIGGERPOLARITY__POSITIVE)
        logger.info("External trigger: %s", activation)

    def get_node_value(self, name: str) -> Any:
        """Read a GenICam-style node (OrcaFireAcquirer compatible)."""
        prop_map = {"ExposureTime": DCAM_IDPROP_EXPOSURETIME,
                    "Width": DCAM_IDPROP_IMAGE_WIDTH,
                    "Height": DCAM_IDPROP_IMAGE_HEIGHT}
        prop_id = prop_map.get(name)
        if prop_id:
            return self.get_property(prop_id)
        raise AttributeError(f"Unknown node: {name}")

    def is_camera_connected(self) -> bool:
        return self._hdcam is not None
