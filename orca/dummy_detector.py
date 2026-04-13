import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class DummyOrcaFireDetector:
    """A software-only detector that simulates an ORCA Fire camera."""

    DEFAULT_PARAMETERS = {
        # --- Sensor info (read-only) ---
        "SensorWidth": 4432,
        "SensorHeight": 2368,
        "SensorPixelWidth": 4.6e-6,
        "SensorPixelHeight": 4.6e-6,
        "DeviceModelName": "C16240-20UP",
        "DeviceFamilyName": "ORCA-Fire",
        "DeviceSerialNumber": "00000000",
        "DeviceFirmwareVersion": "",

        # --- Image format / ROI ---
        "Width": 4432,
        "Height": 2368,
        "OffsetX": 0,
        "OffsetY": 0,
        "BinningHorizontal": 1,
        "BinningVertical": 1,
        "SubarrayMode": "OFF",
        "ReverseX": False,
        "ReverseY": False,

        # --- Pixel format ---
        "PixelFormat": "Mono16",
        "BitsPerChannel": 16,

        # --- Exposure / Acquisition ---
        "ExposureTime": 0.01,          # s  (range: ~7.3e-6 – 10 s)
        "AcquisitionMode": "Continuous",  # Continuous, SingleFrame, MultiFrame
        "AcquisitionFrameCount": 1,
        "AcquisitionFrameRate": 115.0,  # Hz  (max 115 fps full-res CXP)
        "Acquire": 0,
        "ImageCounter": 0,

        # --- Sensor / Readout ---
        "SensorMode": "Area",           # Area, Lightsheet, SplitView, DualLightsheet
        "ReadoutSpeed": "StandardScan",  # StandardScan, SlowScan
        "ReadoutDirection": "Forward",   # Forward, Backward, Bidirectional, ReverseBidirectional
        "ShutterMode": "Rolling",        # Rolling, GlobalReset
        "TriggerGlobalExposure": "DelayedReadout",  # DelayedReadout, GlobalReset

        # --- Cooling / Temperature ---
        "SensorTemperature": 20.0,
        "SensorCooler": "On",            # Off, On, Max
        "SensorTemperatureTarget": 20.0,
        "SensorCoolerStatus": "Ready",   # Ready, Busy, Error, Off

        # --- Trigger input ---
        "TriggerMode": "Off",            # Off (internal), On (external)
        "TriggerSource": "Internal",     # Internal, External, Software, MasterPulse
        "TriggerActive": "Edge",         # Edge, Level, SyncReadout, StartTrigger
        "TriggerPolarity": "Positive",   # Positive, Negative
        "TriggerConnector": "Interface", # Interface, BNC
        "TriggerDelay": 0.0,             # 0 us – 10 s
        "TriggerTimes": 1,

        # --- Output trigger ---
        "NumberOfOutputTriggerConnector": 3,
        "OutputTriggerKind": "ExposureTiming",  # ExposureTiming, ReadoutEnd, TriggerReady, Programmable, High, Low
        "OutputTriggerPolarity": "Positive",
        "OutputTriggerActive": "Edge",          # Edge, Level
        "OutputTriggerDelay": 0.0,
        "OutputTriggerPeriod": 0.0,

        # --- Defect correction ---
        "DefectCorrectMode": "On",        # Off, On
        "HotPixelCorrectLevel": "Standard",  # Off, Standard, Aggressive

        # --- Contrast / Black level ---
        "ContrastGain": 1.0,
        "ContrastOffset": 0,
        "HighDynamicRangeMode": "Off",

        # --- Processing ---
        "RecursiveFilter": "Off",
        "RecursiveFilterFrames": 2,
        "SpotNoiseReducer": "Off",
        "SensorGapCorrectMode": "On",

        # --- Timing info (read-only) ---
        "TimingReadoutTime": 0.0,
        "TimingCyclicTriggerPeriod": 0.0,
        "InternalFrameRate": 115.0,

        # --- Master pulse ---
        "MasterPulseMode": "Off",
        "MasterPulseInterval": 0.0,
        "MasterPulseBurstTimes": 1,

        # --- Status ---
        "StatusMessage": "Idle",
    }

    def __init__(self, gen_tl_producer_path: Optional[str] = None, device_index: int = 0):
        self._parameters: Dict[str, Any] = dict(self.DEFAULT_PARAMETERS)
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._frame_callbacks: List[Callable[[np.ndarray, Dict[str, Any]], None]] = []
        self._param_callbacks: List[Callable[[str, Any], None]] = []
        self._last_frame: Optional[np.ndarray] = None
        self._acquirer = None
        self._camera_connected = False

        # Try to connect to real hardware
        try:
            from orca.harvesters_orca_fire import OrcaFireAcquirer
            acq = OrcaFireAcquirer(gen_tl_producer_path=gen_tl_producer_path, device_index=device_index)
            acq.open()
            self._acquirer = acq
            self._camera_connected = True
            info = acq.describe_device()
            self._parameters["CameraModel"] = info.get("model", "ORCA Fire")
            self._parameters["CameraSerial"] = info.get("serial_number", "Unknown")
            logger.info("Real camera connected: %s", info)
        except Exception as exc:
            logger.info("No real camera available (%s), using simulation mode.", exc)
            self._acquirer = None
            self._camera_connected = False

    def list_parameters(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._parameters)

    def get_parameter(self, name: str) -> Any:
        with self._lock:
            if name not in self._parameters:
                raise KeyError(f"Unknown parameter: {name}")
            return self._parameters[name]

    def set_parameter(self, name: str, value: Any) -> None:
        with self._lock:
            if name not in self._parameters:
                raise KeyError(f"Unknown parameter: {name}")
            self._parameters[name] = value
        self._notify_parameter_change(name, value)

    pv_get = get_parameter
    pv_set = set_parameter

    def register_frame_callback(self, callback: Callable[[np.ndarray, Dict[str, Any]], None]) -> None:
        self._frame_callbacks.append(callback)

    def register_parameter_callback(self, callback: Callable[[str, Any], None]) -> None:
        self._param_callbacks.append(callback)

    def start_acquisition(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._parameters["Acquire"] = 1
            self._notify_parameter_change("Acquire", 1)
            self._thread = threading.Thread(target=self._run_acquisition, daemon=True)
            self._thread.start()

    def stop_acquisition(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._parameters["Acquire"] = 0
            self._notify_parameter_change("Acquire", 0)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def acquire_frame(self) -> np.ndarray:
        if self._acquirer is not None and self._acquirer._is_acquiring:
            frame = self._acquirer.acquire_frame()
        else:
            frame = self._generate_image()
        self._last_frame = frame
        return frame

    def acquire_frames(self, count: int) -> List[np.ndarray]:
        return [self.acquire_frame() for _ in range(count)]

    def _run_acquisition(self) -> None:
        frame_rate = float(self.get_parameter("AcquisitionFrameRate"))
        interval = 1.0 / max(frame_rate, 1.0)
        acq_mode = self.get_parameter("AcquisitionMode")
        num_images = int(self.get_parameter("AcquisitionFrameCount"))
        image_counter = 0

        # Apply current parameters to real camera
        if self._acquirer is not None:
            try:
                self._acquirer.configure({
                    "ExposureTime": float(self.get_parameter("ExposureTime")),
                    "Width": int(self.get_parameter("Width")),
                    "Height": int(self.get_parameter("Height")),
                })
                self._acquirer.start_acquisition()
            except Exception as exc:
                logger.error("Failed to start real acquisition: %s", exc)

        while True:
            with self._lock:
                if not self._running:
                    break

            if acq_mode == "SingleFrame" and image_counter >= 1:
                break
            if acq_mode == "MultiFrame" and image_counter >= num_images:
                break

            frame = self.acquire_frame()
            image_counter += 1
            with self._lock:
                self._parameters["ImageCounter"] = image_counter
                self._parameters["StatusMessage"] = "Acquiring"
            self._dispatch_frame(frame)
            if self._acquirer is None:
                time.sleep(interval)

        if self._acquirer is not None:
            try:
                self._acquirer.stop_acquisition()
            except Exception:
                pass

        with self._lock:
            self._running = False
            self._parameters["Acquire"] = 0
            self._parameters["StatusMessage"] = "Idle"
        self._notify_parameter_change("Acquire", 0)
        self._notify_parameter_change("StatusMessage", "Idle")

    def _generate_image(self) -> np.ndarray:
        binning_h = max(int(self.get_parameter("BinningHorizontal")), 1)
        binning_v = max(int(self.get_parameter("BinningVertical")), 1)
        roi_width = int(self.get_parameter("Width"))
        roi_height = int(self.get_parameter("Height"))
        width = max(1, roi_width // binning_h)
        height = max(1, roi_height // binning_v)

        x = np.linspace(0, 65535, width, dtype=np.uint32)
        y = np.linspace(0, 65535, height, dtype=np.uint32)
        image = np.outer(y, np.ones_like(x, dtype=np.uint32)) // 256
        offset = np.uint32(int(time.time() * 10) % 65536)
        image = ((image + offset) % 65536).astype(np.uint16)
        return image

    def _dispatch_frame(self, frame: np.ndarray) -> None:
        metadata = self.list_parameters()
        self._last_frame = frame
        for callback in self._frame_callbacks:
            callback(frame, metadata)

    def _notify_parameter_change(self, name: str, value: Any) -> None:
        for callback in self._param_callbacks:
            callback(name, value)

    def get_last_frame(self) -> Optional[np.ndarray]:
        return self._last_frame

    def is_camera_connected(self) -> bool:
        """Returns True if a real camera is connected, False if in simulation mode."""
        return self._camera_connected
