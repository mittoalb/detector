"""
Adapter that makes the EPICS IOC's CamPlugin look like DummyOrcaFireDetector
to the Qt GUI. Both the GUI and EPICS clients control the same camera.
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)

from orca.dummy_detector import DummyOrcaFireDetector
from orca.epics_ad_server import OrcaFireIOC


# Map between the Qt GUI parameter names and the IOC's cam1: PV attributes.
# Only parameters that have a direct cam1: equivalent are mapped here.
# All other parameters are handled by the DummyOrcaFireDetector defaults.
_CAM_PV_MAP = {
    "ExposureTime": ("AcquireTime", None, None),
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
        self._last_frame_time = 0.0
        self._fps_counter = 0
        self._fps_last_update = 0.0

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

    # Map GUI binning value to frame grabber BinningMethod
    _BINNING_MAP = {
        1: "Disable",
        2: "Mean_2x2",
        4: "Mean_4x4",
    }

    # Full-sensor dimensions
    _SENSOR_WIDTH = 4480
    _SENSOR_HEIGHT = 2368

    def _sync_to_ioc(self, name, value):
        """Push a parameter change to the corresponding IOC PV and camera."""
        acquirer = self._cam._acquirer

        if name == "ExposureTime":
            self._cam.AcquireTime._data["value"] = float(value)
            self._cam.AcquireTime_RBV._data["value"] = float(value)
            if acquirer:
                try:
                    acquirer.configure({"ExposureTime": float(value)})
                except Exception as exc:
                    logger.warning("Failed to set ExposureTime: %s", exc)
            self._update_frame_rate()
        elif name in ("BinningHorizontal", "BinningVertical"):
            binval = int(value)
            self._cam.BinX._data["value"] = binval
            self._cam.BinX_RBV._data["value"] = binval
            self._cam.BinY._data["value"] = binval
            self._cam.BinY_RBV._data["value"] = binval
            with self._lock:
                self._parameters["BinningHorizontal"] = binval
                self._parameters["BinningVertical"] = binval
            method = self._BINNING_MAP.get(binval)
            if method and acquirer:
                was_running = self._running
                if was_running:
                    self.stop_acquisition()
                acquirer.configure_stream({"BinningMethod": method})
                if was_running:
                    self.start_acquisition()
            elif binval not in self._BINNING_MAP:
                logger.warning("Binning %d not supported (use 1, 2, or 4)", binval)
            self._update_frame_size(binval)
        elif name == "TriggerMode":
            self._cam.TriggerMode._data["value"] = str(value)
            self._cam.TriggerMode_RBV._data["value"] = str(value)
            if acquirer:
                mode = "Off" if str(value) == "Off" else "On"
                acquirer.set_trigger_mode(mode)
        elif name == "TriggerSource":
            self._cam.TriggerSource._data["value"] = str(value)
            if acquirer:
                source_map = {
                    "Internal": None,
                    "External": "TTLIO11",
                    "BNC": "TTLIO11",
                    "Software": "Software",
                }
                src = source_map.get(str(value))
                if src == "Software":
                    acquirer.set_trigger_mode("Software")
                elif src:
                    acquirer.configure_external_trigger(source=src)
        elif name == "TriggerPolarity":
            if acquirer:
                act = "RisingEdge" if str(value) == "Positive" else "FallingEdge"
                acquirer.configure_external_trigger(activation=act)
        elif name == "AcquisitionMode":
            mode_map = {"Continuous": "Continuous", "SingleFrame": "Single",
                        "MultiFrame": "Multiple"}
            self._cam.ImageMode._data["value"] = mode_map.get(str(value), "Multiple")
        elif name == "AcquisitionFrameCount":
            self._cam.NumImages._data["value"] = int(value)

    def _update_frame_size(self, binval):
        """Update Width/Height parameters after binning change."""
        w = self._SENSOR_WIDTH // binval
        h = self._SENSOR_HEIGHT // binval
        with self._lock:
            self._parameters["Width"] = w
            self._parameters["Height"] = h
        self._cam.SizeX_RBV._data["value"] = w
        self._cam.SizeY_RBV._data["value"] = h
        self._cam.ArraySize0_RBV._data["value"] = w
        self._cam.ArraySize1_RBV._data["value"] = h
        self._notify_parameter_change("Width", w)
        self._notify_parameter_change("Height", h)

    def _update_frame_rate(self):
        """Measure and update actual frame rate from recent frames."""
        # Will be computed from actual frame timing in _on_ioc_frame
        pass

    def _on_ioc_frame(self, frame: np.ndarray, metadata: dict):
        """Receive frames from the IOC acquisition loop and forward to GUI."""
        import time as _time
        now = _time.time()
        self._last_frame = frame
        self._fps_counter += 1

        # Update frame rate every second
        elapsed = now - self._fps_last_update
        if elapsed >= 1.0:
            fps = self._fps_counter / elapsed
            self._fps_counter = 0
            self._fps_last_update = now
            with self._lock:
                self._parameters["AcquisitionFrameRate"] = round(fps, 1)
            self._notify_parameter_change("AcquisitionFrameRate", round(fps, 1))

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
