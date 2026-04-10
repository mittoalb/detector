"""
Adapter that makes the EPICS IOC's CamPlugin look like DummyOrcaFireDetector
to the Qt GUI. Both the GUI and EPICS clients control the same camera.
"""

import numpy as np

from orca.dummy_detector import DummyOrcaFireDetector
from orca.epics_ad_server import OrcaFireIOC


# Map between the Qt GUI parameter names and the IOC's cam1: PV attributes.
# Only parameters that have a direct cam1: equivalent are mapped here.
# All other parameters are handled by the DummyOrcaFireDetector defaults.
_CAM_PV_MAP = {
    "ExposureTime": ("AcquireTime", lambda v: v * 1e6, lambda v: v / 1e6),
    "AcquisitionFrameRate": ("AcquirePeriod", None, None),
    "BinningHorizontal": ("BinX", None, None),
    "BinningVertical": ("BinY", None, None),
    "TriggerMode": ("TriggerMode", None, None),
    "TriggerSource": ("TriggerSource", None, None),
    "Width": ("SizeX_RBV", None, None),
    "Height": ("SizeY_RBV", None, None),
}


class IOCBackend(DummyOrcaFireDetector):
    """Backend adapter that wraps the IOC so the Qt GUI can control it.

    Inherits from DummyOrcaFireDetector to get the full parameter set and
    GUI-compatible interface. Overrides acquisition to delegate to the IOC's
    CamPlugin, which owns the real camera connection.
    """

    def __init__(self, ioc: OrcaFireIOC):
        # Skip DummyOrcaFireDetector.__init__ (don't open camera twice).
        # Instead, manually init the fields the GUI needs.
        import threading
        from typing import Any, Callable, Dict, List, Optional

        self._parameters: Dict[str, Any] = dict(self.DEFAULT_PARAMETERS)
        self._lock = threading.RLock()
        self._running = False
        self._thread = None
        self._frame_callbacks: List[Callable] = []
        self._param_callbacks: List[Callable] = []
        self._last_frame: Optional[np.ndarray] = None
        self._acquirer = None

        self._ioc = ioc
        self._cam = ioc.cam1

        # Determine camera connection from IOC
        self._camera_connected = self._cam._acquirer is not None

        # Populate identity from IOC
        if self._camera_connected and self._cam._acquirer is not None:
            try:
                info = self._cam._acquirer.describe_device()
                self._parameters["DeviceModelName"] = info.get("model", "ORCA Fire")
                self._parameters["DeviceSerialNumber"] = info.get("serial_number", "")
            except Exception:
                pass

        # Register for frame callbacks from the IOC's acquisition loop
        self._cam.register_frame_callback(self._on_ioc_frame)

    def start_acquisition(self):
        """Delegate to IOC CamPlugin."""
        self._cam.Acquire._data["value"] = 1
        self._cam._start_acquisition()
        with self._lock:
            self._running = True
            self._parameters["Acquire"] = 1
        self._notify_parameter_change("Acquire", 1)
        self._notify_parameter_change("StatusMessage", "Acquiring")

    def stop_acquisition(self):
        """Delegate to IOC CamPlugin."""
        self._cam._stop_acquisition()
        self._cam.Acquire._data["value"] = 0
        with self._lock:
            self._running = False
            self._parameters["Acquire"] = 0
            self._parameters["StatusMessage"] = "Idle"
        self._notify_parameter_change("Acquire", 0)
        self._notify_parameter_change("StatusMessage", "Idle")

    def set_parameter(self, name, value):
        """Set parameter locally and push to IOC PVs where applicable."""
        super().set_parameter(name, value)
        self._sync_to_ioc(name, value)

    def _sync_to_ioc(self, name, value):
        """Push a parameter change to the corresponding IOC PV."""
        if name == "ExposureTime":
            # GUI uses microseconds, IOC uses seconds
            self._cam.AcquireTime._data["value"] = float(value) / 1e6
            self._cam.AcquireTime_RBV._data["value"] = float(value) / 1e6
        elif name == "BinningHorizontal":
            self._cam.BinX._data["value"] = int(value)
            self._cam.BinX_RBV._data["value"] = int(value)
        elif name == "BinningVertical":
            self._cam.BinY._data["value"] = int(value)
            self._cam.BinY_RBV._data["value"] = int(value)
        elif name == "TriggerMode":
            self._cam.TriggerMode._data["value"] = str(value)
            self._cam.TriggerMode_RBV._data["value"] = str(value)
        elif name == "AcquisitionMode":
            mode_map = {"Continuous": "Continuous", "SingleFrame": "Single",
                        "MultiFrame": "Multiple"}
            self._cam.ImageMode._data["value"] = mode_map.get(str(value), "Multiple")
        elif name == "AcquisitionFrameCount":
            self._cam.NumImages._data["value"] = int(value)

    def _on_ioc_frame(self, frame: np.ndarray, metadata: dict):
        """Receive frames from the IOC acquisition loop and forward to GUI."""
        self._last_frame = frame
        with self._lock:
            counter = metadata.get("frame_number", 0)
            self._parameters["ImageCounter"] = counter
            self._parameters["StatusMessage"] = "Acquiring"

        # Build full metadata dict for GUI callbacks
        gui_metadata = self.list_parameters()
        gui_metadata.update(metadata)
        for cb in self._frame_callbacks:
            cb(frame, gui_metadata)

        self._notify_parameter_change("ImageCounter", counter)

    def _notify_parameter_change(self, name, value):
        for cb in self._param_callbacks:
            cb(name, value)

    def acquire_frame(self) -> np.ndarray:
        """Single frame acquisition via the IOC backend."""
        frame = self._cam._acquire_one_frame(
            float(self._cam.AcquireTime.value)
        )
        if frame is not None:
            self._last_frame = frame
            return frame
        # Fallback: generate simulation frame
        return self._generate_image()
