"""
HDF5 file writer plugin (HDF1:) — areaDetector-compatible.

Writes acquired frames to HDF5 files at /exchange/data dataset.
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


# FrameType -> HDF5 dataset path. Tomoscan's add_theta() reads
# /defaults/HDF5FrameLocation and matches these exact strings.
_FRAME_TYPE_TO_DATASET = {
    "Projection": "/exchange/data",
    "FlatField":  "/exchange/data_white",
    "DarkField":  "/exchange/data_dark",
}


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
    # Standard areaDetector convention: WriteStatus is an enum (0=Success,
    # 1=Failure). Tomoscan watches this for write errors with `value == 1`.
    # Human-readable status goes in WriteMessage.
    WriteStatus = pvproperty(value="Success", dtype=ChannelType.ENUM,
                              enum_strings=["Success", "Failure"],
                              read_only=True)
    WriteMessage = pvproperty(value="Idle", dtype=str, max_length=80,
                               read_only=True)
    XMLFileName = pvproperty(value="", dtype=str, max_length=256)
    # Live buffer / writer telemetry. The writer thread runs behind a
    # bounded queue; if the disk can't keep up these reveal the backlog
    # before frames start dropping.
    #   NumReceived_RBV    — frames handed to on_frame (camera-side count)
    #   QueueDepth_RBV     — frames currently waiting to be written
    #   QueueMax_RBV       — queue capacity (sized from RAM at capture start)
    #   WriterState_RBV    — Idle / Writing / Closing / Error
    NumReceived_RBV = pvproperty(value=0, dtype=int, read_only=True)
    QueueDepth_RBV = pvproperty(value=0, dtype=int, read_only=True)
    QueueMax_RBV = pvproperty(value=0, dtype=int, read_only=True)
    WriterState_RBV = pvproperty(value="Idle", dtype=ChannelType.ENUM,
                                  enum_strings=["Idle", "Writing",
                                                "Closing", "Error"],
                                  read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Reentrant lock: on_frame may call _stop_capture while holding it
        self._lock = threading.RLock()
        self._capturing = False
        self._h5_file = None
        # One HDF5 dataset per FrameType. Created lazily on the first frame
        # of that type. Tomoscan's add_theta reads all three.
        self._h5_datasets: dict = {}  # frame_type -> dataset
        self._dataset_counts: dict = {}  # frame_type -> frames written
        self._frames_captured = 0
        # Per-frame index arrays written to /defaults at close. Tomoscan's
        # add_theta reads these to identify which frames are projections /
        # flats / darks (by HDF5FrameLocation).
        self._unique_ids: list = []
        self._frame_locations: list = []
        # Flush every N frames (set to ~1s worth at expected fps)
        self._flush_every = 100
        # Background HDF5 writer: queue + thread keep acquisition non-blocking
        self._write_queue: Optional[queue.Queue] = None
        self._write_thread: Optional[threading.Thread] = None
        # Queue size is computed from available RAM at capture-start time.
        # Use up to RAM_BUDGET_FRAC of free system memory. 0.75 leaves
        # 25% headroom for the OS page cache (which speeds NFS writes),
        # h5py's internal buffers, and a margin against OOM if another
        # process spikes — but lets a dedicated-capture box buffer most
        # of a long burst in RAM before NFS becomes the bottleneck.
        self._ram_budget_frac = 0.75
        self._dropped_frames = 0
        # Async loop reference for thread-safe PV publishes (set in startup).
        self._async_loop = None
        # Diagnostic: count callback hits that were silently dropped because
        # capture wasn't active. Helps catch logic bugs that lose frames.
        self._not_capturing_drops = 0

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

    def _get_or_create_dataset(self, frame_type: str, frame: np.ndarray):
        """Lazily create a per-frame-type dataset on first frame.

        Returns the dataset, or None if the frame_type is unrecognized.
        """
        path = _FRAME_TYPE_TO_DATASET.get(frame_type)
        if path is None:
            logger.warning("Unknown FrameType=%r; routing to /exchange/data",
                            frame_type)
            path = "/exchange/data"
            frame_type = "Projection"

        ds = self._h5_datasets.get(frame_type)
        if ds is None:
            try:
                num_capture = int(self.NumCapture.value)
            except Exception:
                num_capture = 0
            h, w = frame.shape[0], frame.shape[1]
            # Initial size: 1 frame, grows on resize. Chunked along frame axis.
            ds = self._h5_file.create_dataset(
                path,
                shape=(1, h, w),
                maxshape=(None, h, w),
                dtype=frame.dtype,
                chunks=(1, h, w),
            )
            self._h5_datasets[frame_type] = ds
            self._dataset_counts[frame_type] = 0
            logger.info("HDF5: created dataset %s", path)
        return ds, frame_type, path

    def on_frame(self, frame: np.ndarray, metadata: dict) -> None:
        """Frame callback — non-blocking enqueue to background writer."""
        if not self._capturing:
            self._not_capturing_drops += 1
            if self._not_capturing_drops % 25 == 1:
                logger.warning(
                    "HDF5: frame arrived while not capturing (count=%d, h5_open=%s)",
                    self._not_capturing_drops, self._h5_file is not None)
            return

        # Size the queue based on available RAM and the actual frame bytes.
        # We can only do this once we know the frame size, so deferred to
        # the first frame.
        if self._write_queue is None:
            frame_bytes = int(frame.nbytes)
            qsize = _compute_queue_size(frame_bytes, self._ram_budget_frac)
            self._write_queue = queue.Queue(maxsize=qsize)
            self._frame_bytes = frame_bytes  # stored so GUI can show MB, not slots
            free_mb = _free_ram_bytes() / 1e6
            logger.info(
                "HDF5 queue: %d frames (%.0f MB / frame, %.0f MB free RAM)",
                qsize, frame_bytes / 1e6, free_mb)
            # Publish queue capacity so GUI can show "buffer fill" as a fraction.
            self._publish(self.QueueMax_RBV, qsize)

        # Writer thread watchdog: if missing or died, (re)start it.
        # Tomoscan keeps one capture session across flats / projections /
        # darks — the writer thread must outlive all phases.
        if (self._write_thread is None
                or not self._write_thread.is_alive()):
            if self._write_thread is not None:
                logger.warning(
                    "HDF5 writer thread died, restarting (captured %d so far)",
                    self._frames_captured)
            self._write_thread = threading.Thread(
                target=self._writer_loop, daemon=True, name="hdf5-writer")
            self._write_thread.start()

        # Make a contiguous copy now: the underlying buffer may be reused
        # by the camera before the writer thread gets to it. Bundle the
        # FrameType from metadata so the writer can route to the right
        # /exchange dataset.
        frame_type = metadata.get("frame_type", "Projection") if metadata else "Projection"
        item = (frame_type, np.ascontiguousarray(frame))
        # Camera-side counter: increments on every received frame, regardless
        # of whether the writer ultimately accepts or drops it. Together with
        # NumCaptured_RBV this exposes the receive-vs-write gap.
        self._frames_received = getattr(self, "_frames_received", 0) + 1
        self._publish(self.NumReceived_RBV, self._frames_received)
        try:
            self._write_queue.put_nowait(item)
            self._publish(self.QueueDepth_RBV, self._write_queue.qsize())
        except queue.Full:
            # Writer can't keep up — drop this frame
            self._dropped_frames += 1
            if self._dropped_frames % 50 == 1:
                logger.warning("HDF5 writer behind, dropped %d frames so far",
                               self._dropped_frames)

    def _writer_loop(self):
        """Background thread: drain queue and write to HDF5.

        Only exits on the sentinel (None) placed by _stop_capture, OR when
        the HDF5 file has been closed (defensive). Does NOT exit just because
        `_capturing` flips False — tomoscan stops/starts acquisition between
        flat/projection/dark phases while keeping one capture session open,
        so the writer must survive those gaps.
        """
        while True:
            try:
                item = self._write_queue.get(timeout=0.5)
            except queue.Empty:
                # Idle: nothing in the queue right now.
                self._publish(self.WriterState_RBV, 0)  # Idle
                self._publish(self.QueueDepth_RBV, 0)
                # File closed out from under us → no more work to do.
                if self._h5_file is None:
                    return
                continue
            if item is None:
                # Sentinel from _stop_capture — flush and exit.
                self._publish(self.WriterState_RBV, 2)  # Closing
                return
            # Got an item → actively writing.
            self._publish(self.WriterState_RBV, 1)  # Writing
            self._publish(self.QueueDepth_RBV, self._write_queue.qsize())

            with self._lock:
                if self._h5_file is None:
                    continue
                try:
                    num_capture = int(self.NumCapture.value)
                    frame_type, frame = item

                    ds, frame_type, path = self._get_or_create_dataset(
                        frame_type, frame)

                    # Grow the per-type dataset by 1 if needed.
                    type_idx = self._dataset_counts[frame_type]
                    if type_idx >= ds.shape[0]:
                        ds.resize((type_idx + 1,) + ds.shape[1:])

                    if frame.shape != ds.shape[1:]:
                        logger.warning(
                            "HDF5: frame shape %s != dataset %s, skipping",
                            frame.shape, ds.shape[1:])
                        continue

                    ds[type_idx] = frame
                    self._dataset_counts[frame_type] = type_idx + 1

                    # Tomoscan's add_theta needs one entry per frame in
                    # the order frames were captured.
                    self._frames_captured += 1
                    self._unique_ids.append(self._frames_captured)
                    self._frame_locations.append(path.encode("ascii"))

                    if self._frames_captured % self._flush_every == 0:
                        self._h5_file.flush()
                    # Update PV and notify monitors so clients (tomoscan)
                    # see the counter advance.
                    self._publish(self.NumCaptured_RBV,
                                   self._frames_captured)

                    if num_capture > 0 and self._frames_captured >= num_capture:
                        # Target reached. Mark not-capturing; the cam_plugin
                        # end_callback path (_maybe_stop_capture) will close
                        # the file. We can exit this thread cleanly.
                        logger.info(
                            "writer_loop: target reached (%d >= %d), exiting",
                            self._frames_captured, num_capture)
                        self._capturing = False
                        return
                except Exception as exc:
                    logger.error("HDF5 write error: %s", exc)
                    self._publish(self.WriteStatus, 1)  # Failure
                    self._publish(self.WriteMessage, f"Error: {exc}"[:80])
                    self._publish(self.WriterState_RBV, 3)  # Error

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
            auto_inc = _to_str(self.AutoIncrement.value) == "Yes"
            logger.info("HDF5 start: path='%s' name='%s' num=%d auto_inc=%s",
                         file_path, file_name, file_num, auto_inc)

            try:
                Path(file_path).mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.error("Cannot create dir %s: %s", file_path, exc)
                return

            def _format_name(n: int) -> str:
                try:
                    return template % (file_path + "/", file_name, n)
                except Exception:
                    return f"{file_path}/{file_name}_{n:04d}.h5"

            full_name = _format_name(file_num)
            # areaDetector behavior: if AutoIncrement is on and the target
            # file already exists, bump FileNumber until we find a free name.
            # Prevents overwrites across IOC restarts (FileNumber resets to 1).
            if auto_inc:
                while os.path.exists(full_name):
                    file_num += 1
                    full_name = _format_name(file_num)
                if file_num != int(self.FileNumber.value):
                    self._publish(self.FileNumber, file_num)
                    self.FileNumber._data["value"] = file_num
                    logger.info("HDF5: skipped existing files, using num=%d",
                                file_num)

            try:
                self._h5_file = h5py.File(full_name, "w", libver="latest")
                # Datasets, queue, and writer thread are created lazily on
                # the first frame of each FrameType.
                self._h5_datasets = {}
                self._dataset_counts = {}
                self._unique_ids = []
                self._frame_locations = []
                self._write_queue = None
                self._write_thread = None
                self._frames_captured = 0
                self._frames_received = 0
                self._dropped_frames = 0
                self._not_capturing_drops = 0
                self._capturing = True
                self._publish(self.Capture_RBV, 1)
                self._publish(self.WriteStatus, 0)  # Success
                self._publish(self.WriteMessage, "Capturing")
                self._publish(self.FullFileName_RBV, full_name)
                self._publish(self.NumCaptured_RBV, 0)
                self._publish(self.NumReceived_RBV, 0)
                self._publish(self.QueueDepth_RBV, 0)
                self._publish(self.WriterState_RBV, 0)  # Idle
                # Also poke the underlying values so synchronous reads work
                self.FullFileName_RBV._data["value"] = full_name
                logger.info("HDF5: capturing to %s", full_name)
            except Exception as exc:
                logger.error("Failed to open HDF5 file: %s", exc)
                self._h5_file = None
                self._h5_datasets = {}
                self._write_queue = None

    def _maybe_stop_capture(self):
        """Called when acquisition ends. Only close HDF5 if the capture
        target was reached. Otherwise keep file open — there are more
        frames coming (e.g. tomoscan flats → projections → darks)."""
        try:
            num_capture = int(self.NumCapture.value)
        except Exception:
            num_capture = 0
        if not self._capturing:
            return
        if num_capture > 0 and self._frames_captured >= num_capture:
            logger.info("HDF5: capture target reached (%d/%d), closing file",
                         self._frames_captured, num_capture)
            self._stop_capture()
        else:
            logger.debug("HDF5: phase ended (%d/%d frames). Keeping file open.",
                          self._frames_captured, num_capture)

    def _background_fsync(self, path):
        """Drain NFS write-behind for `path` off the calling thread.

        Run in a daemon thread so a slow flush never freezes the GUI or
        blocks the caproto putter. Flips WriterState_RBV from Closing →
        Idle and WriteMessage to a final summary when the bytes are
        durable on the NFS server.
        """
        start = time.time()
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            elapsed = time.time() - start
            if elapsed > 0.5:
                logger.info(
                    "HDF5: fsync took %.1f s (NFS write-behind drain)",
                    elapsed)
            self._publish(self.WriteMessage,
                           f"Saved (flushed in {elapsed:.1f}s)")
        except Exception as exc:
            logger.warning("HDF5: fsync failed: %s", exc)
            self._publish(self.WriteMessage, f"Saved (fsync failed: {exc})"[:80])
        finally:
            self._publish(self.WriterState_RBV, 0)  # Idle

    def _stop_capture(self):
        self._capturing = False
        # Fire monitor on Capture_RBV — tomoscan's end_scan does
        # wait_pv(FPCaptureRBV, 0) right after putting Capture=Done.
        # Writing _data["value"] directly does NOT notify subscribers,
        # so we publish via the async loop.
        self._publish(self.Capture_RBV, 0)
        self.Capture_RBV._data["value"] = 0
        self._publish(self.WriteMessage, "Idle")

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
                    # Trim each per-type dataset to actual frames written.
                    for ftype, ds in self._h5_datasets.items():
                        count = self._dataset_counts.get(ftype, 0)
                        if count < ds.shape[0]:
                            ds.resize((count,) + ds.shape[1:])

                    # Write per-frame index arrays that tomoscan's
                    # add_theta() reads to identify projections / flats /
                    # darks. Order matches the order frames were captured.
                    if self._unique_ids:
                        self._h5_file.create_dataset(
                            "/defaults/NDArrayUniqueId",
                            data=np.asarray(self._unique_ids, dtype=np.int32))
                        # Fixed-length bytes (matches areaDetector). Width
                        # picked to fit the longest /exchange/* path used.
                        max_len = max(len(loc) for loc in self._frame_locations)
                        loc_arr = np.asarray(self._frame_locations,
                                              dtype=f"|S{max_len}")
                        self._h5_file.create_dataset(
                            "/defaults/HDF5FrameLocation", data=loc_arr)

                    # Capture the filename before closing — needed for the
                    # post-close fsync that pushes NFS write-behind cache
                    # out to the server.
                    closed_path = self._h5_file.filename
                    self._h5_file.close()
                    # NFS pathology: h5py.close() returns as soon as the
                    # bytes are in the Linux page cache (marked dirty);
                    # the kernel then drains them to the NFS server in
                    # the background — and while that drains, every other
                    # op on the same mount gets queued behind it. fsync()
                    # blocks until bytes are durable on the server. For a
                    # multi-GB file on NFS that can take many seconds, so
                    # we do it in a background thread and let _stop_capture
                    # return immediately — otherwise the Qt GUI (or the
                    # caproto putter that called us) would freeze for the
                    # whole flush. WriterState_RBV stays "Closing" until
                    # the fsync completes; "Done" only fires after.
                    self._publish(self.WriterState_RBV, 2)  # Closing
                    self._publish(self.WriteMessage,
                                   "Flushing to disk (NFS sync)…")
                    threading.Thread(
                        target=self._background_fsync,
                        args=(closed_path,),
                        daemon=True,
                        name="hdf5-fsync").start()
                    counts_str = ", ".join(
                        f"{ftype}={c}"
                        for ftype, c in self._dataset_counts.items())
                    msg = (f"HDF5: closed ({self._frames_captured} frames; "
                            f"{counts_str})")
                    if self._dropped_frames:
                        msg += f", {self._dropped_frames} dropped"
                    logger.info(msg)
                except Exception as exc:
                    logger.error("HDF5 close error: %s", exc)
                finally:
                    self._h5_file = None
                    self._h5_datasets = {}

        if _to_str(self.AutoIncrement.value) == "Yes":
            next_num = int(self.FileNumber.value) + 1
            self._publish(self.FileNumber, next_num)
            self.FileNumber._data["value"] = next_num
