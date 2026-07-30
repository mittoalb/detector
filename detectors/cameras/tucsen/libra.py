"""
Tucsen Libra 5514 Pro camera via TUCAM SDK.

Requires:
    - TUCAM SDK installed (libTUCam.so.1)
    - Python ctypes wrapper (Tucsen ships pyTUCam examples)

Reference: https://www.tucsen.com/products/libra-series/
"""

import ctypes
import logging
from typing import Any, List, Optional

import numpy as np

from detectors.core.base import BaseCamera, CameraInfo
from detectors.core.registry import register

logger = logging.getLogger(__name__)


# TUCAM property IDs (from TUCamApi.h)
TUIDP = {
    "ExposureTime": 0x10,        # actually TUCAM_IDPROP_EXPOSURETM
    "GlobalGain": 0x11,
    "Brightness": 0x12,
    "BlackLevel": 0x13,
    "Sharpness": 0x14,
    "Saturation": 0x15,
    "Hue": 0x16,
    "Gamma": 0x17,
    "SensorTemperature": 0x21,
    "FanSpeed": 0x22,
    "ATWGain": 0x32,
    "ATWBalance": 0x33,
}

TUIDC = {
    "Resolution": 0x00,
    "PixelFormat": 0x01,
    "Binning": 0x02,
    "TriggerMode": 0x03,
    "TriggerSource": 0x04,
    "ATExposure": 0x05,
}


@register
class TucsenLibra(BaseCamera):
    """Tucsen Libra 5514 Pro via TUCAM SDK."""

    camera_type = "tucsen.libra_5514pro"
    display_name = "Tucsen Libra 5514 Pro (TUCAM)"

    DEFAULT_LIB_PATHS = [
        "/usr/lib64/libTUCam.so.1",
        "/usr/local/lib/libTUCam.so.1",
        "/opt/tucsen/lib/libTUCam.so.1",
    ]

    def __init__(self, device_index: int = 0,
                 lib_path: Optional[str] = None, **kwargs):
        super().__init__(device_index=device_index, **kwargs)
        from pathlib import Path
        path = lib_path
        if not path:
            for p in self.DEFAULT_LIB_PATHS:
                if Path(p).exists():
                    path = p
                    break
        if not path:
            raise RuntimeError(
                "TUCAM library (libTUCam.so.1) not found. "
                "Install Tucsen SDK from https://www.tucsen.com")
        try:
            self._lib = ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
        except OSError as exc:
            raise RuntimeError(f"Failed to load TUCAM lib: {exc}") from exc
        self._handle = None

    def open(self) -> None:
        if self._handle is not None:
            return
        # NOTE: Stub implementation. Tucsen's Python bindings vary;
        # the SDK ships C examples that need to be wrapped via ctypes.
        # See `tucam-windows-sdk/example/python` in the Tucsen package.
        raise NotImplementedError(
            "Tucsen Libra backend skeleton — requires SDK wrapper integration. "
            "See detectors/cameras/tucsen/libra.py for guidance.")

    def close(self) -> None:
        if self._handle is None:
            return
        # TUCAM_Dev_Close, TUCAM_Api_Uninit
        self._handle = None
        self._info = None

    def list_params(self) -> List[str]:
        return ["ExposureTime", "Width", "Height", "BinningHorizontal",
                "BinningVertical", "SensorTemperature", "TriggerMode",
                "TriggerSource"]

    def get_param(self, name: str) -> Any:
        raise NotImplementedError("Tucsen backend not implemented")

    def set_param(self, name: str, value: Any) -> None:
        raise NotImplementedError("Tucsen backend not implemented")

    def start_acquisition(self) -> None:
        raise NotImplementedError("Tucsen backend not implemented")

    def stop_acquisition(self) -> None:
        pass

    def acquire_frame(self, timeout_ms: int = 5000) -> np.ndarray:
        raise NotImplementedError("Tucsen backend not implemented")
