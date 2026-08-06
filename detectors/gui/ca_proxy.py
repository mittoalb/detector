"""
CA/PVA-backed proxy objects that mimic the IOC interface qt_gui.py expects.

Lets the GUI run as a subprocess that talks to the IOC over the network,
instead of sharing Python objects. That isolation is required for the
Kinetix backend (PVCAM's DMA signalling doesn't survive Qt in the same
process), and gives all cameras the same "GUI is a network client" model.

Interface targets (from qt_gui.py):
    self.ioc.camera             — BaseCamera-like
    self.ioc.cam1               — cam1 pvgroup: .TriggerMode._data['value'], ...
    self.ioc.cam1._start_acquisition() / ._stop_acquisition()
    self.ioc.cam1._acquiring
    self.ioc.cam1.register_frame_callback(cb)
    self.ioc.HDF1 / self.ioc.TIF1  — FilePath/FileName/FileNumber._data['value']
    self.ioc._pva_server._pv_name
"""

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

import numpy as np
from caproto.threading.client import Context as CAContext

logger = logging.getLogger(__name__)


def _decode_ca_value(resp):
    """Turn a caproto ReadResponse into a Python scalar.

    - Regular numeric PVs: `.data` is a length-1 array; return element 0.
    - String PVs: `.data` may be a plain bytes/str, or an array of byte
      integers (char codes). We coalesce into a Python str.
    - Enum PVs: `.data` is a length-1 int index — return the string label
      from `.metadata.enum_strings` if present, else the raw int.
    """
    if resp is None:
        return None
    data = getattr(resp, "data", resp)

    # Bytes → str
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace").rstrip("\x00")

    # Array/list — could be a char array (string PV) or scalar wrapped in a list
    try:
        length = len(data)
    except TypeError:
        return data

    if length == 0:
        return None

    # Single-element scalar (numeric or single-char)
    if length == 1:
        v = data[0]
        # Enum PV: metadata carries the string labels
        meta = getattr(resp, "metadata", None)
        enum_strings = getattr(meta, "enum_strings", None) if meta else None
        if enum_strings:
            try:
                s = enum_strings[int(v)]
                if isinstance(s, bytes):
                    s = s.decode("utf-8", errors="replace")
                return s
            except Exception:
                pass
        if isinstance(v, bytes):
            v = v.decode("utf-8", errors="replace").rstrip("\x00")
        return v

    # Multi-element: for CA string PVs served by caproto this is a
    # sequence of small ints (char codes) representing one string.
    # Coerce each element via int() to handle numpy scalar types.
    try:
        as_ints = [int(c) & 0xFF for c in data]
        s = bytes(as_ints).decode("utf-8", errors="replace").rstrip("\x00")
        # Only treat as a string if it's printable ASCII/UTF-8
        if s and all(ord(ch) >= 9 for ch in s):
            return s
    except Exception:
        pass
    return data


class _CAField:
    """`.FooPV._data['value']` accessor backed by CA."""

    def __init__(self, ca, pvname, default=None, cast=None):
        self._ca = ca
        self._pvname = pvname
        self._default = default
        self._cast = cast

    @property
    def _data(self):
        return _CADataDict(self._ca, self._pvname, self._default, self._cast)


class _CADataDict:
    """Behaves like `._data` — supports `['value']` read and write.

    Two subprocess-mode quirks handled here:
    - A CA read that returns None (empty/missing) is mapped to the
      declared default. The GUI does `str(value)` on results and would
      otherwise turn None into the literal string "None".
    - Writes to _RBV PVs are silently swallowed. In-process, qt_gui.py's
      `writer.FilePath_RBV._data['value'] = path` is a Python assignment;
      over CA it hits a read_only PV and caproto raises Forbidden. The
      driver-side plugin owns readback updates anyway.
    """

    def __init__(self, ca, pvname, default, cast):
        self._ca = ca
        self._pvname = pvname
        self._default = default
        self._cast = cast

    def __getitem__(self, key):
        if key != "value":
            raise KeyError(key)
        try:
            resp = self._ca.get(self._pvname, timeout=0.5)
            v = _decode_ca_value(resp)
            if v is None:
                return self._default
            if self._cast is not None:
                try:
                    v = self._cast(v)
                except Exception:
                    pass
            return v
        except Exception:
            return self._default

    def __setitem__(self, key, value):
        if key != "value":
            raise KeyError(key)
        # Swallow writes to readback PVs — the driver process owns them.
        if self._pvname.endswith("_RBV"):
            return
        try:
            self._ca.put(self._pvname, value, wait=False)
        except Exception as exc:
            logger.debug("put %s=%s failed: %s", self._pvname, value, exc)


