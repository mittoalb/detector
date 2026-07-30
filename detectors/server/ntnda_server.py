"""
PVAccess NTNDArray server for streaming frames to viewers like pystream.
"""

import logging
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class NTNDArrayServer:
    """Publish frames as NTNDArray PVs via pvaccess (pvapy)."""

    def __init__(self, pv_name: str, max_fps: float = 30.0):
        try:
            import pvaccess as pva
        except ImportError as exc:
            raise RuntimeError(
                "pvapy required for PVA streaming. `pip install pvapy`") from exc
        self._pva = pva
        self._pv_name = pv_name
        self._ntnda = pva.NtNdArray()
        self._server = pva.PvaServer(pv_name, self._ntnda)
        self._frame_id = 0
        self._min_interval = 1.0 / max_fps
        self._last_publish = 0.0
        logger.info("NTNDArray server: %s (max %d fps)", pv_name, int(max_fps))

    def publish_frame(self, frame: np.ndarray, metadata: dict) -> None:
        """Frame callback — publish as NTNDArray, throttled."""
        self._frame_id += 1
        now = time.time()
        if now - self._last_publish < self._min_interval:
            return
        self._last_publish = now

        ntnda = self._pva.NtNdArray()
        ntnda.setUniqueId(self._frame_id)

        if frame.dtype == np.uint16:
            ntnda["value"] = {"ushortValue": np.ascontiguousarray(frame.flatten())}
        elif frame.dtype == np.uint8:
            ntnda["value"] = {"ubyteValue": np.ascontiguousarray(frame.flatten())}
        else:
            arr = frame.astype(np.uint16, copy=False)
            ntnda["value"] = {"ushortValue": np.ascontiguousarray(arr.flatten())}

        height, width = frame.shape[:2]
        ntnda["dimension"] = [
            {"size": width, "offset": 0, "fullSize": width,
             "binning": 1, "reverse": False},
            {"size": height, "offset": 0, "fullSize": height,
             "binning": 1, "reverse": False},
        ]
        bytes_per_pixel = frame.dtype.itemsize
        ntnda["uncompressedSize"] = width * height * bytes_per_pixel
        ntnda["compressedSize"] = width * height * bytes_per_pixel
        self._server.update(ntnda)
