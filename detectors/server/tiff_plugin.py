"""
TIFF series writer plugin (TIF1:) — areaDetector-compatible.

Writes one TIFF file per acquired frame: ``<name>_NNNNNN.tif``. Each file
is self-contained, so external viewers can open completed files while a
capture is still running (unlike HDF5, no central index needs flushing).

Tomoscan-side consumers expect HDF5 (``/exchange/data*``), so this writer
is intended for the GUI-driven "save raw frames" path, not for tomoscan.
The plugin exposes the same PV surface as :class:`HDF5Plugin` so the GUI
can switch between formats without per-plugin branching.
"""

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from caproto import ChannelType
from caproto.server import PVGroup, pvproperty

# Reuse the RAM-budget queue sizing from the HDF5 plugin — same constraint
# (size the burst buffer to a fraction of free RAM) applies to TIFF.
from detectors.server.hdf5_plugin import (
    _compute_queue_size,
    _free_ram_bytes,
    _to_str,
)

logger = logging.getLogger(__name__)


class TIFFPlugin(PVGroup):
    """areaDetector TIF1: PVs for TIFF-series file writing."""

    NDArrayPort = pvproperty(value="CAM1", dtype=str, max_length=40)
    EnableCallbacks = pvproperty(value="Enable", dtype=str, max_length=40)

    FilePath = pvproperty(value="", dtype=str, max_length=256)
    FilePath_RBV = pvproperty(value="", dtype=str, max_length=256, read_only=True)
    FilePathExists_RBV = pvproperty(value=0, dtype=int, read_only=True)
    FileName = pvproperty(value="image", dtype=str, max_length=256)
    FileName_RBV = pvproperty(value="image", dtype=str, max_length=256, read_only=True)

    FileNumber = pvproperty(value=1, dtype=int)
    # 6-digit padding handles up to a million frames per session.
    # HDF5's %4.4d would overflow at 10k; tomoscan-style bursts are routinely
    # 10k–100k frames.
    FileTemplate = pvproperty(value="%s%s_%6.6d.tif", dtype=str, max_length=256)
    FullFileName_RBV = pvproperty(value="", dtype=str, max_length=512, read_only=True)
    AutoIncrement = pvproperty(value="Yes", dtype=str, max_length=40)
    AutoSave = pvproperty(value="Yes", dtype=str, max_length=40)

    FileWriteMode = pvproperty(value="Stream", dtype=str, max_length=40)
    # Match the HDF5 default (1000); a literal 1 makes "Start Capture without
    # touching the count" silently stop after a single frame.
    NumCapture = pvproperty(value=1000, dtype=int)
    NumCaptured_RBV = pvproperty(value=0, dtype=int, read_only=True)
    Capture = pvproperty(value="Done", dtype=ChannelType.ENUM,
                          enum_strings=["Done", "Capture"])
    Capture_RBV = pvproperty(value="Done", dtype=ChannelType.ENUM,
                              enum_strings=["Done", "Capture"], read_only=True)
    WriteStatus = pvproperty(value="Success", dtype=ChannelType.ENUM,
                              enum_strings=["Success", "Failure"],
                              read_only=True)
    WriteMessage = pvproperty(value="Idle", dtype=str, max_length=80,
                               read_only=True)
    XMLFileName = pvproperty(value="", dtype=str, max_length=256)

    # Mirror the HDF5 plugin telemetry so the GUI can read the same names.
    NumReceived_RBV = pvproperty(value=0, dtype=int, read_only=True)
    QueueDepth_RBV = pvproperty(value=0, dtype=int, read_only=True)
    QueueMax_RBV = pvproperty(value=0, dtype=int, read_only=True)
    WriterState_RBV = pvproperty(value="Idle", dtype=ChannelType.ENUM,
                                  enum_strings=["Idle", "Writing",
                                                "Closing", "Error"],
                                  read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()
        self._capturing = False
        # Per-session starting file number (the index used in the filename
        # for the first frame). Subsequent frames use start + frame_index.
        self._file_number_start = 1
        self._dir = ""
        self._base_name = "image"
        self._template = "%s%s_%6.6d.tif"
        self._frames_captured = 0
        self._frames_received = 0
        self._dropped_frames = 0
        self._not_capturing_drops = 0
        # Gates the "frame arrived while not capturing" warning so an
        # idle plugin (other format is selected) doesn't spam the log.
        self._ever_started = False
        # Background writer: queue + thread, identical pattern to HDF5 plugin.
        self._write_queue: Optional[queue.Queue] = None
        self._write_thread: Optional[threading.Thread] = None
        self._frame_bytes = 0
        # Match HDF5: 75% of free RAM is the burst-buffer ceiling.
        self._ram_budget_frac = 0.75
        self._async_loop = None

    @NumCaptured_RBV.startup
    async def NumCaptured_RBV(self, instance, async_lib):
        import asyncio
        self._async_loop = asyncio.get_running_loop()

    def _publish(self, prop, value):
        """Thread-safe PV update with monitor notification."""
        import asyncio
        if self._async_loop is not None:
            asyncio.run_coroutine_threadsafe(prop.write(value),
                                              self._async_loop)

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

    def _format_path(self, index: int) -> str:
        try:
            return self._template % (self._dir + "/", self._base_name, index)
        except Exception:
            return f"{self._dir}/{self._base_name}_{index:06d}.tif"

    def on_frame(self, frame: np.ndarray, metadata: dict) -> None:
        """Frame callback — non-blocking enqueue to background TIFF writer."""
        if not self._capturing:
            # Only warn if this plugin has actually been used at least once
            # in this session. A fresh IOC + camera-only streaming (user
            # selected the *other* format) is not a bug, just an idle
            # plugin getting frames it doesn't want.
            if self._ever_started:
                self._not_capturing_drops += 1
                if self._not_capturing_drops % 25 == 1:
                    logger.warning(
                        "TIFF: frame arrived while not capturing (count=%d)",
                        self._not_capturing_drops)
            return

        # Lazy: queue sizing needs to know the actual frame footprint.
        if self._write_queue is None:
            frame_bytes = int(frame.nbytes)
            qsize = _compute_queue_size(frame_bytes, self._ram_budget_frac)
            self._write_queue = queue.Queue(maxsize=qsize)
            self._frame_bytes = frame_bytes
            free_mb = _free_ram_bytes() / 1e6
            logger.info(
                "TIFF queue: %d frames (%.1f MB / frame, %.0f MB free RAM)",
                qsize, frame_bytes / 1e6, free_mb)
            self._publish(self.QueueMax_RBV, qsize)

        if (self._write_thread is None
                or not self._write_thread.is_alive()):
            if self._write_thread is not None:
                logger.warning(
                    "TIFF writer thread died, restarting (captured %d so far)",
                    self._frames_captured)
            self._write_thread = threading.Thread(
                target=self._writer_loop, daemon=True, name="tiff-writer")
            self._write_thread.start()

        # Snapshot the frame — camera buffers may be recycled before the
        # writer reaches it.
        self._frames_received += 1
        self._publish(self.NumReceived_RBV, self._frames_received)
        try:
            self._write_queue.put_nowait(np.ascontiguousarray(frame))
            self._publish(self.QueueDepth_RBV, self._write_queue.qsize())
        except queue.Full:
            self._dropped_frames += 1
            if self._dropped_frames % 50 == 1:
                logger.warning("TIFF writer behind, dropped %d frames so far",
                                self._dropped_frames)

    def _writer_loop(self):
        """Background thread: drain queue, write one TIFF per frame.

        Closes the session (calls _stop_capture) when the NumCapture target
        is reached. Doesn't exit just because `_capturing` flips false: like
        the HDF5 plugin, tomoscan-style multi-phase captures stop/start
        acquisition while the writer session continues.
        """
        try:
            import tifffile
        except ImportError:
            logger.error("tifffile is required for TIFF writing. "
                          "pip install tifffile")
            self._publish(self.WriteStatus, 1)
            self._publish(self.WriteMessage, "tifffile not installed")
            self._publish(self.WriterState_RBV, 3)  # Error
            self._capturing = False
            return

        while True:
            try:
                frame = self._write_queue.get(timeout=0.5)
            except queue.Empty:
                # Queue drained — either we're idle between phases (keep
                # waiting) or _stop_capture wiped the queue (exit).
                self._publish(self.WriterState_RBV, 0)  # Idle
                self._publish(self.QueueDepth_RBV, 0)
                if self._write_queue is None:
                    return
                continue
            if frame is None:
                # Explicit sentinel from _stop_capture.
                self._publish(self.WriterState_RBV, 2)  # Closing
                return
            self._publish(self.WriterState_RBV, 1)  # Writing
            self._publish(self.QueueDepth_RBV, self._write_queue.qsize())

            with self._lock:
                try:
                    num_capture = int(self.NumCapture.value)
                    index = self._file_number_start + self._frames_captured
                    out_path = self._format_path(index)
                    # compression=None: raw uint16 is fastest. /local can
                    # easily sustain it; a compression knob can be added
                    # later if a slow destination needs it.
                    tifffile.imwrite(out_path, frame, compression=None)
                    self._frames_captured += 1
                    self._publish(self.NumCaptured_RBV,
                                   self._frames_captured)

                    if num_capture > 0 and self._frames_captured >= num_capture:
                        logger.info(
                            "writer_loop: target reached (%d >= %d), closing session",
                            self._frames_captured, num_capture)
                        self._capturing = False
                        # Safe to call _stop_capture from this thread:
                        # RLock allows re-entry and _stop_capture skips
                        # joining the writer when called from it.
                        self._stop_capture()
                        return
                except Exception as exc:
                    logger.error("TIFF write error: %s", exc)
                    self._publish(self.WriteStatus, 1)
                    self._publish(self.WriteMessage, f"Error: {exc}"[:80])
                    self._publish(self.WriterState_RBV, 3)  # Error
                    continue

    def _start_capture(self):
        # Lazy import: if tifffile is missing on a system that only uses
        # HDF5, importing this module / loading the IOC still succeeds; the
        # error only surfaces when the user actually clicks Start Capture
        # in TIFF mode.
        try:
            import tifffile  # noqa: F401
        except ImportError:
            logger.error("tifffile is required for TIFF series writing. "
                          "pip install tifffile")
            self._publish(self.WriteStatus, 1)
            self._publish(self.WriteMessage, "tifffile not installed")
            return

        with self._lock:
            self._dir = _to_str(self.FilePath.value).rstrip("/")
            self._base_name = _to_str(self.FileName.value)
            self._file_number_start = int(self.FileNumber.value)
            self._template = _to_str(self.FileTemplate.value)
            auto_inc = _to_str(self.AutoIncrement.value) == "Yes"
            logger.info(
                "TIFF start: path='%s' name='%s' start_num=%d auto_inc=%s",
                self._dir, self._base_name, self._file_number_start, auto_inc)

            if not self._dir:
                logger.error("TIFF start: FilePath is empty — refusing to write")
                self._publish(self.WriteStatus, 1)
                self._publish(self.WriteMessage, "FilePath empty")
                return
            try:
                Path(self._dir).mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.error("Cannot create dir %s: %s", self._dir, exc)
                self._publish(self.WriteStatus, 1)
                self._publish(self.WriteMessage,
                               f"mkdir failed: {exc}"[:80])
                return

            # Avoid clobbering an existing series: if AutoIncrement is on
            # and the first target file already exists, bump the start
            # index until we find a free slot.
            if auto_inc:
                while os.path.exists(self._format_path(self._file_number_start)):
                    self._file_number_start += 1
                if self._file_number_start != int(self.FileNumber.value):
                    self._publish(self.FileNumber, self._file_number_start)
                    self.FileNumber._data["value"] = self._file_number_start
                    logger.info(
                        "TIFF: skipped existing files, starting at num=%d",
                        self._file_number_start)

            # Reset state for the new session.
            self._frames_captured = 0
            self._frames_received = 0
            self._dropped_frames = 0
            self._not_capturing_drops = 0
            self._write_queue = None
            self._write_thread = None
            self._capturing = True
            self._ever_started = True

            first_path = self._format_path(self._file_number_start)
            self._publish(self.FullFileName_RBV, first_path)
            self.FullFileName_RBV._data["value"] = first_path
            self._publish(self.Capture_RBV, 1)
            self._publish(self.WriteStatus, 0)
            self._publish(self.WriteMessage, "Capturing")
            self._publish(self.NumCaptured_RBV, 0)
            self._publish(self.NumReceived_RBV, 0)
            self._publish(self.QueueDepth_RBV, 0)
            self._publish(self.WriterState_RBV, 0)
            logger.info("TIFF: capturing to %s (start)", first_path)

    def _maybe_stop_capture(self):
        """Called from the camera's end_callback when acquisition stops.

        Mirrors HDF5 semantics: only finalize the session if the NumCapture
        target was actually reached; otherwise leave the writer running for
        the next acquisition phase (tomoscan flats → projections → darks).
        Guard on `_capturing` since TIFF has no file handle to track —
        each frame is its own self-contained file.
        """
        try:
            num_capture = int(self.NumCapture.value)
        except Exception:
            num_capture = 0
        if not self._capturing:
            return
        if num_capture > 0 and self._frames_captured >= num_capture:
            logger.info(
                "TIFF: capture target reached (%d/%d), closing session",
                self._frames_captured, num_capture)
            self._stop_capture()
        else:
            logger.debug(
                "TIFF: phase ended (%d/%d frames). Keeping session open.",
                self._frames_captured, num_capture)

    def _stop_capture(self):
        self._capturing = False
        # Mirror HDF5: clear _ever_started so we don't spam warnings
        # for frames that keep arriving after a clean stop (e.g. the
        # user is now capturing in the *other* format and this plugin
        # is idle but still wired to the camera).
        self._ever_started = False
        self._publish(self.Capture_RBV, 0)
        self.Capture_RBV._data["value"] = 0
        self._publish(self.WriteMessage, "Idle")

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

        msg = (f"TIFF: closed ({self._frames_captured} frames written"
                f" starting at #{self._file_number_start})")
        if self._dropped_frames:
            msg += f", {self._dropped_frames} dropped"
        logger.info(msg)
        self._publish(self.WriterState_RBV, 0)  # Idle
        summary = (f"Saved {self._frames_captured} TIFF"
                    + ("s" if self._frames_captured != 1 else ""))
        self._publish(self.WriteMessage, summary[:80])

        # Advance FileNumber to the next unused index so a subsequent
        # session doesn't reuse filenames (matches HDF5 plugin behavior).
        if _to_str(self.AutoIncrement.value) == "Yes":
            next_num = self._file_number_start + self._frames_captured
            self._publish(self.FileNumber, next_num)
            self.FileNumber._data["value"] = next_num