class _CAHelper:
    """Thin wrapper around caproto's threading client."""

    def __init__(self):
        self._ctx = CAContext()
        self._pv_cache: Dict[str, Any] = {}

    def _pv(self, name):
        if name not in self._pv_cache:
            (self._pv_cache[name],) = self._ctx.get_pvs(name)
        return self._pv_cache[name]

    def get(self, name, timeout=0.5):
        return self._pv(name).read(timeout=timeout)

    def put(self, name, value, wait=False):
        self._pv(name).write(value, wait=wait)


class CameraProxy:
    """BaseCamera-shaped proxy that reads/writes params via IPC (preferred)
    or falls back to CA when the driver's IPC socket isn't reachable.

    The IPC path gives full access to whatever the backend's list_params
    exposes — every Oryx GenICam node, every DCAM property, etc. The CA
    fallback keeps working with the hardcoded 15-key surface so nothing
    breaks if the socket is missing (older IOC, remote machine, etc.).
    """

    def __init__(self, ca: _CAHelper, prefix: str):
        self._ca = ca
        self._prefix = prefix

        # Try IPC first — the driver process opens a UNIX socket at a
        # deterministic path derived from the prefix. If it's there, use it.
        self._ipc = None
        try:
            from detectors.server.ipc import socket_path_for
            from detectors.gui.ipc_client import CameraIPCClient
            self._ipc = CameraIPCClient(socket_path_for(prefix))
            logger.info("CameraProxy using IPC to driver process")
        except Exception as exc:
            logger.info(
                "CameraProxy IPC unavailable (%s); falling back to CA gateway",
                exc)

        # Populate CameraInfo — prefer live IPC, fall back to CA reads
        from detectors.core.base import CameraInfo
        info_dict = None
        if self._ipc is not None:
            try:
                info_dict = self._ipc.get_info()
            except Exception as exc:
                logger.debug("IPC get_info failed: %s", exc)
        if info_dict:
            self._info = CameraInfo(**{
                k: v for k, v in info_dict.items()
                if k in {"vendor", "model", "sensor_width", "sensor_height",
                          "bits_per_pixel", "pixel_size_um",
                          "max_frame_rate", "supported_binning",
                          "supported_trigger_modes", "has_temperature",
                          "has_cooler", "has_subarray", "serial_number",
                          "firmware_version"}})
        else:
            try:
                vendor = str(self._safe_get("cam1:Manufacturer_RBV", "Unknown"))
                model = str(self._safe_get("cam1:Model_RBV", "Camera"))
                w = int(self._safe_get("cam1:MaxSizeX_RBV", 0) or 0)
                h = int(self._safe_get("cam1:MaxSizeY_RBV", 0) or 0)
            except Exception:
                vendor = "Unknown"
                model = "Camera"
                w = h = 0
            self._info = CameraInfo(
                vendor=vendor, model=model,
                sensor_width=w, sensor_height=h,
                bits_per_pixel=16, max_frame_rate=60.0,
                supported_binning=[1, 2, 4],
                supported_trigger_modes=["Internal", "External", "Software"],
                has_temperature=True, has_cooler=True, has_subarray=True,
            )

    # BaseCamera-like API used by the GUI
    def is_open(self) -> bool:
        return True

    def get_info(self):
        return self._info

    # standard-name -> (setter PV, cast, read-PV-suffix)
    # setter PV is what set_param writes to; read PV is (setter, read) with
    # read defaulting to setter if omitted. This split matters for params
    # like OffsetX where set_param must hit "cam1:OffsetX" (has a putter)
    # but get_param should read the live "cam1:OffsetX_RBV".
    _STANDARD_TO_PV = {
        "ExposureTime": ("cam1:AcquireTime", float, "cam1:AcquireTime_RBV"),
        "AcquisitionFrameRate": ("cam1:AcquisitionFrameRate_RBV", float, None),
        "Width": ("cam1:SizeX", int, "cam1:SizeX_RBV"),
        "Height": ("cam1:SizeY", int, "cam1:SizeY_RBV"),
        "OffsetX": ("cam1:OffsetX", int, "cam1:OffsetX_RBV"),
        "OffsetY": ("cam1:OffsetY", int, "cam1:OffsetY_RBV"),
        "BinningHorizontal": ("cam1:BinX", int, "cam1:BinX_RBV"),
        "BinningVertical": ("cam1:BinY", int, "cam1:BinY_RBV"),
        "PixelFormat": ("cam1:PixelFormat", str, "cam1:PixelFormat_RBV"),
        "TriggerMode": ("cam1:TriggerMode", str, "cam1:TriggerMode_RBV"),
        "TriggerSource": ("cam1:TriggerSource", str, None),
        "TriggerActive": ("cam1:TriggerActive", str, "cam1:TriggerActive_RBV"),
        "TriggerDelay": ("cam1:TriggerDelay", float, "cam1:TriggerDelay_RBV"),
        "SensorTemperature": ("cam1:SensorTemperature_RBV", float, None),
        "SensorCoolerStatus": ("cam1:SensorCoolerStatus_RBV", str, None),
    }

    def list_params(self) -> List[str]:
        if self._ipc is not None:
            try:
                return self._ipc.list_params()
            except Exception as exc:
                logger.debug("IPC list_params failed, using CA fallback: %s", exc)
        return list(self._STANDARD_TO_PV)

    def get_param(self, name: str) -> Any:
        if self._ipc is not None:
            try:
                return self._ipc.get_param(name)
            except Exception as exc:
                logger.debug("IPC get_param(%s) failed, using CA fallback: %s",
                             name, exc)
        entry = self._STANDARD_TO_PV.get(name)
        if entry is None:
            return None
        setter, cast, read_pv = entry
        return self._safe_get(read_pv or setter, None, cast)

    def set_param(self, name: str, value: Any) -> None:
        if self._ipc is not None:
            try:
                self._ipc.set_param(name, value)
                return
            except Exception as exc:
                logger.debug("IPC set_param(%s=%s) failed, using CA fallback: %s",
                             name, value, exc)
        entry = self._STANDARD_TO_PV.get(name)
        if entry is None:
            return
        setter, cast, _ = entry
        try:
            self._ca.put(f"{self._prefix}{setter}",
                         cast(value) if cast else value, wait=False)
        except Exception as exc:
            logger.debug("set_param %s=%s: %s", name, value, exc)

    def is_param_supported(self, name: str) -> bool:
        if self._ipc is not None:
            try:
                return name in self._ipc.list_params()
            except Exception:
                pass
        return name in self._STANDARD_TO_PV

    def is_param_writable(self, name: str) -> bool:
        if self._ipc is not None:
            try:
                return self._ipc.is_param_writable(name)
            except Exception as exc:
                logger.debug("IPC is_param_writable(%s) failed: %s", name, exc)
        entry = self._STANDARD_TO_PV.get(name)
        if entry is None:
            return False
        setter, _, _ = entry
        return not setter.endswith("_RBV")

    def software_trigger(self) -> None:
        if self._ipc is not None:
            try:
                self._ipc.software_trigger()
                return
            except Exception as exc:
                logger.debug("IPC software_trigger failed: %s", exc)
        try:
            self._ca.put(f"{self._prefix}cam1:TriggerSoftware", 1, wait=False)
        except Exception:
            pass

    def _safe_get(self, pv_short, default=None, cast=None):
        try:
            resp = self._ca.get(f"{self._prefix}{pv_short}", timeout=0.5)
            v = _decode_ca_value(resp)
            if cast is not None:
                try:
                    v = cast(v)
                except Exception:
                    return default
            return v
        except Exception:
            return default


