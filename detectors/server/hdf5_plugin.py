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

# Disable HDF5's POSIX file locking BEFORE h5py imports the libhdf5 it
# delegates to. HDF5 1.10+ takes an exclusive POSIX advisory lock on every
# write-opened file; on NFS that lock leaks past h5py.File.close() and
# remains until *this process* exits, so external viewers / h5dump report
# the file as locked while the IOC keeps running. Only the IOC writes
# these files, so the lock buys us nothing and we turn it off. Must be
# set before libhdf5 loads — hence module-import time, not _start_capture.
os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

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

from detectors.server.hdf5_layout import (
    LayoutWriter,
    ParsedLayout,
    parse_layout_xml,
)
from detectors.server.nd_attributes import default_xml_search_paths

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
    # Default 1000 (not 1) so a freshly-started IOC doesn't silently close
    # the file after a single frame when the user clicks "Start Capture"
    # without first typing a count. 0 means "capture until Stop pressed".
    NumCapture = pvproperty(value=1000, dtype=int)
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
    # Layout-XML mode observability: "Empty" (no XML), "Loaded" (parsed OK),
    # "Failed" (parse or load error), "Simple" (XML set but no ndattr
    # snapshots arriving so we stayed in simple mode).
    XMLLoaded_RBV = pvproperty(value="Empty", dtype=str, max_length=40,
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
        # Gates the "frame arrived while not capturing" warning so an
        # idle plugin (other format is selected) doesn't spam the log.
        self._ever_started = False
        # Layout-XML mode (populated in _start_capture if XMLFileName +
        # per-frame nd_attributes are both available). None → simple mode.
        self._layout_writer: Optional[LayoutWriter] = None
        # Only the most recent snapshot is retained — LayoutWriter.close
        # only needs one representative view for OnFileClose datasets and
        # the /defaults catch-all. Keeping every snapshot would grow O(N).
        self._last_snapshot: Optional[dict] = None
        # Search dirs for basename-only XML PV values (e.g. "TomoScanLayout.xml").
        # Populated by set_layout_search_paths() from DetectorIOC.
        self._layout_search_paths: list = []

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

    def set_layout_search_paths(self, paths: list) -> None:
        """Directories to search for basename-only XMLFileName values.
        Called by DetectorIOC at wiring time."""
        self._layout_search_paths = list(paths)

    def _resolve_layout_xml(self, xml_file: str) -> Optional[str]:
        """Same lookup order as NDAttributesManager._resolve_path — env
        AREA_DETECTOR_ATTRIBUTES_PATH, then constructor-provided search
        dirs, then CWD, then auto-discovered synApps iocBoot dirs.
        Returns None if not found (logs)."""
        if not xml_file:
            return None
        p = Path(xml_file)
        tried = []
        if p.is_absolute():
            tried.append(str(p))
            if p.exists():
                return str(p)
        env = os.environ.get("AREA_DETECTOR_ATTRIBUTES_PATH", "")
        env_dirs = [d for d in env.split(":") if d]
        candidates = (env_dirs + list(self._layout_search_paths)
                      + [os.getcwd()] + default_xml_search_paths())
        seen = set()
        for base in candidates:
            if base in seen:
                continue
            seen.add(base)
            candidate = Path(base) / xml_file
            tried.append(str(candidate))
            if candidate.exists():
                return str(candidate)
        logger.warning(
            "XMLFileName %r not found — layout mode disabled. Tried:\n  %s",
            xml_file, "\n  ".join(tried))
        return None

    def _try_load_layout(self) -> Optional[LayoutWriter]:
        """Attempt to parse XMLFileName and return a LayoutWriter, else None."""
        xml_file = _to_str(self.XMLFileName.value)
        if not xml_file:
            return None
        resolved = self._resolve_layout_xml(xml_file)
        if resolved is None:
            self._publish(self.XMLLoaded_RBV, "Failed")
            return None
        try:
            parsed: ParsedLayout = parse_layout_xml(resolved)
        except Exception as exc:
            logger.error("Layout XML parse failed for %s: %s", resolved, exc)
            self._publish(self.XMLLoaded_RBV, "Failed")
            return None
        logger.info("HDF5 layout mode ENABLED from %s", resolved)
        self._publish(self.XMLLoaded_RBV, "Loaded")
        return LayoutWriter(parsed)

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
            # Only warn if this plugin has actually been used at least once
            # in this session. A fresh IOC + camera-only streaming (user
            # selected the *other* format) is not a bug, just an idle
            # plugin getting frames it doesn't want.
            if self._ever_started:
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
        # /exchange dataset. When layout mode is active, also bundle the
        # NDAttributes snapshot the acquisition loop took at frame time.
        frame_type = metadata.get("frame_type", "Projection") if metadata else "Projection"
        snapshot = metadata.get("nd_attributes") if metadata else None
        item = (frame_type, snapshot, np.ascontiguousarray(frame))
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
                    frame_type, snapshot, frame = item

                    # Layout mode: LayoutWriter routes on the snapshot's
                    # HDF5FrameLocation, creates the file structure lazily
                    # from the parsed XML, and manages HDF5 attributes.
                    # We still write /defaults/{NDArrayUniqueId,HDF5FrameLocation}
                    # from _unique_ids/_frame_locations at close for
                    # tomoscan's add_theta backward compatibility.
                    if self._layout_writer is not None and snapshot is not None:
                        if not self._layout_writer._detector_datasets:
                            # First frame of layout mode — write the file
                            # structure (constants + OnFileOpen ndattrs)
                            # using this snapshot.
                            self._layout_writer.open(self._h5_file, snapshot)
                        path = self._layout_writer.write_frame(
                            self._h5_file, frame, snapshot)
                        self._last_snapshot = snapshot
                        self._frames_captured += 1
                        self._unique_ids.append(self._frames_captured)
                        self._frame_locations.append(path.encode("ascii"))
                    else:
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
                        # Target reached. Close the file from here:
                        # cam_plugin's end_callback (_maybe_stop_capture) fires
                        # the moment the camera stops, which is *before* the
                        # writer drains its queue — so by the time the writer
                        # finally hits the target, end_callback is long gone
                        # and there's no second trigger to close the file.
                        # _stop_capture is safe to call from the writer thread:
                        # the RLock makes its inner `with self._lock:` reentrant,
                        # and it skips joining the writer thread when called
                        # from that same thread.
                        logger.info(
                            "writer_loop: target reached (%d >= %d), closing file",
                            self._frames_captured, num_capture)
                        self._capturing = False
                        self._stop_capture()
                        return
                except Exception as exc:
                    logger.error("HDF5 write error: %s", exc)
                    self._publish(self.WriteStatus, 1)  # Failure
                    self._publish(self.WriteMessage, f"Error: {exc}"[:80])
                    self._publish(self.WriterState_RBV, 3)  # Error
                    # fall through to top of loop; do NOT close the file —
                    # transient write errors shouldn't abort the session.
                    continue

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
                # locking=False (h5py 3.5+) explicitly disables the POSIX
                # advisory lock on this file. Belt-and-suspenders with the
                # HDF5_USE_FILE_LOCKING env var set at module import.
                # Older h5py raises on the locking kwarg → fall back.
                #
                # NOTE: libver is deliberately left at the h5py default
                # ("earliest"). We tried libver="latest" earlier — it
                # writes the newer superblock, which stamps a "file is
                # being written" status flag that stays set if the IOC
                # is killed mid-write. Readers (tomocupy, tomogui) then
                # fail with "file is already open for write (may use
                # <h5clear file> to clear file consistency flags)".
                # Since we don't use SWMR, "latest" bought us nothing.
                try:
                    self._h5_file = h5py.File(full_name, "w",
                                               locking=False)
                except TypeError:
                    self._h5_file = h5py.File(full_name, "w")
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
                self._ever_started = True
                # Layout mode: try to load XMLFileName. If it succeeds we'll
                # enter layout mode on the first frame (once we have a
                # snapshot to pass to LayoutWriter.open()). If XMLFileName
                # is empty or parsing fails we stay in simple mode.
                self._layout_writer = self._try_load_layout()
                self._last_snapshot = None
                if self._layout_writer is None and _to_str(
                        self.XMLFileName.value):
                    # XML was set but couldn't load — stay in simple mode
                    self._publish(self.XMLLoaded_RBV, "Failed")
                elif self._layout_writer is None:
                    self._publish(self.XMLLoaded_RBV, "Empty")
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
        frames coming (e.g. tomoscan flats → projections → darks).

        Guard on _h5_file (the actual resource), not on _capturing —
        the writer thread flips _capturing=False the moment it hits the
        target so on_frame stops enqueuing, but the file is still open
        at that point and needs us to close + fsync it.
        """
        try:
            num_capture = int(self.NumCapture.value)
        except Exception:
            num_capture = 0
        if self._h5_file is None:
            return  # already closed; nothing to do
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
        # Clear _ever_started so the post-stop frames that always trail
        # an active camera don't spam "frame arrived while not capturing"
        # forever. The warning is only useful during the brief window
        # between target-hit and camera-stop, where it catches data loss.
        # Once we've cleanly stopped, the plugin is idle and silent —
        # selecting a different format (TIFF vs HDF5) won't trigger it.
        self._ever_started = False
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
                # Snapshot the file handle upfront so it stays reachable
                # from finally even if any of the pre-close writes raise.
                # PRIOR BUG: if any dataset trim / /defaults create_dataset
                # threw, control jumped to `except` and h5py.File.close()
                # was skipped — leaving the file's superblock consistency
                # flag set, so readers (tomocupy, tomogui) failed on the
                # next open with "file is already open for write". Now
                # close() lives in its own finally: nothing can bypass it.
                h5file = self._h5_file
                closed_path = h5file.filename
                try:
                    # Layout mode close: LayoutWriter handles per-detector
                    # dataset trimming, OnFileClose ndattr writes, and the
                    # /defaults catch-all for un-consumed attributes.
                    if self._layout_writer is not None:
                        try:
                            self._layout_writer.close(
                                h5file, self._last_snapshot)
                        except Exception:
                            logger.exception(
                                "LayoutWriter.close failed — file may be "
                                "missing OnFileClose datasets")
                    else:
                        # Simple-mode: trim each per-type dataset.
                        for ftype, ds in self._h5_datasets.items():
                            count = self._dataset_counts.get(ftype, 0)
                            if count < ds.shape[0]:
                                try:
                                    ds.resize((count,) + ds.shape[1:])
                                except Exception:
                                    logger.exception(
                                        "Failed to trim dataset %s", ftype)

                    # Write per-frame index arrays that tomoscan's
                    # add_theta() reads to identify projections / flats /
                    # darks. Order matches the order frames were captured.
                    # These live at /defaults/NDArrayUniqueId and
                    # /defaults/HDF5FrameLocation; LayoutWriter's catch-all
                    # excludes these two names by design to avoid a clash.
                    try:
                        if self._unique_ids and "/defaults/NDArrayUniqueId" not in h5file:
                            h5file.create_dataset(
                                "/defaults/NDArrayUniqueId",
                                data=np.asarray(self._unique_ids, dtype=np.int32))
                        if self._frame_locations and "/defaults/HDF5FrameLocation" not in h5file:
                            max_len = max(len(loc) for loc in self._frame_locations)
                            loc_arr = np.asarray(self._frame_locations,
                                                  dtype=f"|S{max_len}")
                            h5file.create_dataset(
                                "/defaults/HDF5FrameLocation", data=loc_arr)
                    except Exception:
                        logger.exception(
                            "Failed to write /defaults index datasets")
                except Exception as exc:
                    logger.error("HDF5 pre-close error: %s", exc)
                finally:
                    # Snapshot counts before we null them so the log line
                    # still reports per-type totals.
                    counts_snapshot = dict(self._dataset_counts)
                    # ALWAYS close, no matter what happened above. Then
                    # fire the async fsync. Any exception in close itself
                    # is logged but must not prevent state reset.
                    try:
                        h5file.close()
                    except Exception as exc:
                        logger.error("HDF5 close() raised: %s", exc)
                    self._h5_file = None
                    self._h5_datasets = {}
                    self._layout_writer = None
                    self._last_snapshot = None

                    # NFS pathology: h5py.close() returns as soon as the
                    # bytes are in the Linux page cache (marked dirty);
                    # the kernel drains them to the NFS server in the
                    # background — and while that drains, every other op
                    # on the same mount gets queued behind it. fsync()
                    # blocks until bytes are durable on the server; for
                    # multi-GB files on NFS that can take many seconds,
                    # so we do it in a background thread and let
                    # _stop_capture return immediately — otherwise the
                    # Qt GUI or the caproto putter that called us would
                    # freeze for the whole flush. WriterState_RBV stays
                    # "Closing" until the fsync completes; "Done" only
                    # fires after.
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
                        for ftype, c in counts_snapshot.items())
                    msg = (f"HDF5: closed ({self._frames_captured} frames; "
                            f"{counts_str})")
                    if self._dropped_frames:
                        msg += f", {self._dropped_frames} dropped"
                    logger.info(msg)

        if _to_str(self.AutoIncrement.value) == "Yes":
            next_num = int(self.FileNumber.value) + 1
            self._publish(self.FileNumber, next_num)
            self.FileNumber._data["value"] = next_num
