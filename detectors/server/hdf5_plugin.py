"""
HDF5 file writer plugin (HDF1:) — areaDetector-compatible.

Writes acquired frames to HDF5 files at /exchange/data dataset.
"""

import logging
import os
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from caproto.server import PVGroup, pvproperty

logger = logging.getLogger(__name__)


class HDF5Plugin(PVGroup):
    """areaDetector HDF1: PVs for HDF5 file writing."""

    NDArrayPort = pvproperty(value="CAM1", dtype=str, max_length=40)
    EnableCallbacks = pvproperty(value="Enable", dtype=str, max_length=40)

    FilePath = pvproperty(value="", dtype=str, max_length=256)
    FilePath_RBV = pvproperty(value="", dtype=str, max_length=256, read_only=True)
    FilePathExists_RBV = pvproperty(value=0, dtype=int, read_only=True)
    FileName = pvproperty(value="image", dtype=str, max_length=256)
    FileName_RBV = pvproperty(value="image", dtype=str, max_length=256, read_only=True)

    FileNumber = pvproperty(value=1, dtype=int)
    FileTemplate = pvproperty(value="%s%s_%4.4d.h5", dtype=str, max_length=256)
    FullFileName_RBV = pvproperty(value="", dtype=str, max_length=512, read_only=True)
    AutoIncrement = pvproperty(value="Yes", dtype=str, max_length=40)
    AutoSave = pvproperty(value="Yes", dtype=str, max_length=40)

    FileWriteMode = pvproperty(value="Stream", dtype=str, max_length=40)
    NumCapture = pvproperty(value=1, dtype=int)
    NumCaptured_RBV = pvproperty(value=0, dtype=int, read_only=True)
    Capture = pvproperty(value=0, dtype=int)
    Capture_RBV = pvproperty(value=0, dtype=int, read_only=True)
    WriteStatus = pvproperty(value="Idle", dtype=str, max_length=40, read_only=True)
    XMLFileName = pvproperty(value="", dtype=str, max_length=256)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()
        self._capturing = False
        self._h5_file = None
        self._h5_dataset = None
        self._frames_captured = 0

    @FilePath.putter
    async def FilePath(self, instance, value):
        path = str(value)
        await self.FilePath_RBV.write(path)
        exists = 1 if os.path.isdir(path) else 0
        await self.FilePathExists_RBV.write(exists)
        return path

    @FileName.putter
    async def FileName(self, instance, value):
        await self.FileName_RBV.write(str(value))
        return str(value)

    @Capture.putter
    async def Capture(self, instance, value):
        if value in (1, "Capture") and not self._capturing:
            self._start_capture()
            return 1
        elif value in (0, "Done") or (value not in (1, "Capture") and self._capturing):
            self._stop_capture()
            return 0
        return value

    def on_frame(self, frame: np.ndarray, metadata: dict) -> None:
        """Frame callback — write frame to HDF5 if capturing."""
        if not self._capturing:
            return
        with self._lock:
            if self._h5_file is None:
                return
            try:
                idx = self._frames_captured
                num_capture = int(self.NumCapture.value)

                # Resize dataset if needed
                if idx >= self._h5_dataset.shape[0]:
                    new_size = max(idx + 1, num_capture)
                    self._h5_dataset.resize((new_size, frame.shape[0],
                                             frame.shape[1]))

                self._h5_dataset[idx] = frame
                self._h5_file.flush()
                self._frames_captured += 1
                self.NumCaptured_RBV._data["value"] = self._frames_captured

                if num_capture > 0 and self._frames_captured >= num_capture:
                    self._stop_capture()
            except Exception as exc:
                logger.error("HDF5 write error: %s", exc)

    def _start_capture(self):
        try:
            import h5py
        except ImportError:
            logger.error("h5py is required for HDF5 writing. pip install h5py")
            return

        with self._lock:
            file_path = str(self.FilePath.value).rstrip("/")
            file_name = str(self.FileName.value)
            file_num = int(self.FileNumber.value)
            template = str(self.FileTemplate.value)

            try:
                full_name = template % (file_path + "/", file_name, file_num)
            except Exception:
                full_name = f"{file_path}/{file_name}_{file_num:04d}.h5"

            try:
                Path(file_path).mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.error("Cannot create dir %s: %s", file_path, exc)
                return

            num_capture = max(1, int(self.NumCapture.value))
            try:
                self._h5_file = h5py.File(full_name, "w")
                # Create dataset with first frame's expected shape
                # We don't know the shape yet — use sensor dims if available
                cam = self.parent.cam1 if hasattr(self.parent, 'cam1') else None
                if cam:
                    h = int(cam.SizeY_RBV.value)
                    w = int(cam.SizeX_RBV.value)
                else:
                    h, w = 2368, 4432
                self._h5_dataset = self._h5_file.create_dataset(
                    "/exchange/data",
                    shape=(num_capture, h, w),
                    maxshape=(None, h, w),
                    dtype=np.uint16,
                    chunks=(1, h, w),
                )
                self._frames_captured = 0
                self._capturing = True
                self.Capture_RBV._data["value"] = 1
                self.WriteStatus._data["value"] = "Capturing"
                self.FullFileName_RBV._data["value"] = full_name
                self.NumCaptured_RBV._data["value"] = 0
                logger.info("HDF5: capturing to %s", full_name)
            except Exception as exc:
                logger.error("Failed to open HDF5 file: %s", exc)
                self._h5_file = None
                self._h5_dataset = None

    def _stop_capture(self):
        self._capturing = False
        self.Capture_RBV._data["value"] = 0
        self.WriteStatus._data["value"] = "Idle"
        with self._lock:
            if self._h5_file is not None:
                try:
                    # Trim to actual frames written
                    if self._h5_dataset is not None and \
                            self._frames_captured < self._h5_dataset.shape[0]:
                        self._h5_dataset.resize(
                            (self._frames_captured,) + self._h5_dataset.shape[1:])
                    self._h5_file.close()
                    logger.info("HDF5: closed (%d frames)", self._frames_captured)
                except Exception as exc:
                    logger.error("HDF5 close error: %s", exc)
                finally:
                    self._h5_file = None
                    self._h5_dataset = None

        if str(self.AutoIncrement.value) == "Yes":
            self.FileNumber._data["value"] = int(self.FileNumber.value) + 1
