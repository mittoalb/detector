"""
Teledyne FLIR Oryx via Spinnaker C API (direct ctypes binding).

Mirrors the pattern of the Kinetix backend (kinetix.py), which binds
libpvcam.so.2 directly via ctypes. This module does the same for
libSpinnaker_C.so.2 — no PySpin dependency, works on Python 3.13.

Requires:
    - Spinnaker C API library (libSpinnaker_C.so.2) on LD_LIBRARY_PATH, or
      the vendor path shipped with ADSpinnaker at
      /APSshare/epics/synApps_6_2_1/support/areaDetector-R3-12-1/
      ADSpinnaker/spinnakerSupport/os/linux-x86_64/

Constraint:
    Spinnaker claims the camera exclusively per process. Stop the
    ADSpinnaker EPICS IOC (32idbSP1) before starting this backend.
"""

import ctypes
import logging
import threading
import time
from ctypes import byref, c_char_p, c_double, c_int, c_int64, c_size_t, \
    c_uint8, c_void_p, CFUNCTYPE
from typing import Any, Dict, List, Optional

import numpy as np

from detectors.core.base import BaseCamera, CameraInfo
from detectors.core.registry import register

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spinnaker C API type aliases (SpinnakerDefsC.h, SpinnakerGenApiDefsC.h)
# ---------------------------------------------------------------------------

spinError = c_int
bool8_t = c_uint8

# All handles are void* under the hood
spinSystem = c_void_p
spinCameraList = c_void_p
spinCamera = c_void_p
spinImage = c_void_p
spinImageEventHandler = c_void_p
spinNodeMapHandle = c_void_p
spinNodeHandle = c_void_p

# Image event callback: void (*)(spinImage hImage, void* pUserData)
spinImageEventFunction = CFUNCTYPE(None, c_void_p, c_void_p)

SPINNAKER_ERR_SUCCESS = 0
MAX_BUF_LEN = 256

# spinNodeType (SpinnakerGenApiDefsC.h L71-86) — returned by spinNodeGetType.
# NOT the same as spinInterfaceType (which is 208-222 in the same header);
# older Spinnaker C libs only export the former.
INTF_IVALUE = 0
INTF_IBASE = 1
INTF_IINTEGER = 2
INTF_IBOOLEAN = 3
INTF_IFLOAT = 4
INTF_ICOMMAND = 5
INTF_ISTRING = 6
INTF_IREGISTER = 7
INTF_IENUMERATION = 8
INTF_IENUMENTRY = 9
INTF_ICATEGORY = 10
INTF_IPORT = 11

# Interfaces we can read/write as a scalar parameter
_SCALAR_INTFS = frozenset({INTF_IINTEGER, INTF_IBOOLEAN, INTF_IFLOAT,
                             INTF_ISTRING, INTF_IENUMERATION, INTF_ICOMMAND})


# ---------------------------------------------------------------------------
# Refcounted library load / lifecycle (thread-safe)
# ---------------------------------------------------------------------------

_spin_lib: Optional[ctypes.CDLL] = None
_spin_system: Optional[int] = None
_spin_init_count = 0
_spin_lock = threading.Lock()

_VENDOR_LIB_DIR = (
    "/APSshare/epics/synApps_6_2_1/support/areaDetector-R3-12-1/"
    "ADSpinnaker/spinnakerSupport/os/linux-x86_64"
)
_VENDOR_LIB_PATH = f"{_VENDOR_LIB_DIR}/libSpinnaker_C.so.2"


def _load_spinnaker_library() -> ctypes.CDLL:
    """Try LD_LIBRARY_PATH first, then the vendor path shipped with ADSpinnaker.

    libSpinnaker_C.so.2 depends on libSpinnaker.so.2 which in turn depends
    on GenApi/GCBase/Log/MathParser/NodeMapData/XmlParser — all sitting in
    the same vendor dir but not on any default search path. Pre-load them
    ourselves (RTLD_GLOBAL) in dependency order so the linker resolves each
    subsequent DT_NEEDED entry from an already-loaded image. This removes
    the LD_LIBRARY_PATH requirement entirely.
    """
    last_err = None
    # Fast path: whatever's on LD_LIBRARY_PATH / ldconfig
    for path in ("libSpinnaker_C.so.2", "libSpinnaker_C.so"):
        try:
            return ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
        except OSError as exc:
            last_err = exc
    # Vendor path: pre-load transitive C++ deps in dependency order.
    import os
    deps = [
        "libLog_gcc540_v3_0.so",
        "libMathParser_gcc540_v3_0.so",
        "libXmlParser_gcc540_v3_0.so",
        "libNodeMapData_gcc540_v3_0.so",
        "libGCBase_gcc540_v3_0.so",
        "libGenApi_gcc540_v3_0.so",
        "libSpinnaker.so.2",
    ]
    for dep in deps:
        p = os.path.join(_VENDOR_LIB_DIR, dep)
        if os.path.exists(p):
            try:
                ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
            except OSError as exc:
                last_err = exc
    try:
        return ctypes.CDLL(_VENDOR_LIB_PATH, mode=ctypes.RTLD_GLOBAL)
    except OSError as exc:
        last_err = exc
    raise RuntimeError(
        f"Could not load libSpinnaker_C.so.2 (last error: {last_err}). "
        f"Set LD_LIBRARY_PATH to include the Spinnaker library directory "
        f"or install the vendor library at {_VENDOR_LIB_PATH}")


