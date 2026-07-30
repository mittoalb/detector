"""
Teledyne (Photometrics) Kinetix camera via PVCAM SDK.

Requires:
    - PVCAM SDK installed (libpvcam.so)
    - PyVCAM Python bindings (`pip install pyvcam`)

Reference: https://www.photometrics.com/products/kinetix-family
"""

import logging
from typing import Any, List, Optional

import numpy as np

from detectors.core.base import BaseCamera, CameraInfo
from detectors.core.registry import register

logger = logging.getLogger(__name__)


@register
class TeledyneKinetix(BaseCamera):
    """Teledyne Photometrics Kinetix via PVCAM."""

    camera_type = "teledyne.kinetix"
    display_name = "Teledyne Photometrics Kinetix (PVCAM)"

    def __init__(self, device_index: int = 0, **kwargs):
        super().__init__(device_index=device_index, **kwargs)
        try:
            from pyvcam import pvc
            from pyvcam.camera import Camera
        except ImportError as exc:
            raise RuntimeError(
                "pyvcam required. Install PVCAM SDK and `pip install pyvcam`"
            ) from exc
        self._pvc = pvc
        self._Camera = Camera
        self._cam = None

    def open(self) -> None:
        if self._cam is not None:
            return
        self._pvc.init_pvcam()
        cameras = list(self._Camera.detect_camera())
        if self.device_index >= len(cameras):
            self._pvc.uninit_pvcam()
            raise RuntimeError(
                f"No Kinetix at index {self.device_index} "
                f"(found {len(cameras)} cameras)")
        self._cam = cameras[self.device_index]
        self._cam.open()

        try:
            sn = self._cam.serial_no
        except Exception:
            sn = ""

        self._info = CameraInfo(
            vendor="Teledyne",
            model=getattr(self._cam, "chip_name", "Kinetix"),
            serial_number=sn,
            sensor_width=self._cam.sensor_size[0],
            sensor_height=self._cam.sensor_size[1],
            bits_per_pixel=16,
            max_frame_rate=500.0,
            supported_binning=[1, 2, 4],
            supported_trigger_modes=["Internal", "External", "Software"],
            has_temperature=True, has_cooler=True, has_subarray=True,
        )
        logger.info("Opened %s SN=%s", self.display_name, sn)

    def close(self) -> None:
        if self._is_acquiring:
            try:
                self.stop_acquisition()
            except Exception:
                pass
        if self._cam is not None:
            self._cam.close()
            self._cam = None
        self._pvc.uninit_pvcam()
        self._info = None

    def list_params(self) -> List[str]:
        return ["ExposureTime", "Width", "Height", "BinningHorizontal",
                "BinningVertical", "SensorTemperature", "SensorTemperatureTarget",
                "ReadoutSpeed", "TriggerMode", "TriggerSource"]

    def get_param(self, name: str) -> Any:
        if name == "ExposureTime":
            return self._cam.exp_time / 1000.0  # PVCAM uses ms
        if name == "Width":
            return self._cam.sensor_size[0]
        if name == "Height":
            return self._cam.sensor_size[1]
        if name in ("BinningHorizontal", "BinningVertical"):
            return self._cam.binning
        if name == "SensorTemperature":
            return self._cam.temp
        if name == "SensorTemperatureTarget":
            return self._cam.temp_setpoint
        raise KeyError(f"Unknown param: {name}")

    def set_param(self, name: str, value: Any) -> None:
        if name == "ExposureTime":
            self._cam.exp_time = int(float(value) * 1000)
            return
        if name in ("BinningHorizontal", "BinningVertical"):
            self._cam.binning = int(value)
            return
        if name == "SensorTemperatureTarget":
            self._cam.temp_setpoint = float(value)
            return
        raise KeyError(f"Param '{name}' not implemented")

    def start_acquisition(self) -> None:
        if self._is_acquiring:
            return
        self._cam.start_live()
        self._is_acquiring = True

    def stop_acquisition(self) -> None:
        if not self._is_acquiring:
            return
        self._cam.finish()
        self._is_acquiring = False

    def acquire_frame(self, timeout_ms: int = 5000) -> np.ndarray:
        frame, _, _ = self._cam.poll_frame(timeout_ms=timeout_ms)
        return frame['pixel_data'].copy()