class CamPluginProxy:
    """Mimics ioc.cam1 for the GUI's needs."""

    def __init__(self, ca: _CAHelper, prefix: str, camera: CameraProxy):
        self._ca = ca
        self._prefix = prefix
        self._camera = camera
        self._frame_callbacks: List[Callable] = []
        self._pva_thread = None
        self._pva_stop = threading.Event()

        # Expose the fields qt_gui reads/writes via ._data['value']
        self.TriggerMode = _CAField(ca, f"{prefix}cam1:TriggerMode", "Off")
        self.TriggerMode_RBV = _CAField(
            ca, f"{prefix}cam1:TriggerMode_RBV", "Off")
        self.TriggerSource = _CAField(
            ca, f"{prefix}cam1:TriggerSource", "Internal")
        self.ImageMode = _CAField(
            ca, f"{prefix}cam1:ImageMode", "Continuous")
        self.NumImages = _CAField(ca, f"{prefix}cam1:NumImages", 1, int)

    @property
    def _acquiring(self) -> bool:
        try:
            resp = self._ca.get(f"{self._prefix}cam1:Acquire", timeout=0.5)
            v = resp.data[0] if hasattr(resp, "data") else resp
            if isinstance(v, bytes):
                v = v.decode("utf-8", errors="replace")
            return str(v) in ("1", "Acquire")
        except Exception:
            return False

    def _start_acquisition(self) -> None:
        try:
            # Enum PV — write the numeric index (1 = "Acquire")
            self._ca.put(f"{self._prefix}cam1:Acquire", 1, wait=False)
        except Exception as exc:
            logger.error("Failed to start acquisition: %s", exc)

    def _stop_acquisition(self) -> None:
        try:
            self._ca.put(f"{self._prefix}cam1:Acquire", 0, wait=False)
        except Exception as exc:
            logger.error("Failed to stop acquisition: %s", exc)

    def register_frame_callback(self, cb) -> None:
        """Subscribe to the PVA image channel and dispatch frames to cb."""
        self._frame_callbacks.append(cb)
        if self._pva_thread is None:
            self._pva_thread = threading.Thread(
                target=self._pva_worker, daemon=True)
            self._pva_thread.start()

    def _pva_worker(self):
        try:
            import pvaccess as pva
        except Exception as exc:
            logger.warning("pvaccess unavailable — no live frames: %s", exc)
            return
        pv_name = f"{self._prefix}image1:ArrayData"
        try:
            channel = pva.Channel(pv_name)
        except Exception as exc:
            logger.warning("PVA channel %s failed: %s", pv_name, exc)
            return

        def _cb(ntnda):
            try:
                # NTNDArray value is a union — get whichever field is set
                v_union = ntnda["value"][0]
                dtype = None
                data = None
                for field, np_dtype in [
                    ("ushortValue", np.uint16),
                    ("shortValue", np.int16),
                    ("ubyteValue", np.uint8),
                    ("byteValue", np.int8),
                    ("uintValue", np.uint32),
                    ("intValue", np.int32),
                ]:
                    if field in v_union:
                        data = np.array(v_union[field], dtype=np_dtype)
                        break
                if data is None:
                    return
                dims = ntnda["dimension"]
                if len(dims) >= 2:
                    w = int(dims[0]["size"])
                    h = int(dims[1]["size"])
                    arr = data.reshape((h, w))
                else:
                    return
                meta = {"frame_number": 0, "timestamp": 0.0}
                for cb in list(self._frame_callbacks):
                    try:
                        cb(arr, meta)
                    except Exception:
                        logger.exception("frame callback error")
            except Exception:
                logger.exception("PVA cb decode error")

        try:
            channel.subscribe("cb", _cb)
            channel.startMonitor()
        except Exception as exc:
            logger.warning("PVA subscribe failed: %s", exc)


