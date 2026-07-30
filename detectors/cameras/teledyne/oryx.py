"""
Teledyne (FLIR) Oryx camera via Spinnaker SDK.

Requires:
    - Spinnaker SDK installed (libSpinnaker.so)
    - PySpin Python bindings (`pip install spinnaker-python`)

Reference: https://www.flir.com/products/spinnaker-sdk/

The Oryx is a CoaXPress camera. Use a frame grabber that ships with a
GenTL producer (e.g. Spinnaker's bundled producer or third-party).
"""

import logging
from typing import Any, List, Optional

import numpy as np

from detectors.core.base import BaseCamera, CameraInfo
from detectors.core.registry import register

logger = logging.getLogger(__name__)


@register
class TeledyneOryx(BaseCamera):
    """Teledyne FLIR Oryx via Spinnaker SDK."""

    camera_type = "teledyne.oryx"
    display_name = "Teledyne FLIR Oryx (Spinnaker)"

    # Map standard parameter names to GenICam node names on Oryx
    PARAM_TO_NODE = {
        "ExposureTime": "ExposureTime",
        "AcquisitionFrameRate": "AcquisitionFrameRate",
        "Width": "Width",
        "Height": "Height",
        "OffsetX": "OffsetX",
        "OffsetY": "OffsetY",
        "BinningHorizontal": "BinningHorizontal",
        "BinningVertical": "BinningVertical",
        "PixelFormat": "PixelFormat",
        "TriggerMode": "TriggerMode",
        "TriggerSource": "TriggerSource",
        "TriggerActive": "TriggerActivation",
        "TriggerDelay": "TriggerDelay",
        "SensorTemperature": "DeviceTemperature",
    }

    def __init__(self, device_index: int = 0, **kwargs):
        super().__init__(device_index=device_index, **kwargs)
        try:
            import PySpin
        except ImportError as exc:
            raise RuntimeError(
                "PySpin (spinnaker-python) required. "
                "Install Spinnaker SDK and `pip install spinnaker-python`") from exc
        self._PySpin = PySpin
        self._system = None
        self._cam = None
        self._cam_list = None

    def open(self) -> None:
        if self._cam is not None:
            return
        self._system = self._PySpin.System.GetInstance()
        self._cam_list = self._system.GetCameras()
        if self._cam_list.GetSize() <= self.device_index:
            self._cam_list.Clear()
            self._system.ReleaseInstance()
            raise RuntimeError(
                f"No Oryx at index {self.device_index} "
                f"(found {self._cam_list.GetSize()} cameras)")

        self._cam = self._cam_list.GetByIndex(self.device_index)
        self._cam.Init()

        # Read static info
        nm = self._cam.GetNodeMap()
        try:
            w = int(self._PySpin.CIntegerPtr(nm.GetNode("WidthMax")).GetValue())
            h = int(self._PySpin.CIntegerPtr(nm.GetNode("HeightMax")).GetValue())
        except Exception:
            w = h = 0

        try:
            model = self._PySpin.CStringPtr(
                nm.GetNode("DeviceModelName")).GetValue()
        except Exception:
            model = "Oryx"
        try:
            serial = self._PySpin.CStringPtr(
                nm.GetNode("DeviceSerialNumber")).GetValue()
        except Exception:
            serial = ""

        self._info = CameraInfo(
            vendor="Teledyne", model=model, serial_number=serial,
            sensor_width=w, sensor_height=h,
            bits_per_pixel=12,
            max_frame_rate=1000.0,
            supported_binning=[1, 2, 4],
            supported_trigger_modes=["Internal", "External", "Software"],
            has_temperature=True, has_cooler=False, has_subarray=True,
        )
        logger.info("Opened %s SN=%s (%dx%d)",
                    self.display_name, serial, w, h)

    def close(self) -> None:
        if self._is_acquiring:
            try:
                self.stop_acquisition()
            except Exception:
                pass
        if self._cam is not None:
            self._cam.DeInit()
            del self._cam
            self._cam = None
        if self._cam_list is not None:
            self._cam_list.Clear()
            self._cam_list = None
        if self._system is not None:
            self._system.ReleaseInstance()
            self._system = None
        self._info = None

    def list_params(self) -> List[str]:
        return list(self.PARAM_TO_NODE.keys())

    def get_param(self, name: str) -> Any:
        node_name = self.PARAM_TO_NODE.get(name)
        if not node_name:
            raise KeyError(f"Unknown param: {name}")
        nm = self._cam.GetNodeMap()
        node = nm.GetNode(node_name)
        if node is None:
            raise KeyError(f"Node '{node_name}' not on this camera")

        # Try different node types
        for cls_name, conv in [("CFloatPtr", float),
                                ("CIntegerPtr", int),
                                ("CEnumerationPtr", lambda n: n.GetCurrentEntry().GetSymbolic())]:
            try:
                ptr = getattr(self._PySpin, cls_name)(node)
                return conv(ptr.GetValue() if cls_name != "CEnumerationPtr" else ptr)
            except Exception:
                continue
        raise RuntimeError(f"Could not read {name}")

    def set_param(self, name: str, value: Any) -> None:
        node_name = self.PARAM_TO_NODE.get(name)
        if not node_name:
            raise KeyError(f"Unknown param: {name}")
        nm = self._cam.GetNodeMap()
        node = nm.GetNode(node_name)
        if node is None:
            raise KeyError(f"Node '{node_name}' not on this camera")

        # Try float, int, then enumeration
        try:
            self._PySpin.CFloatPtr(node).SetValue(float(value))
            return
        except Exception:
            pass
        try:
            self._PySpin.CIntegerPtr(node).SetValue(int(value))
            return
        except Exception:
            pass
        try:
            enum = self._PySpin.CEnumerationPtr(node)
            entry = enum.GetEntryByName(str(value))
            enum.SetIntValue(entry.GetValue())
            return
        except Exception:
            pass
        raise RuntimeError(f"Could not set {name}={value}")

    def start_acquisition(self) -> None:
        if self._is_acquiring:
            return
        self._cam.BeginAcquisition()
        self._is_acquiring = True

    def stop_acquisition(self) -> None:
        if not self._is_acquiring:
            return
        self._cam.EndAcquisition()
        self._is_acquiring = False

    def acquire_frame(self, timeout_ms: int = 5000) -> np.ndarray:
        img = self._cam.GetNextImage(timeout_ms)
        try:
            if img.IsIncomplete():
                raise RuntimeError(f"Incomplete image: {img.GetImageStatus()}")
            arr = np.array(img.GetNDArray(), copy=True)
            return arr
        finally:
            img.Release()

    def software_trigger(self) -> None:
        nm = self._cam.GetNodeMap()
        cmd = self._PySpin.CCommandPtr(nm.GetNode("TriggerSoftware"))
        cmd.Execute()