def _spin_init() -> spinSystem:
    """Initialize Spinnaker (refcounted). Returns the shared spinSystem handle."""
    global _spin_lib, _spin_system, _spin_init_count
    with _spin_lock:
        if _spin_lib is None:
            _spin_lib = _load_spinnaker_library()
            _setup_signatures(_spin_lib)
            h = spinSystem()
            err = _spin_lib.spinSystemGetInstance(byref(h))
            if err != SPINNAKER_ERR_SUCCESS:
                _spin_lib = None
                raise RuntimeError(f"spinSystemGetInstance failed (code={err})")
            _spin_system = h.value
            logger.info("Spinnaker system acquired")
        _spin_init_count += 1
        return spinSystem(_spin_system)


def _spin_uninit() -> None:
    """Release Spinnaker (refcounted)."""
    global _spin_lib, _spin_system, _spin_init_count
    with _spin_lock:
        _spin_init_count -= 1
        if _spin_init_count <= 0 and _spin_lib is not None:
            try:
                _spin_lib.spinSystemReleaseInstance(spinSystem(_spin_system))
            except Exception:
                pass
            _spin_lib = None
            _spin_system = None
            _spin_init_count = 0


def _setup_signatures(lib: ctypes.CDLL) -> None:
    # Every function returns spinError; only argtypes vary
    for fn in ("spinSystemGetInstance", "spinSystemReleaseInstance",
               "spinSystemGetCameras", "spinCameraListCreateEmpty",
               "spinCameraListDestroy", "spinCameraListGetSize",
               "spinCameraListGet", "spinCameraListGetBySerial",
               "spinCameraListClear", "spinCameraRelease",
               "spinCameraInit", "spinCameraDeInit",
               "spinCameraGetNodeMap", "spinCameraGetTLStreamNodeMap",
               "spinNodeMapGetNode",
               "spinNodeMapGetNumNodes", "spinNodeMapGetNodeByIndex",
               "spinNodeGetName", "spinNodeGetType",
               "spinNodeIsAvailable", "spinNodeIsReadable", "spinNodeIsWritable",
               "spinFloatGetValue", "spinFloatSetValue",
               "spinIntegerGetValue", "spinIntegerSetValue",
               "spinEnumerationGetCurrentEntry",
               "spinEnumerationGetEntryByName",
               "spinEnumerationSetIntValue",
               "spinEnumerationEntryGetIntValue",
               "spinEnumerationEntryGetSymbolic",
               "spinStringGetValue", "spinCommandExecute",
               "spinCameraBeginAcquisition", "spinCameraEndAcquisition",
               "spinCameraGetNextImageEx",
               "spinImageGetData", "spinImageGetWidth", "spinImageGetHeight",
               "spinImageIsIncomplete", "spinImageGetPixelFormat",
               "spinImageRelease",
               "spinImageEventHandlerCreate", "spinImageEventHandlerDestroy",
               "spinCameraRegisterImageEventHandler",
               "spinCameraUnregisterImageEventHandler"):
        getattr(lib, fn).restype = spinError

    lib.spinSystemGetInstance.argtypes = [ctypes.POINTER(spinSystem)]
    lib.spinSystemReleaseInstance.argtypes = [spinSystem]
    lib.spinSystemGetCameras.argtypes = [spinSystem, spinCameraList]

    lib.spinCameraListCreateEmpty.argtypes = [ctypes.POINTER(spinCameraList)]
    lib.spinCameraListDestroy.argtypes = [spinCameraList]
    lib.spinCameraListGetSize.argtypes = [spinCameraList, ctypes.POINTER(c_size_t)]
    lib.spinCameraListGet.argtypes = [spinCameraList, c_size_t,
                                       ctypes.POINTER(spinCamera)]
    lib.spinCameraListGetBySerial.argtypes = [spinCameraList, c_char_p,
                                                ctypes.POINTER(spinCamera)]
    lib.spinCameraListClear.argtypes = [spinCameraList]
    lib.spinCameraRelease.argtypes = [spinCamera]

    lib.spinCameraInit.argtypes = [spinCamera]
    lib.spinCameraDeInit.argtypes = [spinCamera]
    lib.spinCameraGetNodeMap.argtypes = [spinCamera,
                                          ctypes.POINTER(spinNodeMapHandle)]
    lib.spinCameraGetTLStreamNodeMap.argtypes = [spinCamera,
                                                   ctypes.POINTER(spinNodeMapHandle)]

    lib.spinNodeMapGetNode.argtypes = [spinNodeMapHandle, c_char_p,
                                        ctypes.POINTER(spinNodeHandle)]
    lib.spinNodeMapGetNumNodes.argtypes = [spinNodeMapHandle,
                                             ctypes.POINTER(c_size_t)]
    lib.spinNodeMapGetNodeByIndex.argtypes = [spinNodeMapHandle, c_size_t,
                                                ctypes.POINTER(spinNodeHandle)]
    lib.spinNodeGetName.argtypes = [spinNodeHandle, c_char_p,
                                     ctypes.POINTER(c_size_t)]
    lib.spinNodeGetType.argtypes = [spinNodeHandle,
                                                        ctypes.POINTER(c_int)]
    lib.spinNodeIsAvailable.argtypes = [spinNodeHandle, ctypes.POINTER(bool8_t)]
    lib.spinNodeIsReadable.argtypes = [spinNodeHandle, ctypes.POINTER(bool8_t)]
    lib.spinNodeIsWritable.argtypes = [spinNodeHandle, ctypes.POINTER(bool8_t)]

    lib.spinFloatGetValue.argtypes = [spinNodeHandle, ctypes.POINTER(c_double)]
    lib.spinFloatSetValue.argtypes = [spinNodeHandle, c_double]
    lib.spinIntegerGetValue.argtypes = [spinNodeHandle, ctypes.POINTER(c_int64)]
    lib.spinIntegerSetValue.argtypes = [spinNodeHandle, c_int64]

    lib.spinEnumerationGetCurrentEntry.argtypes = [spinNodeHandle,
                                                    ctypes.POINTER(spinNodeHandle)]
    lib.spinEnumerationGetEntryByName.argtypes = [spinNodeHandle, c_char_p,
                                                    ctypes.POINTER(spinNodeHandle)]
    lib.spinEnumerationSetIntValue.argtypes = [spinNodeHandle, c_int64]
    lib.spinEnumerationEntryGetIntValue.argtypes = [spinNodeHandle,
                                                     ctypes.POINTER(c_int64)]
    lib.spinEnumerationEntryGetSymbolic.argtypes = [spinNodeHandle, c_char_p,
                                                     ctypes.POINTER(c_size_t)]
    lib.spinStringGetValue.argtypes = [spinNodeHandle, c_char_p,
                                        ctypes.POINTER(c_size_t)]
    lib.spinCommandExecute.argtypes = [spinNodeHandle]

    lib.spinCameraBeginAcquisition.argtypes = [spinCamera]
    lib.spinCameraEndAcquisition.argtypes = [spinCamera]
    lib.spinCameraGetNextImageEx.argtypes = [spinCamera, ctypes.c_uint64,
                                              ctypes.POINTER(spinImage)]
    lib.spinImageGetData.argtypes = [spinImage, ctypes.POINTER(c_void_p)]
    lib.spinImageGetWidth.argtypes = [spinImage, ctypes.POINTER(c_size_t)]
    lib.spinImageGetHeight.argtypes = [spinImage, ctypes.POINTER(c_size_t)]
    lib.spinImageIsIncomplete.argtypes = [spinImage, ctypes.POINTER(bool8_t)]
    lib.spinImageGetPixelFormat.argtypes = [spinImage, ctypes.POINTER(c_int)]
    lib.spinImageRelease.argtypes = [spinImage]

    lib.spinImageEventHandlerCreate.argtypes = [
        ctypes.POINTER(spinImageEventHandler),
        spinImageEventFunction, c_void_p]
    lib.spinImageEventHandlerDestroy.argtypes = [spinImageEventHandler]
    lib.spinCameraRegisterImageEventHandler.argtypes = [spinCamera,
                                                          spinImageEventHandler]
    lib.spinCameraUnregisterImageEventHandler.argtypes = [spinCamera,
                                                            spinImageEventHandler]