class _FilePluginProxy:
    """CA-backed proxy exposing the HDF5/TIFF plugin surface the GUI needs.

    In-process, the GUI reaches into the plugin instance for state like
    _capturing / _frames_captured / _write_queue. In subprocess mode we
    approximate those via CA reads (Capture_RBV, NumCaptured_RBV) and
    provide harmless stubs for the internals the GUI displays.
    """

    def __init__(self, ca: _CAHelper, prefix: str, sub: str):
        self._ca = ca
        self._pv_base = f"{prefix}{sub}"
        # Public PV fields (used by GUI via ._data['value'])
        self.FilePath = _CAField(ca, f"{prefix}{sub}:FilePath", "/tmp/detector/", str)
        self.FilePath_RBV = _CAField(
            ca, f"{prefix}{sub}:FilePath_RBV", "/tmp/detector/", str)
        self.FilePathExists_RBV = _CAField(
            ca, f"{prefix}{sub}:FilePathExists_RBV", 0, int)
        self.FileName = _CAField(ca, f"{prefix}{sub}:FileName", "scan", str)
        self.FileName_RBV = _CAField(ca, f"{prefix}{sub}:FileName_RBV", "scan", str)
        self.FileNumber = _CAField(ca, f"{prefix}{sub}:FileNumber", 1, int)
        self.Capture = _CAField(ca, f"{prefix}{sub}:Capture", 0, int)
        self.NumCapture = _CAField(ca, f"{prefix}{sub}:NumCapture", 1000, int)
        self.NumCaptured_RBV = _CAField(
            ca, f"{prefix}{sub}:NumCaptured_RBV", 0, int)
        self.FullFileName_RBV = _CAField(
            ca, f"{prefix}{sub}:FullFileName_RBV", "", str)
        # Internal-state stubs used by qt_gui.py in-process paths. In
        # subprocess mode the plugin's actual queue / byte counters live
        # in the driver process; we surface what we can via CA and
        # zero the rest.
        self._write_queue = None
        self._frame_bytes = 0
        self._dropped_frames = 0
        self._frames_received = 0

    @property
    def _capturing(self) -> bool:
        # Capture PV: 0=Done, 1=Capture. Any non-zero → capturing.
        try:
            return bool(self._read_int("Capture_RBV")
                        or self._read_int("Capture"))
        except Exception:
            return False

    @property
    def _frames_captured(self) -> int:
        return self._read_int("NumCaptured_RBV") or 0

    def _read_int(self, suffix: str) -> int:
        try:
            resp = self._ca.get(f"{self._pv_base}:{suffix}", timeout=0.5)
            v = _decode_ca_value(resp)
            return int(v) if v is not None else 0
        except Exception:
            return 0

    def _start_capture(self) -> None:
        try:
            self._ca.put(f"{self._pv_base}:Capture", 1, wait=False)
        except Exception as exc:
            logger.debug("start_capture(%s): %s", self._pv_base, exc)

    def _stop_capture(self) -> None:
        try:
            self._ca.put(f"{self._pv_base}:Capture", 0, wait=False)
        except Exception as exc:
            logger.debug("stop_capture(%s): %s", self._pv_base, exc)

    def _maybe_stop_capture(self) -> None:
        # End-of-acquisition hook — no-op on the client side; the driver
        # process's HDF5Plugin handles the auto-close on its own.
        pass


class _PvaServerProxy:
    def __init__(self, prefix: str):
        self._pv_name = f"{prefix}image1:ArrayData"


class IocProxy:
    """Top-level proxy exposing the attributes qt_gui.py reads off `ioc`."""

    def __init__(self, prefix: str):
        self._prefix = prefix
        self._ca = _CAHelper()
        self.camera = CameraProxy(self._ca, prefix)
        self.cam1 = CamPluginProxy(self._ca, prefix, self.camera)
        self.HDF1 = _FilePluginProxy(self._ca, prefix, "HDF1")
        self.TIF1 = _FilePluginProxy(self._ca, prefix, "TIF1")
        self._pva_server = _PvaServerProxy(prefix)
