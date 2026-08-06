"""
Teledyne Photometrics Kinetix via PVCAM SDK (direct ctypes binding).

Mirrors the pattern of the Hamamatsu ORCA Fire backend
(orca_fire_dcam.py), which binds libdcamapi.so directly via ctypes. This
module does the same for libpvcam.so.2 — no pyvcam dependency.

Requires:
    - PVCAM runtime library (libpvcam.so.2) on LD_LIBRARY_PATH, or the
      vendor path
      /home/beams19/USERTXM/epics/synApps/support/ADKinetix/kinetixSupport/os/linux-x86_64/
      shipped with the ADKinetix synApps module.

Constraint:
    PVCAM claims the camera exclusively. Stop the ADKinetix EPICS IOC
    (iocKinetix/softioc/32idKinetix.pl stop) before starting this backend.
"""

import ctypes
import logging
import queue
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from detectors.core.base import BaseCamera, CameraInfo
from detectors.core.registry import register

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PVCAM base type aliases (from master.h)
# ---------------------------------------------------------------------------

rs_bool = ctypes.c_ushort
int8 = ctypes.c_byte
uns8 = ctypes.c_ubyte
int16 = ctypes.c_short
uns16 = ctypes.c_ushort
int32 = ctypes.c_int
uns32 = ctypes.c_uint
long64 = ctypes.c_longlong
ulong64 = ctypes.c_ulonglong
flt32 = ctypes.c_float
flt64 = ctypes.c_double

PV_OK = 1
PV_FAIL = 0
CAM_NAME_LEN = 32
ERROR_MSG_LEN = 255

OPEN_EXCLUSIVE = 0
# PL_CIRC_MODES (pvcam.h): CIRC_NONE=0 (invalid for cont), CIRC_OVERWRITE=1,
# CIRC_NO_OVERWRITE=2. Getting this wrong makes pl_exp_start_cont fail
# with PL_ERR_CONFIGURATION_INVALID even though pl_exp_setup_cont succeeds.
CIRC_NONE = 0
CIRC_OVERWRITE = 1
CIRC_NO_OVERWRITE = 2
CCS_NO_CHANGE = 0
CCS_HALT = 1


# ---------------------------------------------------------------------------
# PARAM_* IDs (verified against pvcam.h)
# ---------------------------------------------------------------------------

PARAM_METADATA_ENABLED = 0x0B0300A8   # CLASS3<<16 | TYPE_BOOLEAN<<24 | 168
PARAM_CAM_INTERFACE_TYPE = 0x0900000A
PARAM_VENDOR_NAME = 0x0D020083
PARAM_PRODUCT_NAME = 0x0D020084
PARAM_CAMERA_PART_NUMBER = 0x0D020085
PARAM_PAR_SIZE = 0x06020039
PARAM_SER_SIZE = 0x0602003A
PARAM_TEMP = 0x0102020D
PARAM_TEMP_SETPOINT = 0x0102020E
PARAM_CAM_FW_VERSION = 0x06020214
PARAM_FAN_SPEED_SETPOINT = 0x090202C6
PARAM_EXPOSE_OUT_MODE = 0x09020230
PARAM_BIT_DEPTH = 0x010201FF
PARAM_IMAGE_FORMAT = 0x090200F8
PARAM_GAIN_INDEX = 0x01020200
PARAM_SPDTAB_INDEX = 0x01020201
PARAM_GAIN_NAME = 0x0D020202
PARAM_READOUT_PORT = 0x090200F7
PARAM_PIX_TIME = 0x06020204
PARAM_SHTR_OPEN_MODE = 0x09020209
PARAM_EXP_RES = 0x09030002
PARAM_EXPOSURE_TIME = 0x08030008
PARAM_BINNING_SER = 0x090300A5
PARAM_BINNING_PAR = 0x090300A6
PARAM_ROI = 0x14030001
PARAM_EXPOSURE_MODE = 0x09020217  # header symbol; a.k.a. "EXP_MODE"

# TYPE_* codes
TYPE_INT16 = 1
TYPE_INT32 = 2
TYPE_FLT64 = 4
TYPE_UNS8 = 5
TYPE_UNS16 = 6
TYPE_UNS32 = 7
TYPE_UNS64 = 8
TYPE_ENUM = 9
TYPE_BOOLEAN = 11
TYPE_INT8 = 12
TYPE_CHAR_PTR = 13
TYPE_INT64 = 16
TYPE_SMART_STREAM_TYPE = 17
TYPE_FLT32 = 19

# ATTR_* codes
ATTR_CURRENT = 0
ATTR_COUNT = 1
ATTR_TYPE = 2
ATTR_MIN = 3
ATTR_MAX = 4
ATTR_DEFAULT = 5
ATTR_INCREMENT = 6
ATTR_ACCESS = 7
ATTR_AVAIL = 8

# PL_CALLBACK_EVENT
PL_CALLBACK_EOF = 1
PL_CALLBACK_CAM_REMOVED = 3

# PVCAM ctypes type table: TYPE_* -> ctypes scalar
_CTYPE_FOR_TYPE = {
    TYPE_INT8: ctypes.c_byte,
    TYPE_UNS8: ctypes.c_ubyte,
    TYPE_INT16: ctypes.c_short,
    TYPE_UNS16: ctypes.c_ushort,
    TYPE_INT32: ctypes.c_int,
    TYPE_UNS32: ctypes.c_uint,
    TYPE_INT64: ctypes.c_longlong,
    TYPE_UNS64: ctypes.c_ulonglong,
    TYPE_FLT32: ctypes.c_float,
    TYPE_FLT64: ctypes.c_double,
    TYPE_BOOLEAN: ctypes.c_ushort,   # rs_bool
    TYPE_ENUM: ctypes.c_int,         # int32
}