def _check(err: int, ctx: str) -> None:
    if err != SPINNAKER_ERR_SUCCESS:
        raise RuntimeError(f"{ctx}: spinError={err}")


# ---------------------------------------------------------------------------
# Node access helpers — take a nodemap and a GenICam name; hide the C dance
# ---------------------------------------------------------------------------

def _get_node(nm: spinNodeMapHandle, name: str) -> spinNodeHandle:
    node = spinNodeHandle()
    _check(_spin_lib.spinNodeMapGetNode(nm, name.encode("ascii"),
                                          byref(node)),
           f"GetNode({name})")
    if not node.value:
        raise KeyError(f"Node '{name}' not present on this camera")
    return node


def _node_available(nm: spinNodeMapHandle, name: str) -> bool:
    try:
        node = _get_node(nm, name)
    except Exception:
        return False
    v = bool8_t()
    if _spin_lib.spinNodeIsAvailable(node, byref(v)) != SPINNAKER_ERR_SUCCESS:
        return False
    return bool(v.value)


def _node_writable(nm: spinNodeMapHandle, name: str) -> bool:
    try:
        node = _get_node(nm, name)
    except Exception:
        return False
    v = bool8_t()
    if _spin_lib.spinNodeIsWritable(node, byref(v)) != SPINNAKER_ERR_SUCCESS:
        return False
    return bool(v.value)


def _get_float(nm: spinNodeMapHandle, name: str) -> float:
    node = _get_node(nm, name)
    v = c_double()
    _check(_spin_lib.spinFloatGetValue(node, byref(v)), f"FloatGet({name})")
    return float(v.value)


def _set_float(nm: spinNodeMapHandle, name: str, value: float) -> None:
    node = _get_node(nm, name)
    _check(_spin_lib.spinFloatSetValue(node, c_double(float(value))),
           f"FloatSet({name}={value})")


def _get_int(nm: spinNodeMapHandle, name: str) -> int:
    node = _get_node(nm, name)
    v = c_int64()
    _check(_spin_lib.spinIntegerGetValue(node, byref(v)), f"IntGet({name})")
    return int(v.value)


def _set_int(nm: spinNodeMapHandle, name: str, value: int) -> None:
    node = _get_node(nm, name)
    _check(_spin_lib.spinIntegerSetValue(node, c_int64(int(value))),
           f"IntSet({name}={value})")


