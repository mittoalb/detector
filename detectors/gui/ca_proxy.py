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
    """Behaves like `._data` — supports `['value']` read and write."""

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
            v = resp.data[0] if hasattr(resp, "data") else resp
            if isinstance(v, bytes):
                v = v.decode("utf-8", errors="replace")
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
    """BaseCamera-shaped proxy that reads/writes params via CA."""

    def __init__(self, ca: _CAHelper, prefix: str):
        self._ca = ca
        self._prefix = prefix
        # Populate CameraInfo from CA reads
        from detectors.core.base import CameraInfo
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

    _STANDARD_TO_PV = {
        "ExposureTime": ("cam1:AcquireTime", float),
        "AcquisitionFrameRate": ("cam1:AcquireTime_RBV", float),
        "Width": ("cam1:SizeX_RBV", int),
        "Height": ("cam1:SizeY_RBV", int),
        "OffsetX": ("cam1:MinX", int),
        "OffsetY": ("cam1:MinY", int),
        "BinningHorizontal": ("cam1:BinX", int),
        "BinningVertical": ("cam1:BinY", int),
        "PixelFormat": ("cam1:PixelFormat_RBV", str),
        "TriggerMode": ("cam1:TriggerMode", str),
        "TriggerSource": ("cam1:TriggerSource", str),
        "SensorTemperature": ("cam1:SensorTemperature_RBV", float),
    }

    def list_params(self) -> List[str]:
        return list(self._STANDARD_TO_PV)

    def get_param(self, name: str) -> Any:
        pv_short, cast = self._STANDARD_TO_PV.get(name, (None, None))
        if pv_short is None:
            return None
        return self._safe_get(pv_short, None, cast)

    def set_param(self, name: str, value: Any) -> None:
        pv_short, cast = self._STANDARD_TO_PV.get(name, (None, None))
        if pv_short is None:
            return
        try:
            self._ca.put(f"{self._prefix}{pv_short}",
                         cast(value) if cast else value, wait=False)
        except Exception as exc:
            logger.debug("set_param %s=%s: %s", name, value, exc)

    def is_param_supported(self, name: str) -> bool:
        return name in self._STANDARD_TO_PV

    def is_param_writable(self, name: str) -> bool:
        return name in self._STANDARD_TO_PV and not name.endswith("_RBV")

    def software_trigger(self) -> None:
        try:
            self._ca.put(f"{self._prefix}cam1:TriggerSoftware", 1, wait=False)
        except Exception:
            pass

    def _safe_get(self, pv_short, default=None, cast=None):
        try:
            resp = self._ca.get(f"{self._prefix}{pv_short}", timeout=0.5)
            v = resp.data[0] if hasattr(resp, "data") else resp
            if isinstance(v, bytes):
                v = v.decode("utf-8", errors="replace")
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
            self._ca.put(f"{self._prefix}cam1:Acquire", "Acquire", wait=False)
        except Exception as exc:
            logger.error("Failed to start acquisition: %s", exc)

    def _stop_acquisition(self) -> None:
        try:
            self._ca.put(f"{self._prefix}cam1:Acquire", "Done", wait=False)
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
        pv_name = f"{self._prefix}image1:Pva1:Image"
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
    def __init__(self, ca: _CAHelper, prefix: str, sub: str):
        self.FilePath = _CAField(ca, f"{prefix}{sub}:FilePath", "/tmp/detector/", str)
        self.FileName = _CAField(ca, f"{prefix}{sub}:FileName", "scan", str)
        self.FileNumber = _CAField(ca, f"{prefix}{sub}:FileNumber", 1, int)
        self.Capture = _CAField(ca, f"{prefix}{sub}:Capture", 0, int)
        self.NumCapture = _CAField(ca, f"{prefix}{sub}:NumCapture", 1000, int)
        self.NumCaptured_RBV = _CAField(
            ca, f"{prefix}{sub}:NumCaptured_RBV", 0, int)


class _PvaServerProxy:
    def __init__(self, prefix: str):
        self._pv_name = f"{prefix}image1:Pva1:Image"


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