# ---------------------------------------------------------------------------
# Trigger-mode values (PL_EXPOSURE_MODES + extended modes, pvcam.h L546-650)
# Extended modes are OR-able with expose-out mode; use them as-is.
# ---------------------------------------------------------------------------

EXPOSURE_MODE = {
    "Internal": 0x0700,       # EXT_TRIG_INTERNAL: free-run, timed
    "External": 0x0900,       # EXT_TRIG_EDGE_RISING: rising-edge triggered
    "ExternalLevel": 0x0A00,  # EXT_TRIG_LEVEL: level-triggered
    "Software": 0x0C00,       # EXT_TRIG_SOFTWARE_EDGE
    "TriggerFirst": 0x0800,   # EXT_TRIG_TRIG_FIRST: external, then internal
    "LevelOverlap": 0x0D00,   # EXT_TRIG_LEVEL_OVERLAP
}
EXPOSURE_MODE_INV = {v: k for k, v in EXPOSURE_MODE.items()}

# PL_EXPOSE_OUT_MODES
EXPOSE_OUT_MODE = {
    "FirstRow": 0,
    "AllRows": 1,
    "AnyRow": 2,
    "Rolling": 3,
    "LineTrigger": 4,
}
EXPOSE_OUT_MODE_INV = {v: k for k, v in EXPOSE_OUT_MODE.items()}

# Simplified alias tables the GUI's ENUM_CHOICES understands
TRIGGER_ACTIVE = {"Edge": 0x0900, "Level": 0x0A00}
TRIGGER_ACTIVE_INV = {v: k for k, v in TRIGGER_ACTIVE.items()}


# ---------------------------------------------------------------------------
# Structs
# ---------------------------------------------------------------------------

class rgn_type(ctypes.Structure):
    """PVCAM ROI: sensor coords + binning (pvcam.h L1184)."""
    _fields_ = [
        ("s1", uns16), ("s2", uns16), ("sbin", uns16),
        ("p1", uns16), ("p2", uns16), ("pbin", uns16),
    ]


class PVCAM_FRAME_INFO_GUID(ctypes.Structure):
    _fields_ = [
        ("f1", uns32), ("f2", uns16), ("f3", uns16),
        ("f4", uns8 * 8),
    ]


class FRAME_INFO(ctypes.Structure):
    """Per-frame metadata delivered to EOF callback (pvcam.h L88)."""
    _fields_ = [
        ("FrameInfoGUID", PVCAM_FRAME_INFO_GUID),
        ("hCam", int16),
        ("FrameNr", int32),
        ("TimeStamp", long64),
        ("ReadoutTime", int32),
        ("TimeStampBOF", long64),
    ]


# PVCAM callback signature: void (*)(FRAME_INFO*, void*)
PVCAM_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.POINTER(FRAME_INFO), ctypes.c_void_p)


# ---------------------------------------------------------------------------
# Module-level PVCAM library lifecycle (refcounted)
# ---------------------------------------------------------------------------

_pvcam_lib = None
_pvcam_init_count = 0
_pvcam_lock = threading.Lock()

_VENDOR_LIB_PATH = (
    "/home/beams19/USERTXM/epics/synApps/support/ADKinetix/"
    "kinetixSupport/os/linux-x86_64/libpvcam.so.2"
)


def _load_pvcam_library() -> ctypes.CDLL:
    """Try LD_LIBRARY_PATH first, then the vendor path shipped with ADKinetix."""
    last_err = None
    for path in ("libpvcam.so.2", "libpvcam.so", _VENDOR_LIB_PATH):
        try:
            return ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
        except OSError as exc:
            last_err = exc
    raise RuntimeError(
        f"Could not load libpvcam.so.2 (last error: {last_err}). "
        f"Set LD_LIBRARY_PATH to include the PVCAM library directory "
        f"or install the vendor library at {_VENDOR_LIB_PATH}")


def _pvcam_init() -> None:
    """Initialize PVCAM library (refcounted, thread-safe)."""
    global _pvcam_lib, _pvcam_init_count
    with _pvcam_lock:
        if _pvcam_lib is None:
            _pvcam_lib = _load_pvcam_library()
            _setup_signatures(_pvcam_lib)
            if _pvcam_lib.pl_pvcam_init() != PV_OK:
                _pvcam_lib = None
                raise RuntimeError(
                    f"pl_pvcam_init failed: {_last_pvcam_error()}")
            logger.info("PVCAM initialized")
        _pvcam_init_count += 1


def _pvcam_uninit() -> None:
    """Release PVCAM library (refcounted)."""
    global _pvcam_lib, _pvcam_init_count
    with _pvcam_lock:
        _pvcam_init_count -= 1
        if _pvcam_init_count <= 0 and _pvcam_lib is not None:
            _pvcam_lib.pl_pvcam_uninit()
            _pvcam_lib = None
            _pvcam_init_count = 0