def _get_enum_str(nm: spinNodeMapHandle, name: str) -> str:
    node = _get_node(nm, name)
    entry = spinNodeHandle()
    _check(_spin_lib.spinEnumerationGetCurrentEntry(node, byref(entry)),
           f"EnumGetCurrent({name})")
    buf = ctypes.create_string_buffer(MAX_BUF_LEN)
    n = c_size_t(MAX_BUF_LEN)
    _check(_spin_lib.spinEnumerationEntryGetSymbolic(entry, buf, byref(n)),
           f"EnumGetSymbolic({name})")
    return buf.value.decode(errors="replace")


def _set_enum_str(nm: spinNodeMapHandle, name: str, value: str) -> None:
    node = _get_node(nm, name)
    entry = spinNodeHandle()
    _check(_spin_lib.spinEnumerationGetEntryByName(
        node, str(value).encode("ascii"), byref(entry)),
           f"EnumGetEntryByName({name}={value})")
    ival = c_int64()
    _check(_spin_lib.spinEnumerationEntryGetIntValue(entry, byref(ival)),
           f"EnumEntryGetIntValue({name})")
    _check(_spin_lib.spinEnumerationSetIntValue(node, ival),
           f"EnumSet({name}={value})")


def _get_string(nm: spinNodeMapHandle, name: str) -> str:
    try:
        node = _get_node(nm, name)
    except Exception:
        return ""
    buf = ctypes.create_string_buffer(MAX_BUF_LEN)
    n = c_size_t(MAX_BUF_LEN)
    if _spin_lib.spinStringGetValue(node, buf, byref(n)) != SPINNAKER_ERR_SUCCESS:
        return ""
    return buf.value.decode(errors="replace")


def _execute(nm: spinNodeMapHandle, name: str) -> None:
    node = _get_node(nm, name)
    _check(_spin_lib.spinCommandExecute(node), f"Execute({name})")


def _node_intf_type(node: spinNodeHandle) -> int:
    """Return the GenICam interface type (INTF_I*) of a node."""
    t = c_int()
    if _spin_lib.spinNodeGetType(
            node, byref(t)) != SPINNAKER_ERR_SUCCESS:
        return INTF_IBASE
    return int(t.value)


def _node_name(node: spinNodeHandle) -> str:
    buf = ctypes.create_string_buffer(MAX_BUF_LEN)
    n = c_size_t(MAX_BUF_LEN)
    if _spin_lib.spinNodeGetName(node, buf, byref(n)) != SPINNAKER_ERR_SUCCESS:
        return ""
    return buf.value.decode(errors="replace")


def _enumerate_nodes(nm: spinNodeMapHandle) -> List[str]:
    """Return every readable/available GenICam node name on the map."""
    n = c_size_t()
    if _spin_lib.spinNodeMapGetNumNodes(
            nm, byref(n)) != SPINNAKER_ERR_SUCCESS:
        return []
    out: List[str] = []
    for i in range(int(n.value)):
        node = spinNodeHandle()
        if _spin_lib.spinNodeMapGetNodeByIndex(
                nm, c_size_t(i), byref(node)) != SPINNAKER_ERR_SUCCESS:
            continue
        if not node.value:
            continue
        # Skip categories/registers/etc — only scalar-ish nodes are
        # useful as "parameters" from the GUI's perspective.
        if _node_intf_type(node) not in _SCALAR_INTFS:
            continue
        avail = bool8_t()
        if (_spin_lib.spinNodeIsAvailable(node, byref(avail))
                != SPINNAKER_ERR_SUCCESS or not avail.value):
            continue
        name = _node_name(node)
        if name:
            out.append(name)
    return out


def _get_by_intf(nm: spinNodeMapHandle, name: str) -> Any:
    """Read a node using its declared interface type — no guessing."""
    node = _get_node(nm, name)
    intf = _node_intf_type(node)
    if intf == INTF_IINTEGER:
        v = c_int64()
        _check(_spin_lib.spinIntegerGetValue(node, byref(v)), f"IntGet({name})")
        return int(v.value)
    if intf == INTF_IFLOAT:
        v = c_double()
        _check(_spin_lib.spinFloatGetValue(node, byref(v)), f"FloatGet({name})")
        return float(v.value)
    if intf == INTF_IBOOLEAN:
        # spinBooleanGetValue not bound; fall through to int path
        v = c_int64()
        _check(_spin_lib.spinIntegerGetValue(node, byref(v)), f"BoolGet({name})")
        return bool(v.value)
    if intf == INTF_IENUMERATION:
        entry = spinNodeHandle()
        _check(_spin_lib.spinEnumerationGetCurrentEntry(node, byref(entry)),
               f"EnumGetCurrent({name})")
        buf = ctypes.create_string_buffer(MAX_BUF_LEN)
        n = c_size_t(MAX_BUF_LEN)
        _check(_spin_lib.spinEnumerationEntryGetSymbolic(entry, buf, byref(n)),
               f"EnumGetSymbolic({name})")
        return buf.value.decode(errors="replace")
    if intf == INTF_ISTRING:
        buf = ctypes.create_string_buffer(MAX_BUF_LEN)
        n = c_size_t(MAX_BUF_LEN)
        _check(_spin_lib.spinStringGetValue(node, buf, byref(n)),
               f"StringGet({name})")
        return buf.value.decode(errors="replace")
    if intf == INTF_ICOMMAND:
        return "<command>"
    raise RuntimeError(f"Unsupported interface type {intf} for {name}")


