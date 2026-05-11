"""
HDF5 file writer plugin (HDF1:) — areaDetector-compatible.

Writes acquired frames to HDF5 files at /exchange/data dataset.
"""

import logging
import os
import queue
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from caproto import ChannelType


def _free_ram_bytes() -> int:
    """Return free system memory in bytes (Linux). Returns 0 if unknown."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


def _compute_queue_size(frame_bytes: int, budget_frac: float = 0.25) -> int:
    """How many frames fit in budget_frac * available RAM."""
    free = _free_ram_bytes()
    if free <= 0 or frame_bytes <= 0:
        return 100  # safe default
    return max(8, int(free * budget_frac / frame_bytes))


def _to_str(val) -> str:
    """Convert a caproto string PV value to a Python str.

    `pvproperty(dtype=str, max_length=N)` may return a numpy array of bytes
    (CHAR array) instead of a Python string. This handles all forms.
    """
    if isinstance(val, str):
        return val
    if isinstance(val, bytes):
        return val.decode("ascii", errors="replace").rstrip("\x00")
    try:
        return bytes(val).decode("ascii", errors="replace").rstrip("\x00")
    except Exception:
        return str(val)

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
    Capture = pvproperty(value="Done", dtype=ChannelType.ENUM,
                          enum_strings=["Done", "Capture"])
    Capture_RBV = pvproperty(value="Done", dtype=ChannelType.ENUM,
                              enum_strings=["Done", "Capture"], read_only=True)
    WriteStatus = pvproperty(value="Idle", dtype=str, max_length=40, read_only=True)
    XMLFileName = pvproperty(value="", dtype=str, max_length=256)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Reentrant lock: on_frame may call _stop_capture while holding it
        self._lock = threading.RLock()
        self._capturing = False
        self._h5_file = None
        self._h5_dataset = None
        self._frames_captured = 0
        # Flush every N frames (set to ~1s worth at expected fps)
        self._flush_every = 100
        # Background HDF5 writer: queue + thread keep acquisition non-blocking
        self._write_queue: Optional[queue.Queue] = None
        self._write_thread: Optional[threading.Thread] = None
        # Queue size is computed from available RAM at capture-start time.
        # Use up to RAM_BUDGET_FRAC of free system memory.
        self._ram_budget_frac = 0.25
        self._dropped_frames = 0

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
        """Frame callback — non-blocking enqueue to background writer."""
        if not self._capturing:
            return

        # Size the queue based on available RAM and the actual frame bytes.
        # We can only do this once we know the frame size, so deferred to
        # the first frame.
        if self._write_queue is None:
            frame_bytes = int(frame.nbytes)
            qsize = _compute_queue_size(frame_bytes, self._ram_budget_frac)
            self._write_queue = queue.Queue(maxsize=qsize)
            free_mb = _free_ram_bytes() / 1e6
            logger.info(
                "HDF5 queue: %d frames (%.0f MB / frame, %.0f MB free RAM)",
                qsize, frame_bytes / 1e6, free_mb)
            # Start writer thread now (couldn't earlier — queue didn't exist)
            self._write_thread = threading.Thread(
                target=self._writer_loop, daemon=True, name="hdf5-writer")
            self._write_thread.start()

        # Make a contiguous copy now: the underlying buffer may be reused
        # by the camera before the writer thread gets to it.
        try:
            self._write_queue.put_nowait(np.ascontiguousarray(frame))
        except queue.Full:
            # Writer can't keep up — drop this frame
            self._dropped_frames += 1
            if self._dropped_frames % 50 == 1:
                logger.warning("HDF5 writer behind, dropped %d frames so far",
                               self._dropped_frames)

    def _writer_loop(self):
        """Background thread: drain queue and write to HDF5."""
        while True:
            try:
                item = self._write_queue.get(timeout=0.5)
            except queue.Empty:
                if not self._capturing:
                    return
                continue
            if item is None:
                # Sentinel — flush and exit
                return

            with self._lock:
                if self._h5_file is None:
                    continue
                try:
                    num_capture = int(self.NumCapture.value)
                    frame = item

                    # Lazily create dataset from first frame's actual shape
                    if self._h5_dataset is None:
                        h, w = frame.shape[0], frame.shape[1]
                        self._h5_dataset = self._h5_file.create_dataset(
                            "/exchange/data",
                            shape=(max(num_capture, 1), h, w),
                            maxshape=(None, h, w),
                            dtype=frame.dtype,
                            chunks=(1, h, w),
                        )

                    idx = self._frames_captured

                    if idx >= self._h5_dataset.shape[0]:
                        new_size = max(idx + 1, num_capture)
                        self._h5_dataset.resize((new_size,) +
                                                self._h5_dataset.shape[1:])

                    if frame.shape != self._h5_dataset.shape[1:]:
                        logger.warning(
                            "HDF5: frame shape %s != dataset %s, skipping",
                            frame.shape, self._h5_dataset.shape[1:])
                        continue

                    self._h5_dataset[idx] = frame
                    self._frames_captured += 1
                    if self._frames_captured % self._flush_every == 0:
                        self._h5_file.flush()
                    self.NumCaptured_RBV._data["value"] = self._frames_captured

                    if num_capture > 0 and self._frames_captured >= num_capture:
                        # Don't recurse via _stop_capture (it joins this thread).
                        # Just signal the cam_plugin via end_callback path.
                        self._capturing = False
                        return
                except Exception as exc:
                    logger.error("HDF5 write error: %s", exc)

    def _start_capture(self):
        try:
            import h5py
        except ImportError:
            logger.error("h5py is required for HDF5 writing. pip install h5py")
            return

        with self._lock:
            file_path = _to_str(self.FilePath.value).rstrip("/")
            file_name = _to_str(self.FileName.value)
            file_num = int(self.FileNumber.value)
            template = _to_str(self.FileTemplate.value)
            logger.info("HDF5 start: path='%s' name='%s' num=%d",
                         file_path, file_name, file_num)

            try:
                full_name = template % (file_path + "/", file_name, file_num)
            except Exception:
                full_name = f"{file_path}/{file_name}_{file_num:04d}.h5"

            try:
                Path(file_path).mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.error("Cannot create dir %s: %s", file_path, exc)
                return

            try:
                self._h5_file = h5py.File(full_name, "w", libver="latest")
                # Dataset, queue, and writer thread are created lazily on
                # the first frame (we need the frame size to budget RAM).
                self._h5_dataset = None
                self._write_queue = None
                self._write_thread = None
                self._frames_captured = 0
                self._dropped_frames = 0
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
                self._write_queue = None

    def _stop_capture(self):
        self._capturing = False
        self.Capture_RBV._data["value"] = 0
        self.WriteStatus._data["value"] = "Idle"

        # Drain queue and stop writer thread first (outside the lock,
        # since writer needs the lock to drain remaining items)
        if self._write_queue is not None:
            try:
                self._write_queue.put_nowait(None)  # sentinel
            except queue.Full:
                pass
        if self._write_thread is not None:
            current = threading.current_thread()
            if self._write_thread is not current:
                self._write_thread.join(timeout=10.0)
            self._write_thread = None
        self._write_queue = None

        with self._lock:
            if self._h5_file is not None:
                try:
                    # Trim to actual frames written
                    if self._h5_dataset is not None and \
                            self._frames_captured < self._h5_dataset.shape[0]:
                        self._h5_dataset.resize(
                            (self._frames_captured,) + self._h5_dataset.shape[1:])
                    self._h5_file.close()
                    msg = f"HDF5: closed ({self._frames_captured} frames)"
                    if self._dropped_frames:
                        msg += f", {self._dropped_frames} dropped"
                    logger.info(msg)
                except Exception as exc:
                    logger.error("HDF5 close error: %s", exc)
                finally:
                    self._h5_file = None
                    self._h5_dataset = None

        if str(self.AutoIncrement.value) == "Yes":
            self.FileNumber._data["value"] = int(self.FileNumber.value) + 1