def _setup_signatures(lib: ctypes.CDLL) -> None:
    lib.pl_pvcam_init.restype = rs_bool
    lib.pl_pvcam_uninit.restype = rs_bool
    lib.pl_cam_get_total.restype = rs_bool
    lib.pl_cam_get_total.argtypes = [ctypes.POINTER(int16)]
    lib.pl_cam_get_name.restype = rs_bool
    lib.pl_cam_get_name.argtypes = [int16, ctypes.c_char_p]
    lib.pl_cam_open.restype = rs_bool
    lib.pl_cam_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(int16), int16]
    lib.pl_cam_close.restype = rs_bool
    lib.pl_cam_close.argtypes = [int16]
    lib.pl_get_param.restype = rs_bool
    lib.pl_get_param.argtypes = [int16, uns32, int16, ctypes.c_void_p]
    lib.pl_set_param.restype = rs_bool
    lib.pl_set_param.argtypes = [int16, uns32, ctypes.c_void_p]
    lib.pl_enum_str_length.restype = rs_bool
    lib.pl_enum_str_length.argtypes = [int16, uns32, uns32, ctypes.POINTER(uns32)]
    lib.pl_get_enum_param.restype = rs_bool
    lib.pl_get_enum_param.argtypes = [
        int16, uns32, uns32, ctypes.POINTER(int32), ctypes.c_char_p, uns32]
    lib.pl_exp_setup_cont.restype = rs_bool
    lib.pl_exp_setup_cont.argtypes = [
        int16, uns16, ctypes.POINTER(rgn_type),
        int16, uns32, ctypes.POINTER(uns32), int16]
    lib.pl_exp_start_cont.restype = rs_bool
    lib.pl_exp_start_cont.argtypes = [int16, ctypes.c_void_p, uns32]
    lib.pl_exp_stop_cont.restype = rs_bool
    lib.pl_exp_stop_cont.argtypes = [int16, int16]
    lib.pl_exp_get_latest_frame.restype = rs_bool
    lib.pl_exp_get_latest_frame.argtypes = [int16, ctypes.POINTER(ctypes.c_void_p)]
    lib.pl_cam_register_callback_ex3.restype = rs_bool
    lib.pl_cam_register_callback_ex3.argtypes = [
        int16, int32, ctypes.c_void_p, ctypes.c_void_p]
    lib.pl_cam_deregister_callback.restype = rs_bool
    lib.pl_cam_deregister_callback.argtypes = [int16, int32]
    lib.pl_error_code.restype = int16
    lib.pl_error_message.restype = rs_bool
    lib.pl_error_message.argtypes = [int16, ctypes.c_char_p]
    lib.pl_pp_reset.restype = rs_bool
    lib.pl_pp_reset.argtypes = [int16]


def _last_pvcam_error() -> str:
    if _pvcam_lib is None:
        return "(pvcam not loaded)"
    code = _pvcam_lib.pl_error_code()
    buf = ctypes.create_string_buffer(ERROR_MSG_LEN + 1)
    _pvcam_lib.pl_error_message(code, buf)
    return f"code={code}, {buf.value.decode(errors='replace')}"


# ---------------------------------------------------------------------------
# Camera backend
# ---------------------------------------------------------------------------