def _set_by_intf(nm: spinNodeMapHandle, name: str, value: Any) -> None:
    """Write a node using its declared interface type."""
    node = _get_node(nm, name)
    intf = _node_intf_type(node)
    if intf == INTF_IINTEGER:
        _check(_spin_lib.spinIntegerSetValue(node, c_int64(int(value))),
               f"IntSet({name}={value})")
        return
    if intf == INTF_IFLOAT:
        _check(_spin_lib.spinFloatSetValue(node, c_double(float(value))),
               f"FloatSet({name}={value})")
        return
    if intf == INTF_IBOOLEAN:
        _check(_spin_lib.spinIntegerSetValue(
            node, c_int64(1 if bool(value) else 0)),
               f"BoolSet({name}={value})")
        return
    if intf == INTF_IENUMERATION:
        entry = spinNodeHandle()
        _check(_spin_lib.spinEnumerationGetEntryByName(
            node, str(value).encode("ascii"), byref(entry)),
               f"EnumGetEntryByName({name}={value})")
        ival = c_int64()
        _check(_spin_lib.spinEnumerationEntryGetIntValue(entry, byref(ival)),
               f"EnumEntryGetIntValue({name})")
        _check(_spin_lib.spinEnumerationSetIntValue(node, ival),
               f"EnumSet({name}={value})")
        return
    if intf == INTF_ICOMMAND:
        _check(_spin_lib.spinCommandExecute(node), f"Execute({name})")
        return
    raise RuntimeError(f"Unsupported interface type {intf} for {name}")


# ---------------------------------------------------------------------------
# Camera backend
# ---------------------------------------------------------------------------

