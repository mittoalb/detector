"""
Hamamatsu ORCA Fire via Harvesters/GenTL (Euresys Coaxlink fallback).

Use when DCAM is not available. Provides limited control:
- ExposureTime (only writable parameter via CXP)
- Frame grabber binning (FPGA-side, no readout speedup)
- Frame grabber-gated triggering
"""

import logging
import os
import threading
from pathlib import Path
from typing import Any, List, Optional

import numpy as np

from detectors.core.base import BaseCamera, CameraInfo
from detectors.core.registry import register

logger = logging.getLogger(__name__)


DEFAULT_GENTL_PATHS = [
    "/opt/euresys/egrabber/lib/x86_64/coaxlink.cti",
    "/opt/euresys/egrabber/lib/coaxlink.cti",
    "/opt/euresys/GenTL/Producer/x86_64/coaxlink.cti",
]


def _find_gentl_producer() -> Optional[str]:
    env = os.environ.get("EURESYS_GENTL_PRODUCER")
    if env and Path(env).exists():
        return env
    for p in DEFAULT_GENTL_PATHS:
        if Path(p).exists():
            return p
    return None


@register
class OrcaFireGenTL(BaseCamera):
    """ORCA Fire via Harvesters / Euresys Coaxlink GenTL.

    Limited functionality - prefer OrcaFireDCAM for full control.
    """

    camera_type = "hamamatsu.orca_fire_gentl"
    display_name = "Hamamatsu ORCA Fire (GenTL/Euresys)"

    BINNING_MAP = {1: "Disable", 2: "Mean_2x2", 4: "Mean_4x4"}

    def __init__(self, device_index: int = 0,
                 gen_tl_producer_path: Optional[str] = None, **kwargs):
        super().__init__(device_index=device_index, **kwargs)
        try:
            from harvesters.core import Harvester
        except ImportError as exc:
            raise RuntimeError("harvesters package required for GenTL backend") from exc

        path = gen_tl_producer_path or _find_gentl_producer()
        if not path:
            raise FileNotFoundError("Euresys GenTL producer not found")

        self._harvester = Harvester()
        self._harvester.add_file(path)
        self._harvester.update()
        self._image_acquirer = None

    def open(self) -> None:
        if self._image_acquirer is not None:
            return
        if self.device_index >= len(self._harvester.device_info_list):
            raise IndexError(f"Device index {self.device_index} out of range")

        self._image_acquirer = self._harvester.create(self.device_index)

        # Configure data stream
        try:
            nm = self._image_acquirer.remote_device.node_map
            ds = self._image_acquirer.data_streams[0]
            dsnm = ds.node_map
            dsnm.ImageFormatSource.value = 'DataStream'
            dsnm.ImageFormatSelector.value = 'DataStream'
            dsnm.RemoteWidth.value = nm.Width.value
            dsnm.RemoteHeight.value = nm.Height.value
            dsnm.RemotePixelFormat.value = str(nm.PixelFormat.value)
            w, h = int(dsnm.Width.value), int(dsnm.Height.value)
        except Exception as exc:
            logger.warning("Stream config failed: %s", exc)
            w, h = 4480, 2368

        self._info = CameraInfo(
            vendor="Hamamatsu", model="C16240-20UP",
            sensor_width=w, sensor_height=h,
            bits_per_pixel=16, pixel_size_um=4.6,
            max_frame_rate=115.0,
            supported_binning=[1, 2, 4],
            supported_trigger_modes=["Internal"],
            has_temperature=False, has_cooler=False, has_subarray=False,
        )
        logger.info("Opened %s: %dx%d", self.display_name, w, h)

    def close(self) -> None:
        if self._is_acquiring:
            try:
                self.stop_acquisition()
            except Exception:
                pass
        if self._image_acquirer is not None:
            self._image_acquirer.destroy()
            self._image_acquirer = None
        self._harvester.reset()
        self._info = None

    def list_params(self) -> List[str]:
        return ["ExposureTime", "Width", "Height", "PixelFormat",
                "BinningHorizontal", "BinningVertical"]

    def get_param(self, name: str) -> Any:
        nm = self._image_acquirer.remote_device.node_map
        if name == "ExposureTime":
            return float(nm.ExposureTime.value)
        if name == "Width":
            return int(nm.Width.value)
        if name == "Height":
            return int(nm.Height.value)
        if name == "PixelFormat":
            return str(nm.PixelFormat.value)
        if name in ("BinningHorizontal", "BinningVertical"):
            return 1  # Default - frame grabber binning isn't reflected here
        raise KeyError(f"Unknown param: {name}")

    def set_param(self, name: str, value: Any) -> None:
        nm = self._image_acquirer.remote_device.node_map
        if name == "ExposureTime":
            nm.ExposureTime.value = float(value)
            return
        if name in ("BinningHorizontal", "BinningVertical"):
            method = self.BINNING_MAP.get(int(value))
            if method:
                ds = self._image_acquirer.data_streams[0]
                ds.node_map.BinningMethod.value = method
            return
        raise KeyError(f"Param '{name}' not writable via GenTL")

    def start_acquisition(self) -> None:
        if self._is_acquiring:
            return
        self._image_acquirer.start()
        self._is_acquiring = True

    def stop_acquisition(self) -> None:
        if not self._is_acquiring:
            return
        self._image_acquirer.stop()
        self._is_acquiring = False

    def acquire_frame(self, timeout_ms: int = 5000) -> np.ndarray:
        if not self._is_acquiring:
            raise RuntimeError("Acquisition not running")
        with self._image_acquirer.fetch(timeout=timeout_ms) as buffer:
            comp = buffer.payload.components[0]
            arr = np.array(comp.data, dtype=np.uint16, copy=True)
            return arr.reshape((comp.height, comp.width))
