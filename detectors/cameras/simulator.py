"""
Simulated camera for testing without hardware.

Generates synthetic frames at the configured resolution and exposure time.
Implements the full BaseCamera interface so the IOC and GUI can be tested
without any real camera connected.
"""

import logging
import threading
import time
from typing import Any, List

import numpy as np

from detectors.core.base import BaseCamera, CameraInfo
from detectors.core.registry import register

logger = logging.getLogger(__name__)


@register
class SimulatedCamera(BaseCamera):
    """Simulated camera that generates noise+pattern frames."""

    camera_type = "simulator"
    display_name = "Simulated Camera"

    def __init__(self, device_index: int = 0, width: int = 4432,
                 height: int = 2368, **kwargs):
        super().__init__(device_index=device_index, **kwargs)
        self._params = {
            "ExposureTime": 0.01,
            "Width": width,
            "Height": height,
            "OffsetX": 0,
            "OffsetY": 0,
            "BinningHorizontal": 1,
            "BinningVertical": 1,
            "PixelFormat": "Mono16",
            "TriggerMode": "Off",
            "TriggerSource": "Internal",
            "TriggerActive": "Edge",
            "TriggerPolarity": "Positive",
            "TriggerDelay": 0.0,
            "SensorMode": "Area",
            "ReadoutSpeed": "Fastest",
            "ShutterMode": "Rolling",
            "SensorTemperature": 22.5,
            "SensorCooler": "Off",
            "SensorCoolerStatus": "Off",
            "AcquisitionFrameRate": 0.0,
        }
        self._software_trigger = threading.Event()

    def open(self) -> None:
        self._info = CameraInfo(
            vendor="Simulator", model="Synthetic",
            sensor_width=self._params["Width"],
            sensor_height=self._params["Height"],
            bits_per_pixel=16, pixel_size_um=4.6, max_frame_rate=120.0,
            supported_binning=[1, 2, 4],
            supported_trigger_modes=["Internal", "External", "Software"],
            has_temperature=True, has_cooler=True, has_subarray=True,
        )
        logger.info("Simulated camera opened: %dx%d",
                    self._info.sensor_width, self._info.sensor_height)

    def close(self) -> None:
        if self._is_acquiring:
            self.stop_acquisition()
        self._info = None

    def list_params(self) -> List[str]:
        return list(self._params.keys())

    def get_param(self, name: str) -> Any:
        if name not in self._params:
            raise KeyError(f"Unknown param: {name}")
        return self._params[name]

    def set_param(self, name: str, value: Any) -> None:
        if name not in self._params:
            raise KeyError(f"Unknown param: {name}")
        # Type-coerce based on existing type
        existing = self._params[name]
        if isinstance(existing, int) and not isinstance(value, str):
            value = int(value)
        elif isinstance(existing, float) and not isinstance(value, str):
            value = float(value)
        self._params[name] = value

    def start_acquisition(self) -> None:
        self._is_acquiring = True

    def stop_acquisition(self) -> None:
        self._is_acquiring = False
        self._software_trigger.set()

    def acquire_frame(self, timeout_ms: int = 5000) -> np.ndarray:
        if self._params["TriggerMode"] != "Off":
            self._software_trigger.wait(timeout=timeout_ms / 1000)
            self._software_trigger.clear()
            if not self._is_acquiring:
                raise RuntimeError("Acquisition stopped")
        else:
            time.sleep(self._params["ExposureTime"])

        binval = self._params["BinningHorizontal"]
        w = self._params["Width"] // binval
        h = self._params["Height"] // binval
        frame = np.random.randint(100, 1000, (h, w), dtype=np.uint16)
        # Add a moving pattern based on time
        t = int(time.time() * 10) % h
        frame[t:t + 5] = 60000
        return frame

    def software_trigger(self) -> None:
        self._software_trigger.set()
