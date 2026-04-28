"""
Abstract base class for camera acquirers.

All camera-specific backends (Hamamatsu DCAM, Teledyne Spinnaker/PVCAM,
Tucsen, etc.) implement this interface so the IOC and GUI work uniformly.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

import numpy as np


class CameraInfo:
    """Static description of a camera model.

    Attributes:
        vendor: Manufacturer name (e.g. "Hamamatsu").
        model: Model number (e.g. "C16240-20UP").
        sensor_width: Native sensor width in pixels.
        sensor_height: Native sensor height in pixels.
        bits_per_pixel: Bit depth (typically 16).
        pixel_size_um: Pixel size in micrometers.
        max_frame_rate: Maximum native frame rate (fps).
        supported_binning: List of supported binning factors (e.g. [1, 2, 4]).
        supported_trigger_modes: List of trigger source names.
        has_temperature: Camera reports sensor temperature.
        has_cooler: Camera has a controllable cooler.
        has_subarray: Camera supports software ROI / sub-array.
    """

    def __init__(self, vendor: str = "", model: str = "",
                 sensor_width: int = 0, sensor_height: int = 0,
                 bits_per_pixel: int = 16, pixel_size_um: float = 0.0,
                 max_frame_rate: float = 0.0,
                 supported_binning: Optional[List[int]] = None,
                 supported_trigger_modes: Optional[List[str]] = None,
                 has_temperature: bool = False,
                 has_cooler: bool = False,
                 has_subarray: bool = False,
                 serial_number: str = "",
                 firmware_version: str = ""):
        self.vendor = vendor
        self.model = model
        self.sensor_width = sensor_width
        self.sensor_height = sensor_height
        self.bits_per_pixel = bits_per_pixel
        self.pixel_size_um = pixel_size_um
        self.max_frame_rate = max_frame_rate
        self.supported_binning = supported_binning or [1]
        self.supported_trigger_modes = supported_trigger_modes or ["Internal"]
        self.has_temperature = has_temperature
        self.has_cooler = has_cooler
        self.has_subarray = has_subarray
        self.serial_number = serial_number
        self.firmware_version = firmware_version

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vendor": self.vendor,
            "model": self.model,
            "sensor_width": self.sensor_width,
            "sensor_height": self.sensor_height,
            "bits_per_pixel": self.bits_per_pixel,
            "pixel_size_um": self.pixel_size_um,
            "max_frame_rate": self.max_frame_rate,
            "supported_binning": self.supported_binning,
            "supported_trigger_modes": self.supported_trigger_modes,
            "has_temperature": self.has_temperature,
            "has_cooler": self.has_cooler,
            "has_subarray": self.has_subarray,
            "serial_number": self.serial_number,
            "firmware_version": self.firmware_version,
        }


# Standard parameter names that backends should support where applicable
STANDARD_PARAMS = {
    # Acquisition
    "ExposureTime": "float",       # seconds
    "AcquisitionFrameRate": "float",  # fps (may be read-only)

    # Image format
    "Width": "int",                # pixels
    "Height": "int",               # pixels
    "OffsetX": "int",
    "OffsetY": "int",
    "BinningHorizontal": "int",    # 1, 2, 4, etc.
    "BinningVertical": "int",
    "PixelFormat": "str",          # "Mono16", "Mono12", etc.

    # Trigger
    "TriggerMode": "str",          # "Off" (free-run), "On"
    "TriggerSource": "str",        # "Internal", "External", "Software"
    "TriggerActive": "str",        # "Edge", "Level"
    "TriggerPolarity": "str",      # "Positive", "Negative"
    "TriggerDelay": "float",       # seconds

    # Sensor
    "SensorMode": "str",           # "Area", "Lightsheet"
    "ReadoutSpeed": "str",         # "Fastest", "Slowest"
    "ShutterMode": "str",          # "Rolling", "Global"

    # Cooling
    "SensorTemperature": "float",      # Celsius (read-only)
    "SensorCooler": "str",             # "Off", "On", "Max"
    "SensorCoolerStatus": "str",       # "Off", "Ready", "Busy", "Error"
    "SensorTemperatureTarget": "float",
}


class BaseCamera(ABC):
    """Abstract base class for camera backends.

    Concrete subclasses implement camera-specific control via vendor SDKs
    (DCAM, Spinnaker, PVCAM, Tucsen, GenTL, etc.).
    """

    # Unique identifier for this camera type, e.g. "hamamatsu.orca_fire_dcam"
    camera_type: str = ""

    # Display name shown in GUI/logs
    display_name: str = ""

    def __init__(self, device_index: int = 0, **kwargs):
        self.device_index = device_index
        self._is_acquiring = False
        self._frame_callbacks: List[Callable[[np.ndarray, dict], None]] = []
        self._frame_counter = 0
        self._info: Optional[CameraInfo] = None

    # --- Lifecycle ---

    @abstractmethod
    def open(self) -> None:
        """Open the camera device. Raises on failure."""

    @abstractmethod
    def close(self) -> None:
        """Close the camera and release all resources."""

    def is_open(self) -> bool:
        return self._info is not None

    # --- Identity ---

    def get_info(self) -> CameraInfo:
        """Return static camera information."""
        if self._info is None:
            raise RuntimeError("Camera not open")
        return self._info

    # --- Parameters ---

    @abstractmethod
    def get_param(self, name: str) -> Any:
        """Read a parameter by standard name."""

    @abstractmethod
    def set_param(self, name: str, value: Any) -> None:
        """Set a parameter by standard name."""

    @abstractmethod
    def list_params(self) -> List[str]:
        """List all parameter names supported by this camera."""

    def is_param_supported(self, name: str) -> bool:
        """Check if a parameter is supported."""
        return name in self.list_params()

    def is_param_writable(self, name: str) -> bool:
        """Check if a parameter can be written. Default: try to read attribute."""
        return self.is_param_supported(name)

    # --- Acquisition ---

    @abstractmethod
    def start_acquisition(self) -> None:
        """Start continuous acquisition."""

    @abstractmethod
    def stop_acquisition(self) -> None:
        """Stop acquisition."""

    def is_acquiring(self) -> bool:
        return self._is_acquiring

    @abstractmethod
    def acquire_frame(self, timeout_ms: int = 5000) -> np.ndarray:
        """Wait for and return a single frame as a numpy array (height, width)."""

    def software_trigger(self) -> None:
        """Fire a software trigger. Default: not supported."""
        raise NotImplementedError(
            f"Software trigger not supported on {self.camera_type}")

    # --- Frame callbacks ---

    def register_frame_callback(self,
                                 callback: Callable[[np.ndarray, dict], None]) -> None:
        """Register a callback to be called for each acquired frame."""
        self._frame_callbacks.append(callback)

    def unregister_frame_callback(self,
                                   callback: Callable[[np.ndarray, dict], None]) -> None:
        if callback in self._frame_callbacks:
            self._frame_callbacks.remove(callback)

    def _notify_frame(self, frame: np.ndarray, metadata: dict) -> None:
        """Call all registered frame callbacks. Subclasses use this internally."""
        for cb in self._frame_callbacks:
            try:
                cb(frame, metadata)
            except Exception:
                # Don't let one bad callback break the chain
                import logging
                logging.getLogger(__name__).exception("Frame callback error")