@register
class TeledyneKinetix(BaseCamera):
    """Teledyne Photometrics Kinetix via PVCAM (direct ctypes)."""

    camera_type = "teledyne.kinetix"
    display_name = "Teledyne Photometrics Kinetix (PVCAM)"

    NUM_BUFFERS = 5

    # Standard-name -> (PARAM_ID, TYPE_ hint) for pass-through get/set.
    # TYPE_ hint is a fallback; actual type is queried per-param and cached.
    _STANDARD_TO_PARAM: Dict[str, int] = {
        "ExposureTime": PARAM_EXPOSURE_TIME,      # units depend on EXP_RES
        "SensorTemperature": PARAM_TEMP,
        "SensorTemperatureTarget": PARAM_TEMP_SETPOINT,
        "BinningHorizontal": PARAM_BINNING_SER,
        "BinningVertical": PARAM_BINNING_PAR,
        "ReadoutPortIndex": PARAM_READOUT_PORT,
        "SpeedIndex": PARAM_SPDTAB_INDEX,
        "GainIndex": PARAM_GAIN_INDEX,
        "PixelTime": PARAM_PIX_TIME,
        "BitsPerChannel": PARAM_BIT_DEPTH,
        "ImageFormat": PARAM_IMAGE_FORMAT,
        "ShutterMode": PARAM_SHTR_OPEN_MODE,
        "SensorCoolerFan": PARAM_FAN_SPEED_SETPOINT,
        "ExposureResolution": PARAM_EXP_RES,
        "ExposeOutMode": PARAM_EXPOSE_OUT_MODE,
    }

    def __init__(self, device_index: int = 0, **kwargs):
        super().__init__(device_index=device_index, **kwargs)
        self._hcam: Optional[int] = None
        self._cam_name: bytes = b""
        # Acquisition state
        self._rgn = rgn_type(0, 0, 1, 0, 0, 1)
        self._exp_bytes: int = 0
        self._frame_shape: Tuple[int, int] = (0, 0)
        self._circ_buf: Optional[ctypes.Array] = None
        self._frame_queue: "queue.Queue[Tuple[np.ndarray, dict]]" = queue.Queue(
            maxsize=self.NUM_BUFFERS)
        self._callback_ref: Optional[PVCAM_CALLBACK] = None
        self._last_metadata: dict = {}
        # Per-param TYPE_ cache to avoid re-querying ATTR_TYPE every access
        self._param_type_cache: Dict[int, int] = {}
        # Exposure time and trigger mode are NOT pl_set_param-writable on
        # PVCAM — they're arguments to pl_exp_setup_cont. Cache them here
        # and apply on the next start_acquisition().
        self._exposure_time_units: int = 10000   # populated in open()
        self._exp_mode: int = EXPOSURE_MODE["Internal"]

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def open(self) -> None:
        if self._hcam is not None:
            return
        _pvcam_init()
        try:
            total = int16()
            if _pvcam_lib.pl_cam_get_total(ctypes.byref(total)) != PV_OK:
                raise RuntimeError(
                    f"pl_cam_get_total: {_last_pvcam_error()}")
            if self.device_index >= total.value:
                raise RuntimeError(
                    f"No Kinetix at index {self.device_index} "
                    f"(found {total.value})")

            name = ctypes.create_string_buffer(CAM_NAME_LEN)
            if _pvcam_lib.pl_cam_get_name(self.device_index, name) != PV_OK:
                raise RuntimeError(
                    f"pl_cam_get_name: {_last_pvcam_error()}")
            self._cam_name = name.value

            hcam = int16()
            if _pvcam_lib.pl_cam_open(
                    self._cam_name, ctypes.byref(hcam), OPEN_EXCLUSIVE) != PV_OK:
                raise RuntimeError(
                    f"pl_cam_open({self._cam_name!r}): {_last_pvcam_error()}")
            self._hcam = hcam.value
        except Exception:
            _pvcam_uninit()
            raise

        # Populate CameraInfo
        try:
            ser = int(self._get_param_raw(PARAM_SER_SIZE, ATTR_CURRENT))
            par = int(self._get_param_raw(PARAM_PAR_SIZE, ATTR_CURRENT))
        except Exception as exc:
            logger.warning("Could not read sensor size: %s", exc)
            ser = par = 0

        vendor = self._get_str_param(PARAM_VENDOR_NAME) or "Teledyne"
        model = self._get_str_param(PARAM_PRODUCT_NAME) or "Kinetix"
        serial = self._get_str_param(PARAM_CAMERA_PART_NUMBER) or ""
        fw = self._get_str_param(PARAM_CAM_FW_VERSION) or ""
        try:
            bpc = int(self._get_param_raw(PARAM_BIT_DEPTH, ATTR_CURRENT))
        except Exception:
            bpc = 16

        self._info = CameraInfo(
            vendor=vendor, model=model, serial_number=serial,
            firmware_version=fw,
            sensor_width=ser, sensor_height=par,
            bits_per_pixel=bpc, pixel_size_um=6.5,
            max_frame_rate=500.0,
            supported_binning=[1, 2, 4],
            supported_trigger_modes=["Internal", "External", "Software"],
            has_temperature=True, has_cooler=True, has_subarray=True,
        )

        # Initialize ROI to full frame
        self._rgn = rgn_type(0, ser - 1, 1, 0, par - 1, 1)
        self._frame_shape = (par, ser)

        # Reset any pre-configured post-processing. Without this, stale
        # settings from a previous session (e.g. a prior ADKinetix IOC
        # run) leave the camera in a state where pl_exp_start_cont
        # rejects with PL_ERR_CONFIGURATION_INVALID.
        # Matches ADKinetix.cpp:662.
        try:
            _pvcam_lib.pl_pp_reset(self._hcam)
        except Exception as exc:
            logger.warning("pl_pp_reset failed: %s", exc)

        # Disable frame-embedded metadata. When it's on, PVCAM appends a
        # header to each frame and the buffer must include that overhead;
        # if the buffer is sized without it, pl_exp_start_cont fails
        # (sometimes with PL_ERR_NONE — no error recorded). Explicit off
        # matches the C++ driver's assumption.
        try:
            if self._is_param_available(PARAM_METADATA_ENABLED):
                self._set_param_raw(PARAM_METADATA_ENABLED, 0)
        except Exception as exc:
            logger.debug("PARAM_METADATA_ENABLED: %s", exc)

        # Force microsecond exposure resolution so the IOC's default
        # AcquireTime=0.01 s doesn't round to 0 (PARAM_EXP_RES defaults
        # to seconds on many Kinetix units). The ADKinetix C++ driver
        # does the same on open (ADKinetix.cpp:1050-1063).
        try:
            self._set_param_raw(PARAM_EXP_RES, 1)  # EXP_RES_ONE_MICROSEC
        except Exception as exc:
            logger.warning("Could not set EXP_RES=us: %s", exc)

        # Select a valid (port, speed, gain) combination. Without this,
        # pl_exp_start_cont fails with PL_ERR_CONFIGURATION_INVALID because
        # the camera has no readout mode selected after open. Matches
        # ADKinetix.cpp:222-243 which calls applyReadoutMode() with saved
        # user indices.
        try:
            ports = self._get_enum_values(PARAM_READOUT_PORT)
            if ports:
                port_value = ports[0][0]  # first port's enum value
                self._set_param_raw(PARAM_READOUT_PORT, port_value)
                self._set_param_raw(PARAM_SPDTAB_INDEX, 0)  # fastest speed
                # PVCAM gain indices are 1-based
                n_gain = int(self._get_param_raw(PARAM_GAIN_INDEX, ATTR_COUNT))
                self._set_param_raw(PARAM_GAIN_INDEX, min(1, n_gain))
                logger.info("Default readout: port=%d ('%s'), speed=0, gain=1",
                            port_value, ports[0][1])
        except Exception as exc:
            logger.warning("Could not set default readout mode: %s", exc)

        # Default trigger and exposure — cached in Python and passed to
        # pl_exp_setup_cont at start_acquisition. PVCAM refuses
        # pl_set_param on PARAM_EXPOSURE_MODE / PARAM_EXPOSURE_TIME
        # (PL_ERR_ACCESS_DENIED); the C++ driver does the same thing.
        self._exp_mode = EXPOSURE_MODE["Internal"]
        # Default 10 ms exposure in current EXP_RES units
        self._exposure_time_units = int(round(0.010 / self._exposure_scale_to_seconds()))

        # Register EOF callback ONCE for the camera's lifetime — matches
        # the C++ driver (ADKinetix.cpp:676). Registering/deregistering
        # per acquisition works on some PVCAM versions and fails silently
        # (returning PV_FAIL with PL_ERR_NONE) on others. Keep the
        # CFUNCTYPE reference alive so it isn't GC'd.
        self._callback_ref = PVCAM_CALLBACK(self._on_eof)
        cb_ok = _pvcam_lib.pl_cam_register_callback_ex3(
            self._hcam, PL_CALLBACK_EOF,
            ctypes.cast(self._callback_ref, ctypes.c_void_p), None)
        if cb_ok != PV_OK:
            logger.warning("pl_cam_register_callback_ex3 failed: %s",
                           _last_pvcam_error())
        else:
            logger.info("EOF callback registered")

        logger.info("Opened %s SN=%s (%dx%d, %d-bit)",
                    self.display_name, serial, ser, par, bpc)

    def close(self) -> None:
        if self._is_acquiring:
            try:
                self.stop_acquisition()
            except Exception:
                pass
        if self._hcam is not None:
            try:
                _pvcam_lib.pl_cam_deregister_callback(
                    self._hcam, PL_CALLBACK_EOF)
            except Exception:
                pass
            try:
                _pvcam_lib.pl_cam_close(self._hcam)
            except Exception:
                pass
            self._hcam = None
        self._callback_ref = None
        self._info = None
        _pvcam_uninit()

    # -----------------------------------------------------------------------
    # Raw parameter access
    # -----------------------------------------------------------------------

    def _query_param_type(self, param_id: int) -> int:
        """Query ATTR_TYPE for a param (cached)."""
        cached = self._param_type_cache.get(param_id)
        if cached is not None:
            return cached
        t = uns16()
        if _pvcam_lib.pl_get_param(
                self._hcam, param_id, ATTR_TYPE, ctypes.byref(t)) != PV_OK:
            raise RuntimeError(
                f"ATTR_TYPE for 0x{param_id:08X}: {_last_pvcam_error()}")
        self._param_type_cache[param_id] = int(t.value)
        return int(t.value)

    def _is_param_available(self, param_id: int) -> bool:
        avail = rs_bool()
        if _pvcam_lib.pl_get_param(
                self._hcam, param_id, ATTR_AVAIL, ctypes.byref(avail)) != PV_OK:
            return False
        return bool(avail.value)

    def _get_param_raw(self, param_id: int, attr: int) -> Any:
        """Get a numeric parameter attribute, decoded by its PVCAM type."""
        type_id = self._query_param_type(param_id)
        ctype = _CTYPE_FOR_TYPE.get(type_id)
        if ctype is None:
            raise RuntimeError(
                f"Numeric access to type {type_id} unsupported "
                f"(param 0x{param_id:08X})")
        val = ctype()
        if _pvcam_lib.pl_get_param(
                self._hcam, param_id, attr, ctypes.byref(val)) != PV_OK:
            raise RuntimeError(
                f"pl_get_param 0x{param_id:08X} attr={attr}: "
                f"{_last_pvcam_error()}")
        return val.value

    def _set_param_raw(self, param_id: int, value: Any) -> None:
        type_id = self._query_param_type(param_id)
        ctype = _CTYPE_FOR_TYPE.get(type_id)
        if ctype is None:
            raise RuntimeError(
                f"Numeric set for type {type_id} unsupported "
                f"(param 0x{param_id:08X})")
        val = ctype(value)
        if _pvcam_lib.pl_set_param(
                self._hcam, param_id, ctypes.byref(val)) != PV_OK:
            raise RuntimeError(
                f"pl_set_param 0x{param_id:08X}={value}: "
                f"{_last_pvcam_error()}")

    def _get_str_param(self, param_id: int) -> str:
        """Read a TYPE_CHAR_PTR parameter."""
        if not self._is_param_available(param_id):
            return ""
        buf = ctypes.create_string_buffer(256)
        if _pvcam_lib.pl_get_param(
                self._hcam, param_id, ATTR_CURRENT, buf) != PV_OK:
            return ""
        return buf.value.decode(errors="replace")

    def _get_enum_values(self, param_id: int) -> List[Tuple[int, str]]:
        """Enumerate all (int_value, description) pairs of a TYPE_ENUM param."""
        count = uns32()
        if _pvcam_lib.pl_get_param(
                self._hcam, param_id, ATTR_COUNT, ctypes.byref(count)) != PV_OK:
            raise RuntimeError(
                f"ATTR_COUNT: {_last_pvcam_error()}")
        results: List[Tuple[int, str]] = []
        for idx in range(count.value):
            length = uns32()
            if _pvcam_lib.pl_enum_str_length(
                    self._hcam, param_id, idx, ctypes.byref(length)) != PV_OK:
                continue
            buf = ctypes.create_string_buffer(length.value + 1)
            v = int32()
            if _pvcam_lib.pl_get_enum_param(
                    self._hcam, param_id, idx,
                    ctypes.byref(v), buf, length.value + 1) != PV_OK:
                continue
            results.append((int(v.value), buf.value.decode(errors="replace")))
        return results

    # -----------------------------------------------------------------------
    # Standard-name parameter interface
    # -----------------------------------------------------------------------

    def list_params(self) -> List[str]:
        """Return names of standard params this camera reports available."""
        base = ["ExposureTime", "Width", "Height", "OffsetX", "OffsetY",
                "BinningHorizontal", "BinningVertical", "PixelFormat",
                "TriggerMode", "TriggerSource", "TriggerActive",
                "AcquisitionFrameRate"]
        for std_name, pid in self._STANDARD_TO_PARAM.items():
            if std_name in base:
                continue
            try:
                if self._is_param_available(pid):
                    base.append(std_name)
            except Exception:
                pass
        return base

    def get_param(self, name: str) -> Any:
        # Special (composed / derived) params
        if name == "ExposureTime":
            return self._get_exposure_time_seconds()
        if name == "Width":
            return int(self._rgn.s2 - self._rgn.s1 + 1) // max(1, self._rgn.sbin)
        if name == "Height":
            return int(self._rgn.p2 - self._rgn.p1 + 1) // max(1, self._rgn.pbin)
        if name == "OffsetX":
            return int(self._rgn.s1)
        if name == "OffsetY":
            return int(self._rgn.p1)
        if name == "PixelFormat":
            return self._pixel_format_string()
        if name == "TriggerMode":
            src = self.get_param("TriggerSource")
            return "On" if src == "External" else "Off"
        if name == "TriggerSource":
            label = EXPOSURE_MODE_INV.get(self._exp_mode, "Internal")
            if label in ("ExternalLevel", "TriggerFirst", "LevelOverlap"):
                return "External"
            return label if label in ("Internal", "External", "Software") else "Internal"
        if name == "TriggerActive":
            return "Level" if self._exp_mode == EXPOSURE_MODE["ExternalLevel"] else "Edge"
        if name == "TriggerPolarity":
            return "Positive"  # PVCAM triggers are edge-rising by default
        if name == "AcquisitionFrameRate":
            return self._compute_frame_rate()
        if name == "SensorCoolerStatus":
            return "Ready" if self._info and self._info.has_cooler else "Off"

        # Direct map
        pid = self._STANDARD_TO_PARAM.get(name)
        if pid is None:
            raise KeyError(f"Unknown param: {name}")
        return self._get_param_raw(pid, ATTR_CURRENT)

    def set_param(self, name: str, value: Any) -> None:
        # Special
        if name == "ExposureTime":
            self._set_exposure_time_seconds(float(value))
            return
        if name in ("BinningHorizontal", "BinningVertical"):
            # ROI carries binning too. Update both the parameter and the
            # cached rgn so start_acquisition uses the new value.
            self._require_idle(name)
            try:
                self._set_param_raw(self._STANDARD_TO_PARAM[name], int(value))
            except Exception:
                pass  # some cameras only accept binning via rgn_type
            if name == "BinningHorizontal":
                self._rgn.sbin = int(value)
            else:
                self._rgn.pbin = int(value)
            return
        if name in ("Width", "Height", "OffsetX", "OffsetY"):
            self._require_idle(name)
            r = self._rgn
            if name == "Width":
                r.s2 = int(r.s1 + int(value) * r.sbin - 1)
            elif name == "Height":
                r.p2 = int(r.p1 + int(value) * r.pbin - 1)
            elif name == "OffsetX":
                w = r.s2 - r.s1
                r.s1 = int(value)
                r.s2 = r.s1 + w
            elif name == "OffsetY":
                h = r.p2 - r.p1
                r.p1 = int(value)
                r.p2 = r.p1 + h
            return
        if name == "TriggerMode":
            # areaDetector "Off" -> Internal, "On" -> External
            self._exp_mode = EXPOSURE_MODE[
                "Internal" if str(value) == "Off" else "External"]
            if self._is_acquiring:
                self.stop_acquisition()
                self.start_acquisition()
            return
        if name == "TriggerSource":
            v = EXPOSURE_MODE.get(str(value))
            if v is None:
                raise ValueError(
                    f"Invalid TriggerSource {value!r}. "
                    f"Allowed: {list(EXPOSURE_MODE)}")
            self._exp_mode = v
            if self._is_acquiring:
                self.stop_acquisition()
                self.start_acquisition()
            return
        if name == "TriggerActive":
            v = TRIGGER_ACTIVE.get(str(value))
            if v is None:
                raise ValueError(
                    f"Invalid TriggerActive {value!r}. "
                    f"Allowed: {list(TRIGGER_ACTIVE)}")
            self._exp_mode = v
            if self._is_acquiring:
                self.stop_acquisition()
                self.start_acquisition()
            return
        if name in ("TriggerPolarity", "TriggerDelay",
                    "SensorCooler", "SensorCoolerStatus"):
            # Not directly controllable via PVCAM on the Kinetix; silently
            # accept so the GUI/IOC don't error out.
            return

        pid = self._STANDARD_TO_PARAM.get(name)
        if pid is None:
            raise KeyError(f"Unknown param: {name}")
        # PVCAM requires idle for readout-affecting params
        if name in ("ReadoutPortIndex", "SpeedIndex", "GainIndex",
                    "ImageFormat", "ExposureResolution"):
            self._require_idle(name)
        self._set_param_raw(pid, value)

    def is_param_writable(self, name: str) -> bool:
        if name in ("SensorTemperature", "PixelTime", "BitsPerChannel",
                    "SensorCoolerStatus", "AcquisitionFrameRate",
                    "Width", "Height", "PixelFormat"):
            return False
        return self.is_param_supported(name)

    # -----------------------------------------------------------------------
    # Convenience: ROI, speed table
    # -----------------------------------------------------------------------

    def set_roi(self, hsize: int, vsize: int,
                hpos: Optional[int] = None, vpos: Optional[int] = None,
                hbin: int = 1, vbin: int = 1) -> None:
        """Set a (centered by default) ROI. Takes effect on next start."""
        if self._info is None:
            raise RuntimeError("Camera not open")
        sw, sh = self._info.sensor_width, self._info.sensor_height
        if hpos is None:
            hpos = max(0, (sw - hsize) // 2)
        if vpos is None:
            vpos = max(0, (sh - vsize) // 2)
        was = self._is_acquiring
        if was:
            self.stop_acquisition()
        try:
            self._rgn = rgn_type(hpos, hpos + hsize - 1, hbin,
                                 vpos, vpos + vsize - 1, vbin)
            logger.info("ROI: %dx%d at (%d,%d) bin=(%d,%d)",
                        hsize, vsize, hpos, vpos, hbin, vbin)
        finally:
            if was:
                self.start_acquisition()

    def set_speed_table(self, port_idx: int, speed_idx: int,
                        gain_idx: int) -> None:
        """Configure readout port / speed / gain (must be set in this order)."""
        was = self._is_acquiring
        if was:
            self.stop_acquisition()
        try:
            self._set_param_raw(PARAM_READOUT_PORT, port_idx)
            self._set_param_raw(PARAM_SPDTAB_INDEX, speed_idx)
            self._set_param_raw(PARAM_GAIN_INDEX, gain_idx)
            logger.info("Speed table: port=%d, speed=%d, gain=%d",
                        port_idx, speed_idx, gain_idx)
        finally:
            if was:
                self.start_acquisition()

    def list_speed_table(self) -> List[dict]:
        """Enumerate all (port, speed, gain) combinations available."""
        out: List[dict] = []
        for port_val, port_desc in self._get_enum_values(PARAM_READOUT_PORT):
            self._set_param_raw(PARAM_READOUT_PORT, port_val)
            try:
                n_speed = int(self._get_param_raw(PARAM_SPDTAB_INDEX, ATTR_COUNT))
            except Exception:
                n_speed = 0
            for si in range(n_speed):
                self._set_param_raw(PARAM_SPDTAB_INDEX, si)
                try:
                    n_gain = int(self._get_param_raw(PARAM_GAIN_INDEX, ATTR_COUNT))
                    pix_ns = int(self._get_param_raw(PARAM_PIX_TIME, ATTR_CURRENT))
                except Exception:
                    n_gain = 0
                    pix_ns = 0
                for gi in range(1, n_gain + 1):
                    self._set_param_raw(PARAM_GAIN_INDEX, gi)
                    try:
                        bpc = int(self._get_param_raw(
                            PARAM_BIT_DEPTH, ATTR_CURRENT))
                    except Exception:
                        bpc = 16
                    out.append({
                        "port_index": port_val, "port_name": port_desc,
                        "speed_index": si, "pixel_time_ns": pix_ns,
                        "gain_index": gi, "bit_depth": bpc,
                    })
        return out

    # -----------------------------------------------------------------------
    # Exposure time helpers (units depend on PARAM_EXP_RES)
    # -----------------------------------------------------------------------

    def _exposure_scale_to_seconds(self) -> float:
        """PARAM_EXP_RES: 0=ms, 1=us, 2=s. Convert stored int to seconds."""
        try:
            res = int(self._get_param_raw(PARAM_EXP_RES, ATTR_CURRENT))
        except Exception:
            res = 0  # milliseconds default
        return {0: 1e-3, 1: 1e-6, 2: 1.0}.get(res, 1e-3)

    def _get_exposure_time_seconds(self) -> float:
        return self._exposure_time_units * self._exposure_scale_to_seconds()

    def _set_exposure_time_seconds(self, seconds: float) -> None:
        scale = self._exposure_scale_to_seconds()
        # PVCAM refuses pl_set_param on PARAM_EXPOSURE_TIME. The value is
        # passed to pl_exp_setup_cont at start_acquisition. Store it here.
        self._exposure_time_units = max(1, int(round(seconds / scale)))
        # If already acquiring, restart to pick up the new exposure.
        if self._is_acquiring:
            self.stop_acquisition()
            self.start_acquisition()

    def _compute_frame_rate(self) -> float:
        """Approximate frame rate from exposure + readout time."""
        try:
            exp = self._get_exposure_time_seconds()
            pix_ns = int(self._get_param_raw(PARAM_PIX_TIME, ATTR_CURRENT))
            h = self._frame_shape[0] or 1
            # Rolling shutter approximation: readout ≈ height * pix_time
            readout = (h * pix_ns) * 1e-9
            return 1.0 / max(exp + readout, 1e-9)
        except Exception:
            return 0.0

    def _pixel_format_string(self) -> str:
        try:
            bpc = int(self._get_param_raw(PARAM_BIT_DEPTH, ATTR_CURRENT))
        except Exception:
            return "Mono16"
        if bpc <= 8:
            return "Mono8"
        if bpc <= 12:
            return "Mono12"
        return "Mono16"

    def _require_idle(self, name: str) -> None:
        """Some PVCAM params need acquisition stopped to write."""
        if self._is_acquiring:
            logger.info("Stopping acquisition to change %s", name)
            self.stop_acquisition()

    # -----------------------------------------------------------------------
    # Acquisition — callback-to-queue bridge
    # -----------------------------------------------------------------------

    def start_acquisition(self) -> None:
        if self._is_acquiring:
            return

        # Compute frame shape from the current ROI (post-binning)
        width = (self._rgn.s2 - self._rgn.s1 + 1) // max(1, self._rgn.sbin)
        height = (self._rgn.p2 - self._rgn.p1 + 1) // max(1, self._rgn.pbin)
        self._frame_shape = (int(height), int(width))

        # exp_mode = trigger mode OR'd with expose-out mode. The Kinetix
        # requires a valid expose-out selection; EXPOSE_OUT_FIRST_ROW is
        # the safest default (matches ADKinetix.cpp:890,892,894).
        combined_mode = self._exp_mode | EXPOSE_OUT_MODE["FirstRow"]

        exp_bytes = uns32()
        if _pvcam_lib.pl_exp_setup_cont(
                self._hcam, 1, ctypes.byref(self._rgn),
                int16(combined_mode), uns32(self._exposure_time_units),
                ctypes.byref(exp_bytes), int16(CIRC_OVERWRITE)) != PV_OK:
            raise RuntimeError(f"pl_exp_setup_cont: {_last_pvcam_error()}")
        self._exp_bytes = int(exp_bytes.value)

        buf_bytes = self._exp_bytes * self.NUM_BUFFERS
        # Use numpy for the circular buffer — page-aligned by default,
        # which the pvcam_pcie kernel driver needs for direct DMA mapping
        # (see `dmesg | grep pvcam` — "mapping user buffer directly for DMA").
        self._circ_buf = np.zeros(buf_bytes, dtype=np.uint8)
        buf_ptr = self._circ_buf.ctypes.data_as(ctypes.c_void_p)

        # Drain any stale frames from a previous run
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

        logger.info("start_acquisition: %dx%d, exp_mode=0x%X, exp_time=%d, "
                    "%d bytes/frame, %d buffers, total=%d bytes, buf_addr=0x%X",
                    width, height, self._exp_mode, self._exposure_time_units,
                    self._exp_bytes, self.NUM_BUFFERS, buf_bytes,
                    buf_ptr.value or 0)

        if _pvcam_lib.pl_exp_start_cont(
                self._hcam, buf_ptr, uns32(buf_bytes)) != PV_OK:
            self._circ_buf = None
            raise RuntimeError(
                f"pl_exp_start_cont: {_last_pvcam_error()}")

        self._is_acquiring = True
        logger.info("Acquisition started")

    def stop_acquisition(self) -> None:
        if not self._is_acquiring:
            return
        try:
            _pvcam_lib.pl_exp_stop_cont(self._hcam, int16(CCS_HALT))
        except Exception:
            pass
        # NOTE: callback stays registered for the camera's lifetime.
        self._circ_buf = None
        self._is_acquiring = False
        logger.info("Acquisition stopped")

    def acquire_frame(self, timeout_ms: int = 5000) -> np.ndarray:
        if not self._is_acquiring:
            raise RuntimeError("Acquisition not running")
        try:
            frame, meta = self._frame_queue.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            raise RuntimeError(f"acquire_frame timed out after {timeout_ms} ms")
        self._last_metadata = meta
        return frame

    def _on_eof(self, p_frame_info, p_context) -> None:
        """PVCAM EOF callback (runs on PVCAM's thread). Never block here."""
        # Diagnostic: prove the callback is firing. Remove once verified.
        self._cb_fire_count = getattr(self, "_cb_fire_count", 0) + 1
        if self._cb_fire_count <= 3 or self._cb_fire_count % 100 == 0:
            logger.info("EOF fired #%d", self._cb_fire_count)
        try:
            ptr = ctypes.c_void_p()
            if _pvcam_lib.pl_exp_get_latest_frame(
                    self._hcam, ctypes.byref(ptr)) != PV_OK or not ptr.value:
                return
            h, w = self._frame_shape
            n_pixels = h * w
            # Assume 16-bit pixel data (Kinetix default). Bit-depth-8 path
            # would need a branch on PARAM_IMAGE_FORMAT, but PixelFormat
            # reports Mono16 in that case too since PVCAM packs 8-bit as
            # uint16 unless the app opts into a packed format.
            buf_type = ctypes.c_uint16 * n_pixels
            src = buf_type.from_address(ptr.value)
            arr = np.frombuffer(src, dtype=np.uint16).reshape((h, w)).copy()

            metadata = {}
            if p_frame_info:
                fi = p_frame_info.contents
                metadata = {
                    "frame_number": int(fi.FrameNr),
                    "timestamp": int(fi.TimeStamp),
                    "readout_time": int(fi.ReadoutTime),
                }
            # Drop-oldest on backlog — never block PVCAM's thread.
            if self._frame_queue.full():
                try:
                    self._frame_queue.get_nowait()
                except queue.Empty:
                    pass
            try:
                self._frame_queue.put_nowait((arr, metadata))
            except queue.Full:
                pass
            # Notify base-class frame callbacks (HDF5 sinks, etc.)
            try:
                self._notify_frame(arr, metadata)
            except Exception:
                pass
        except Exception:
            logger.exception("PVCAM EOF callback failed")