@register
class TeledyneOryx(BaseCamera):
    """Teledyne FLIR Oryx via Spinnaker C API (direct ctypes)."""

    camera_type = "teledyne.oryx"
    display_name = "Teledyne FLIR Oryx (Spinnaker)"

    # Standard-name -> GenICam node name. Verified against
    # /home/beams/USERTXM/epics/synApps/support/32idbSP1/db/
    # FLIR_ORX_10G_310S9M.template
    _STANDARD_TO_NODE: Dict[str, str] = {
        # ExposureTime handled specially (µs <-> s conversion)
        "AcquisitionFrameRate": "AcquisitionFrameRate",
        "Width": "Width",
        "Height": "Height",
        "OffsetX": "OffsetX",
        "OffsetY": "OffsetY",
        "BinningHorizontal": "BinningHorizontal",
        "BinningVertical": "BinningVertical",
        "PixelFormat": "PixelFormat",
        "TriggerMode": "TriggerMode",
        "TriggerSource": "TriggerSource",       # handled specially
        "TriggerActive": "TriggerActivation",
        "TriggerDelay": "TriggerDelay",
        "SensorTemperature": "DeviceTemperature",
    }

    def __init__(self, device_index: int = 0,
                 serial: Optional[str] = None, **kwargs):
        super().__init__(device_index=device_index, **kwargs)
        self._serial = str(serial) if serial else None
        self._hsystem: Optional[spinSystem] = None
        self._hcam_list: Optional[spinCameraList] = None
        self._hcam: Optional[spinCamera] = None
        self._nodemap: Optional[spinNodeMapHandle] = None
        self._tl_nodemap: Optional[spinNodeMapHandle] = None

        # Acquisition state
        self._handler: Optional[spinImageEventHandler] = None
        self._callback_ref: Optional[Any] = None   # keep CFUNCTYPE alive
        self._frame_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_counter_ref = [0]
        self._last_frame_counter = 0
        self._pixel_dtype = np.uint16

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def open(self) -> None:
        if self._hcam is not None:
            return
        self._hsystem = _spin_init()
        try:
            hlist = spinCameraList()
            _check(_spin_lib.spinCameraListCreateEmpty(byref(hlist)),
                   "spinCameraListCreateEmpty")
            self._hcam_list = hlist
            _check(_spin_lib.spinSystemGetCameras(self._hsystem, self._hcam_list),
                   "spinSystemGetCameras")

            n = c_size_t()
            _check(_spin_lib.spinCameraListGetSize(self._hcam_list, byref(n)),
                   "spinCameraListGetSize")

            hcam = spinCamera()
            if self._serial:
                err = _spin_lib.spinCameraListGetBySerial(
                    self._hcam_list, self._serial.encode("ascii"), byref(hcam))
                if err != SPINNAKER_ERR_SUCCESS or not hcam.value:
                    raise RuntimeError(
                        f"No Oryx with serial {self._serial!r} "
                        f"(found {int(n.value)} cameras)")
            else:
                if int(n.value) <= self.device_index:
                    raise RuntimeError(
                        f"No Oryx at index {self.device_index} "
                        f"(found {int(n.value)} cameras)")
                _check(_spin_lib.spinCameraListGet(
                    self._hcam_list, c_size_t(self.device_index), byref(hcam)),
                       "spinCameraListGet")
            self._hcam = hcam

            _check(_spin_lib.spinCameraInit(self._hcam), "spinCameraInit")

            nm = spinNodeMapHandle()
            _check(_spin_lib.spinCameraGetNodeMap(self._hcam, byref(nm)),
                   "spinCameraGetNodeMap")
            self._nodemap = nm

            tl_nm = spinNodeMapHandle()
            _check(_spin_lib.spinCameraGetTLStreamNodeMap(self._hcam, byref(tl_nm)),
                   "spinCameraGetTLStreamNodeMap")
            self._tl_nodemap = tl_nm

            # Match ADSpinnaker's buffer config so behavior is comparable
            # (ADSpinnaker.cpp:271-279).
            try:
                _set_enum_str(self._tl_nodemap, "StreamBufferCountMode", "Manual")
                _set_int(self._tl_nodemap, "StreamBufferCountManual", 10)
            except Exception as exc:
                logger.warning("Could not configure TL stream buffers: %s", exc)

            # Populate CameraInfo
            vendor = _get_string(self._nodemap, "DeviceVendorName") or "Teledyne"
            model = _get_string(self._nodemap, "DeviceModelName") or "Oryx"
            serial = _get_string(self._nodemap, "DeviceSerialNumber")
            fw = _get_string(self._nodemap, "DeviceFirmwareVersion")
            # SensorWidth/SensorHeight report the true sensor size,
            # unaffected by current binning. WidthMax/HeightMax return the
            # max ROI in the current binning mode — smaller than the sensor
            # when binning>1 — so they're wrong for CameraInfo.
            w = h = 0
            for node in ("SensorWidth", "WidthMax"):
                try:
                    w = _get_int(self._nodemap, node)
                    break
                except Exception:
                    continue
            for node in ("SensorHeight", "HeightMax"):
                try:
                    h = _get_int(self._nodemap, node)
                    break
                except Exception:
                    continue

            self._info = CameraInfo(
                vendor=vendor, model=model, serial_number=serial,
                firmware_version=fw,
                sensor_width=int(w), sensor_height=int(h),
                bits_per_pixel=12, pixel_size_um=3.45,
                max_frame_rate=1000.0,
                supported_binning=[1, 2, 4],
                supported_trigger_modes=["Internal", "External", "Software"],
                has_temperature=_node_available(self._nodemap,
                                                  "DeviceTemperature"),
                has_cooler=False, has_subarray=True,
            )

            logger.info("Opened %s SN=%s (%dx%d)",
                        self.display_name, serial, int(w), int(h))
        except Exception:
            # Roll back partial init
            self._teardown_camera()
            _spin_uninit()
            raise

    def _teardown_camera(self) -> None:
        """Release camera/list handles without touching the system refcount."""
        if self._hcam is not None:
            try:
                _spin_lib.spinCameraDeInit(self._hcam)
            except Exception:
                pass
            try:
                _spin_lib.spinCameraRelease(self._hcam)
            except Exception:
                pass
            self._hcam = None
        if self._hcam_list is not None:
            try:
                _spin_lib.spinCameraListClear(self._hcam_list)
            except Exception:
                pass
            try:
                _spin_lib.spinCameraListDestroy(self._hcam_list)
            except Exception:
                pass
            self._hcam_list = None
        self._nodemap = None
        self._tl_nodemap = None

    def close(self) -> None:
        if self._is_acquiring:
            try:
                self.stop_acquisition()
            except Exception:
                pass
        self._teardown_camera()
        self._hsystem = None
        self._info = None
        _spin_uninit()

    # -----------------------------------------------------------------------
    # Standard-name parameter interface
    # -----------------------------------------------------------------------

    def list_params(self) -> List[str]:
        """Every GenICam node the camera reports as available.

        Includes the curated standard-name aliases (ExposureTime,
        TriggerMode, TriggerSource, TriggerActive, SensorTemperature,
        etc.) — those go first, then the full GenICam node dump so the
        GUI can show *everything* the SDK exposes, not just a hardcoded
        subset. Duplicates removed while preserving order.
        """
        if self._nodemap is None:
            return []
        # Standard aliases the plugin/GUI expect
        aliases = ["ExposureTime"]
        for std_name, node_name in self._STANDARD_TO_NODE.items():
            if _node_available(self._nodemap, node_name):
                aliases.append(std_name)
        # Every scalar-ish GenICam node
        raw_nodes = _enumerate_nodes(self._nodemap)
        # Also enumerate TL stream nodes (StreamBufferHandlingMode, etc.)
        if self._tl_nodemap is not None:
            raw_nodes += _enumerate_nodes(self._tl_nodemap)
        seen = set()
        out: List[str] = []
        for name in aliases + raw_nodes:
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out

    def get_param(self, name: str) -> Any:
        if self._nodemap is None:
            raise RuntimeError("Camera not open")

        if name == "ExposureTime":
            return _get_float(self._nodemap, "ExposureTime") * 1e-6

        if name == "TriggerMode":
            # Camera enum: "Off"/"On" — same as areaDetector convention
            return _get_enum_str(self._nodemap, "TriggerMode")

        if name == "TriggerSource":
            # Map Spinnaker's ("Software"/"Line0"/...) plus TriggerMode
            # back to the plugin's ("Internal"/"External"/"Software").
            tmode = _get_enum_str(self._nodemap, "TriggerMode")
            if tmode == "Off":
                return "Internal"
            try:
                src = _get_enum_str(self._nodemap, "TriggerSource")
            except Exception:
                return "External"
            return "Software" if src == "Software" else "External"

        if name in ("SensorCooler", "SensorCoolerStatus"):
            return "Off"

        # Standard-name alias → underlying GenICam node
        node_name = self._STANDARD_TO_NODE.get(name, name)
        # Try main node map first, then TL stream node map. Use the
        # interface-type dispatch so we read as the correct scalar type.
        for nm in (self._nodemap, self._tl_nodemap):
            if nm is None:
                continue
            try:
                return _get_by_intf(nm, node_name)
            except KeyError:
                continue
            except Exception as exc:
                logger.debug("get_param(%s) via %s: %s", name, node_name, exc)
                continue
        raise KeyError(f"Unknown param: {name}")

    # ROI / format nodes that are locked while acquisition is running.
    # For these, stop→set→restart so writes actually land instead of
    # returning spinError=-1006 (access denied during grab).
    _IDLE_REQUIRED = frozenset({
        "Width", "Height", "OffsetX", "OffsetY",
        "BinningHorizontal", "BinningVertical", "PixelFormat",
    })

    def _with_idle(self, name: str, apply):
        """Run `apply()` with acquisition paused if `name` requires idle."""
        if name in self._IDLE_REQUIRED and self._is_acquiring:
            logger.info("Pausing acquisition to set %s", name)
            self.stop_acquisition()
            try:
                apply()
            finally:
                self.start_acquisition()
        else:
            apply()

    def set_param(self, name: str, value: Any) -> None:
        if self._nodemap is None:
            raise RuntimeError("Camera not open")

        if name == "ExposureTime":
            # areaDetector unit is seconds; Spinnaker's is microseconds.
            # ExposureTime IS writable during acquisition on Oryx, so no
            # idle guard needed.
            _set_float(self._nodemap, "ExposureTime",
                        max(1.0, float(value) * 1e6))
            return

        if name == "TriggerMode":
            _set_enum_str(self._nodemap, "TriggerMode",
                          "On" if str(value) in ("On", "1", "True") else "Off")
            return

        if name == "TriggerSource":
            v = str(value)
            if v == "Internal":
                _set_enum_str(self._nodemap, "TriggerMode", "Off")
            elif v == "Software":
                _set_enum_str(self._nodemap, "TriggerMode", "Off")
                _set_enum_str(self._nodemap, "TriggerSource", "Software")
                _set_enum_str(self._nodemap, "TriggerMode", "On")
            elif v in ("External", "Line0", "Line1", "Line2", "Line3"):
                line = "Line0" if v == "External" else v
                _set_enum_str(self._nodemap, "TriggerMode", "Off")
                _set_enum_str(self._nodemap, "TriggerSource", line)
                _set_enum_str(self._nodemap, "TriggerMode", "On")
            else:
                raise ValueError(f"Unknown TriggerSource: {v}")
            return

        if name == "TriggerActive":
            # Standard names map to TriggerActivation enum values
            mapping = {"Edge": "RisingEdge", "Level": "LevelHigh",
                       "RisingEdge": "RisingEdge", "FallingEdge": "FallingEdge",
                       "LevelHigh": "LevelHigh", "LevelLow": "LevelLow",
                       "AnyEdge": "AnyEdge"}
            _set_enum_str(self._nodemap, "TriggerActivation",
                          mapping.get(str(value), str(value)))
            return

        if name in ("TriggerGlobalExposure", "TriggerPolarity",
                    "SensorCooler", "SensorCoolerStatus"):
            # Not supported on Oryx; accept silently so cam_plugin/GUI
            # writers don't spam warnings.
            return

        # Standard-name alias → underlying GenICam node name.
        # If it's not aliased, we pass the raw node name straight through.
        node_name = self._STANDARD_TO_NODE.get(name, name)

        def _apply():
            # Use the correct interface-type dispatch — no guessing.
            for nm in (self._nodemap, self._tl_nodemap):
                if nm is None:
                    continue
                try:
                    _set_by_intf(nm, node_name, value)
                    return
                except KeyError:
                    continue
            raise KeyError(f"Unknown param: {name}")

        # Width/Height/Offset/Binning/PixelFormat are locked while
        # acquiring — stop, apply, restart. Otherwise the SDK returns
        # spinError=-1006 and the write silently doesn't happen.
        self._with_idle(name, _apply)

    def is_param_writable(self, name: str) -> bool:
        if self._nodemap is None:
            return False
        if name in ("SensorTemperature", "SensorCoolerStatus",
                    "AcquisitionFrameRate"):
            return False
        node_name = self._STANDARD_TO_NODE.get(name, name)
        for nm in (self._nodemap, self._tl_nodemap):
            if nm is None:
                continue
            if _node_writable(nm, node_name):
                return True
        return False

    def _numpy_dtype_from_pixel_format(self) -> np.dtype:
        """Determine numpy dtype from PixelFormat.

        Only unpacked 8/16-bit mono formats are supported. Packed formats
        (Mono12Packed, Mono10Packed, YUV*, RGB*) would need per-frame
        unpacking; raise a clear error rather than return a wrong dtype
        that would corrupt frames or crash on the reshape.
        """
        try:
            fmt = _get_enum_str(self._nodemap, "PixelFormat")
        except Exception:
            return np.dtype(np.uint16)
        if fmt in ("Mono8", "BayerGB8", "BayerRG8", "BayerGR8", "BayerBG8"):
            return np.dtype(np.uint8)
        if fmt in ("Mono16", "Mono12", "Mono10",
                   "BayerGB16", "BayerRG16", "BayerGR16", "BayerBG16"):
            return np.dtype(np.uint16)
        raise RuntimeError(
            f"PixelFormat={fmt!r} not supported. Set the camera to "
            f"'Mono16' (or 'Mono8') before starting acquisition, e.g. "
            f"cam.set_param('PixelFormat', 'Mono16').")

    # -----------------------------------------------------------------------
    # Acquisition — callback-to-Event bridge (mirrors kinetix.py)
    # -----------------------------------------------------------------------

    def start_acquisition(self) -> None:
        if self._is_acquiring:
            return

        # Refresh pixel dtype in case PixelFormat changed while idle
        self._pixel_dtype = self._numpy_dtype_from_pixel_format()

        # Close over module-local references — Spinnaker calls this on
        # its own thread, so we must NOT touch anything that requires
        # the GIL beyond ctypes accessors and the frame_event/refs.
        lib = _spin_lib
        dtype = self._pixel_dtype
        frame_lock = self._frame_lock
        frame_event = self._frame_event
        frame_counter_ref = self._frame_counter_ref
        latest_frame_holder = [None]  # box to let closure write

        def _image_cb(hImage, pUser):
            try:
                incomplete = bool8_t()
                if lib.spinImageIsIncomplete(hImage, byref(incomplete)) \
                        != SPINNAKER_ERR_SUCCESS:
                    return
                if incomplete.value:
                    return

                w = c_size_t(); h = c_size_t(); data = c_void_p()
                if lib.spinImageGetWidth(hImage, byref(w)) != SPINNAKER_ERR_SUCCESS:
                    return
                if lib.spinImageGetHeight(hImage, byref(h)) != SPINNAKER_ERR_SUCCESS:
                    return
                if lib.spinImageGetData(hImage, byref(data)) != SPINNAKER_ERR_SUCCESS:
                    return
                if not data.value:
                    return

                n = int(w.value) * int(h.value)
                ctype = ctypes.c_uint8 if dtype == np.uint8 else ctypes.c_uint16
                buf = (ctype * n).from_address(data.value)
                arr = np.frombuffer(buf, dtype=dtype).reshape(
                    (int(h.value), int(w.value))).copy()

                with frame_lock:
                    latest_frame_holder[0] = arr
                    frame_counter_ref[0] += 1
                frame_event.set()

                # Fan out to registered callbacks (HDF5/PVA sinks).
                try:
                    self._notify_frame(
                        arr, {"frame_number": frame_counter_ref[0]})
                except Exception:
                    logger.exception("Frame callback error")
            finally:
                try:
                    lib.spinImageRelease(hImage)
                except Exception:
                    pass

        # Keep the CFUNCTYPE object alive AND the frame holder alive
        self._callback_ref = spinImageEventFunction(_image_cb)
        self._latest_frame_holder = latest_frame_holder

        handler = spinImageEventHandler()
        _check(_spin_lib.spinImageEventHandlerCreate(
            byref(handler), self._callback_ref, None),
               "spinImageEventHandlerCreate")
        self._handler = handler

        _check(_spin_lib.spinCameraRegisterImageEventHandler(
            self._hcam, self._handler),
               "spinCameraRegisterImageEventHandler")

        # Reset counters/state
        self._last_frame_counter = 0
        self._frame_counter_ref[0] = 0
        self._frame_event.clear()

        _check(_spin_lib.spinCameraBeginAcquisition(self._hcam),
               "spinCameraBeginAcquisition")
        self._is_acquiring = True
        logger.info("Acquisition started")

    def stop_acquisition(self) -> None:
        if not self._is_acquiring:
            return
        try:
            _spin_lib.spinCameraEndAcquisition(self._hcam)
        except Exception:
            pass
        if self._handler is not None:
            try:
                _spin_lib.spinCameraUnregisterImageEventHandler(
                    self._hcam, self._handler)
            except Exception:
                pass
            try:
                _spin_lib.spinImageEventHandlerDestroy(self._handler)
            except Exception:
                pass
            self._handler = None
        self._callback_ref = None
        self._is_acquiring = False
        # Wake any pending acquire_frame
        self._frame_event.set()
        logger.info("Acquisition stopped")

    def acquire_frame(self, timeout_ms: int = 5000) -> np.ndarray:
        """Block on threading.Event set by the image-event callback.

        Event.wait() releases the GIL while blocking — Qt / caproto keep
        running. Same pattern as kinetix.py's acquire_frame.
        """
        if not self._is_acquiring:
            raise RuntimeError("Acquisition not running")
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            if self._frame_counter_ref[0] > self._last_frame_counter:
                self._last_frame_counter = self._frame_counter_ref[0]
                with self._frame_lock:
                    arr = self._latest_frame_holder[0]
                if arr is not None:
                    return arr
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"acquire_frame timed out after {timeout_ms} ms "
                    f"(frame_counter={self._frame_counter_ref[0]})")
            self._frame_event.wait(timeout=min(remaining, 0.05))
            self._frame_event.clear()

    def software_trigger(self) -> None:
        if self._nodemap is None:
            raise RuntimeError("Camera not open")
        _execute(self._nodemap, "TriggerSoftware")
