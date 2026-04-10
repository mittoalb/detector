#!/usr/bin/env python3
"""
areaDetector-compatible EPICS PV server for ORCA Fire via harvesters/GenTL.

Serves the standard areaDetector cam1: and HDF1: PVs so that tomoscan
(and other areaDetector clients) can drive the camera without DCAM-SDK.

Architecture:
    tomoscan --> [EPICS CA] --> this server --> harvesters --> coaxlink.cti --> ORCA Fire
"""

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from caproto.server import PVGroup, SubGroup, pvproperty, run
from caproto import ChannelType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# cam1: plugin — mirrors the areaDetector camera driver PVs
# ---------------------------------------------------------------------------

class CamPlugin(PVGroup):
    """areaDetector cam1: PVs used by tomoscan."""

    # --- Identity (read-only) ---
    Manufacturer_RBV = pvproperty(value="Hamamatsu", dtype=str, read_only=True)
    Model_RBV = pvproperty(value="ORCA Fire", dtype=str, read_only=True)
    PortName_RBV = pvproperty(value="ORCA1", dtype=str, read_only=True)

    # --- Acquisition control ---
    Acquire = pvproperty(value=0, dtype=int)
    AcquireBusy = pvproperty(value=0, dtype=int, read_only=True)
    ImageMode = pvproperty(value="Multiple", dtype=str)
    NumImages = pvproperty(value=1, dtype=int)
    NumImagesCounter_RBV = pvproperty(value=0, dtype=int, read_only=True)
    ArrayCounter_RBV = pvproperty(value=0, dtype=int, read_only=True)

    # --- Exposure ---
    AcquireTime = pvproperty(value=0.01, dtype=float, precision=6)
    AcquireTime_RBV = pvproperty(value=0.01, dtype=float, precision=6, read_only=True)
    AcquirePeriod = pvproperty(value=0.0, dtype=float, precision=6)
    AcquirePeriod_RBV = pvproperty(value=0.0, dtype=float, precision=6, read_only=True)

    # --- Binning ---
    BinX = pvproperty(value=1, dtype=int)
    BinX_RBV = pvproperty(value=1, dtype=int, read_only=True)
    BinY = pvproperty(value=1, dtype=int)
    BinY_RBV = pvproperty(value=1, dtype=int, read_only=True)

    # --- Image size ---
    SizeX_RBV = pvproperty(value=4432, dtype=int, read_only=True)
    SizeY_RBV = pvproperty(value=2368, dtype=int, read_only=True)
    MaxSizeX_RBV = pvproperty(value=4432, dtype=int, read_only=True)
    MaxSizeY_RBV = pvproperty(value=2368, dtype=int, read_only=True)

    # --- Trigger ---
    TriggerMode = pvproperty(value="Off", dtype=str)
    TriggerMode_RBV = pvproperty(value="Off", dtype=str, read_only=True)
    TriggerSource = pvproperty(value="Internal", dtype=str)
    TriggerSoftware = pvproperty(value=0, dtype=int)
    TriggerOverlap = pvproperty(value="Off", dtype=str)
    ExposureMode = pvproperty(value="Timed", dtype=str)
    FrameRateEnable = pvproperty(value=0, dtype=int)

    # --- Pixel format ---
    PixelFormat = pvproperty(value="Mono16", dtype=str)
    PixelFormat_RBV = pvproperty(value="Mono16", dtype=str, read_only=True)

    # --- Plugin support ---
    WaitForPlugins = pvproperty(value="No", dtype=str)
    NDAttributesFile = pvproperty(value="", dtype=str, max_length=256)
    NDAttributesMacros = pvproperty(value="", dtype=str, max_length=256)
    UniqueIdMode = pvproperty(value=0, dtype=int)
    ArrayCallbacks = pvproperty(value="Enable", dtype=str)

    # --- NDArray output (for live viewing) ---
    ArrayData = pvproperty(value=np.zeros(1, dtype=np.uint16),
                           dtype=ChannelType.INT, max_length=4432 * 2368)
    ArraySize0_RBV = pvproperty(value=4432, dtype=int, read_only=True)
    ArraySize1_RBV = pvproperty(value=2368, dtype=int, read_only=True)
    NDimensions_RBV = pvproperty(value=2, dtype=int, read_only=True)
    ColorMode_RBV = pvproperty(value=0, dtype=int, read_only=True)
    DataType_RBV = pvproperty(value="UInt16", dtype=str, read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._acquirer = None
        self._acquiring = False
        self._acq_thread = None
        self._frame_counter = 0
        self._frame_callbacks = []
        self._software_trigger_event = threading.Event()

    def set_acquirer(self, acquirer):
        """Set the harvesters acquirer instance."""
        self._acquirer = acquirer
        if acquirer is not None:
            try:
                info = acquirer.describe_device()
                # Will be set via async_lib in startup if needed
                logger.info("Camera: %s %s (SN: %s)",
                            info.get("vendor", ""),
                            info.get("model", ""),
                            info.get("serial_number", ""))
            except Exception:
                pass

    def register_frame_callback(self, callback):
        """Register a callback(frame, metadata) for each acquired frame."""
        self._frame_callbacks.append(callback)

    @Acquire.putter
    async def Acquire(self, instance, value):
        # tomoscan puts 'Acquire' (string) or 1 to start, 0/'Done' to stop
        if value in (1, "Acquire") and not self._acquiring:
            self._start_acquisition()
            return 1
        elif value in (0, "Done") or (value not in (1, "Acquire") and self._acquiring):
            self._stop_acquisition()
            return 0
        return value

    @AcquireTime.putter
    async def AcquireTime(self, instance, value):
        value = float(value)
        if self._acquirer:
            try:
                # Convert seconds to microseconds for camera
                self._acquirer.configure({"ExposureTime": value * 1e6})
            except Exception as exc:
                logger.warning("Failed to set ExposureTime: %s", exc)
        await self.AcquireTime_RBV.write(value)
        return value

    @AcquirePeriod.putter
    async def AcquirePeriod(self, instance, value):
        await self.AcquirePeriod_RBV.write(float(value))
        return float(value)

    @BinX.putter
    async def BinX(self, instance, value):
        if self._acquirer:
            try:
                self._acquirer.configure({"BinningHorizontal": int(value)})
            except Exception as exc:
                logger.warning("Failed to set BinX: %s", exc)
        await self.BinX_RBV.write(int(value))
        return int(value)

    @BinY.putter
    async def BinY(self, instance, value):
        if self._acquirer:
            try:
                self._acquirer.configure({"BinningVertical": int(value)})
            except Exception as exc:
                logger.warning("Failed to set BinY: %s", exc)
        await self.BinY_RBV.write(int(value))
        return int(value)

    @TriggerMode.putter
    async def TriggerMode(self, instance, value):
        if self._acquirer:
            try:
                mode = "On" if value in ("External", "On") else "Off"
                self._acquirer.configure({"TriggerMode": mode})
            except Exception as exc:
                logger.warning("Failed to set TriggerMode: %s", exc)
        await self.TriggerMode_RBV.write(value)
        return value

    @TriggerSoftware.putter
    async def TriggerSoftware(self, instance, value):
        if value == 1:
            self._software_trigger_event.set()
        return value

    def _start_acquisition(self):
        """Start the acquisition thread."""
        if self._acq_thread is not None and self._acq_thread.is_alive():
            return
        self._acquiring = True
        self._frame_counter = 0
        self._acq_thread = threading.Thread(target=self._acquisition_loop, daemon=True)
        self._acq_thread.start()

    def _stop_acquisition(self):
        """Stop the acquisition thread."""
        self._acquiring = False
        self._software_trigger_event.set()  # unblock any waiting trigger
        if self._acq_thread is not None:
            self._acq_thread.join(timeout=3.0)
            self._acq_thread = None

    def _acquisition_loop(self):
        """Background acquisition thread."""
        try:
            image_mode = str(self.ImageMode.value)
            num_images = int(self.NumImages.value)
            trigger_mode = str(self.TriggerMode.value)
            acquire_time = float(self.AcquireTime.value)

            # Configure camera
            if self._acquirer:
                try:
                    self._acquirer.configure({
                        "ExposureTime": acquire_time * 1e6,
                    })
                    self._acquirer.start_acquisition()
                except Exception as exc:
                    logger.error("Failed to start camera acquisition: %s", exc)
                    self._acquirer = None  # fall back to simulation

            self.AcquireBusy._data["value"] = 1

            while self._acquiring:
                # Determine how many frames to collect
                if image_mode == "Single":
                    target = 1
                elif image_mode == "Multiple":
                    target = num_images
                else:  # Continuous
                    target = 0  # unlimited

                if target > 0 and self._frame_counter >= target:
                    break

                # Wait for software trigger if in external/software trigger mode
                if trigger_mode in ("External", "On"):
                    self._software_trigger_event.clear()
                    self._software_trigger_event.wait(timeout=5.0)
                    if not self._acquiring:
                        break

                # Acquire frame
                frame = self._acquire_one_frame(acquire_time)
                if frame is None:
                    continue

                self._frame_counter += 1
                metadata = {
                    "timestamp": time.time(),
                    "frame_number": self._frame_counter,
                    "width": frame.shape[1],
                    "height": frame.shape[0],
                }

                # Update PVs
                self.NumImagesCounter_RBV._data["value"] = self._frame_counter
                self.ArrayCounter_RBV._data["value"] = self._frame_counter
                self.ArraySize0_RBV._data["value"] = frame.shape[1]
                self.ArraySize1_RBV._data["value"] = frame.shape[0]

                # Publish frame data for live viewing
                try:
                    self.ArrayData._data["value"] = frame.flatten()
                except Exception:
                    pass

                # Notify frame callbacks (HDF5 writer, etc.)
                for cb in self._frame_callbacks:
                    try:
                        cb(frame, metadata)
                    except Exception as exc:
                        logger.error("Frame callback error: %s", exc)

                logger.debug("Frame %d: %s", self._frame_counter, frame.shape)

        finally:
            if self._acquirer:
                try:
                    self._acquirer.stop_acquisition()
                except Exception:
                    pass
            self._acquiring = False
            self.AcquireBusy._data["value"] = 0
            self.Acquire._data["value"] = 0
            logger.info("Acquisition stopped after %d frames", self._frame_counter)

    def _acquire_one_frame(self, acquire_time: float) -> Optional[np.ndarray]:
        """Acquire a single frame from real camera or simulation."""
        if self._acquirer:
            try:
                return self._acquirer.acquire_frame(timeout_ms=5000)
            except Exception as exc:
                logger.warning("Frame acquisition failed: %s", exc)
                return None
        else:
            # Simulation mode
            time.sleep(acquire_time)
            width = int(self.SizeX_RBV.value)
            height = int(self.SizeY_RBV.value)
            frame = np.random.randint(0, 1000, (height, width), dtype=np.uint16)
            y, x = np.ogrid[:height, :width]
            frame[y % 20 < 10] += 500
            frame[x % 20 < 10] += 500
            return frame


# ---------------------------------------------------------------------------
# HDF1: plugin — mimics the areaDetector HDF5 file writer plugin
# ---------------------------------------------------------------------------

class HDF5Plugin(PVGroup):
    """areaDetector HDF1: PVs for file writing, used by tomoscan."""

    # --- Port wiring ---
    NDArrayPort = pvproperty(value="ORCA1", dtype=str)
    EnableCallbacks = pvproperty(value="Enable", dtype=str)

    # --- File path / name ---
    FilePath = pvproperty(value="", dtype=str, max_length=256)
    FilePath_RBV = pvproperty(value="", dtype=str, max_length=256, read_only=True)
    FilePathExists_RBV = pvproperty(value=0, dtype=int, read_only=True)
    FileName = pvproperty(value="", dtype=str, max_length=256)
    FileName_RBV = pvproperty(value="", dtype=str, max_length=256, read_only=True)
    FileNumber = pvproperty(value=1, dtype=int)
    FileTemplate = pvproperty(value="%s%s_%04d.h5", dtype=str, max_length=256)
    FullFileName_RBV = pvproperty(value="", dtype=str, max_length=256, read_only=True)
    AutoIncrement = pvproperty(value="Yes", dtype=str)
    AutoSave = pvproperty(value="Yes", dtype=str)

    # --- Capture control ---
    FileWriteMode = pvproperty(value="Stream", dtype=str)
    NumCapture = pvproperty(value=1, dtype=int)
    NumCaptured_RBV = pvproperty(value=0, dtype=int, read_only=True)
    Capture = pvproperty(value=0, dtype=int)
    Capture_RBV = pvproperty(value=0, dtype=int, read_only=True)
    WriteStatus = pvproperty(value=0, dtype=int, read_only=True)

    # --- XML layout ---
    XMLFileName = pvproperty(value="", dtype=str, max_length=256)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._h5_file = None
        self._h5_dataset = None
        self._capturing = False
        self._frames_captured = 0
        self._lock = threading.Lock()

    @FilePath.putter
    async def FilePath(self, instance, value):
        value = str(value)
        await self.FilePath_RBV.write(value)
        exists = 1 if os.path.isdir(value) else 0
        await self.FilePathExists_RBV.write(exists)
        return value

    @FileName.putter
    async def FileName(self, instance, value):
        await self.FileName_RBV.write(str(value))
        return str(value)

    @Capture.putter
    async def Capture(self, instance, value):
        # tomoscan puts 'Capture'/1 to start, 'Done'/0 to stop
        if value in (1, "Capture") and not self._capturing:
            self._start_capture()
            return 1
        elif value in (0, "Done") or (value not in (1, "Capture") and self._capturing):
            self._stop_capture()
            return 0
        return value

    def on_frame(self, frame: np.ndarray, metadata: dict):
        """Called by CamPlugin for each acquired frame."""
        if not self._capturing:
            return

        with self._lock:
            if self._h5_file is None:
                return

            try:
                idx = self._frames_captured
                num_capture = int(self.NumCapture.value)

                # Extend dataset and write frame
                if idx == 0:
                    self._h5_dataset = self._h5_file.create_dataset(
                        "/exchange/data",
                        shape=(1, frame.shape[0], frame.shape[1]),
                        maxshape=(None, frame.shape[0], frame.shape[1]),
                        dtype=np.uint16,
                        chunks=(1, frame.shape[0], frame.shape[1]),
                    )
                else:
                    self._h5_dataset.resize(idx + 1, axis=0)

                self._h5_dataset[idx] = frame
                self._h5_file.flush()

                self._frames_captured = idx + 1
                self.NumCaptured_RBV._data["value"] = self._frames_captured

                logger.info("HDF5: wrote frame %d/%d", self._frames_captured, num_capture)

                # Check if capture is complete
                if num_capture > 0 and self._frames_captured >= num_capture:
                    self._stop_capture()

            except Exception as exc:
                logger.error("HDF5 write error: %s", exc)
                self.WriteStatus._data["value"] = 1

    def _start_capture(self):
        """Open HDF5 file and start capturing."""
        try:
            import h5py
        except ImportError:
            logger.error("h5py is required for HDF5 writing. pip install h5py")
            self.WriteStatus._data["value"] = 1
            return

        file_path = str(self.FilePath.value)
        file_name = str(self.FileName.value)
        file_number = int(self.FileNumber.value)
        file_template = str(self.FileTemplate.value)

        try:
            full_name = file_template % (file_path, file_name, file_number)
        except (TypeError, ValueError):
            full_name = os.path.join(file_path, f"{file_name}_{file_number:04d}.h5")

        # Ensure directory exists
        Path(full_name).parent.mkdir(parents=True, exist_ok=True)

        try:
            self._h5_file = __import__("h5py").File(full_name, "w")
            self._frames_captured = 0
            self._capturing = True
            self.Capture_RBV._data["value"] = 1
            self.NumCaptured_RBV._data["value"] = 0
            self.WriteStatus._data["value"] = 0
            self.FullFileName_RBV._data["value"] = full_name
            logger.info("HDF5: opened %s for writing", full_name)
        except Exception as exc:
            logger.error("Failed to open HDF5 file %s: %s", full_name, exc)
            self.WriteStatus._data["value"] = 1

    def _stop_capture(self):
        """Close the HDF5 file."""
        self._capturing = False
        self.Capture_RBV._data["value"] = 0

        with self._lock:
            if self._h5_file is not None:
                try:
                    self._h5_file.close()
                    logger.info("HDF5: closed file (%d frames written)",
                                self._frames_captured)
                except Exception as exc:
                    logger.error("Failed to close HDF5 file: %s", exc)
                finally:
                    self._h5_file = None
                    self._h5_dataset = None

        # Auto-increment file number
        if str(self.AutoIncrement.value) == "Yes":
            self.FileNumber._data["value"] = int(self.FileNumber.value) + 1


# ---------------------------------------------------------------------------
# Top-level IOC — wires cam1 + HDF1 together with the harvesters backend
# ---------------------------------------------------------------------------

class OrcaFireIOC(PVGroup):
    """
    areaDetector-compatible IOC for ORCA Fire via harvesters/GenTL.

    Serves PVs like:
        {prefix}cam1:Acquire
        {prefix}cam1:AcquireTime
        {prefix}HDF1:Capture
        {prefix}HDF1:FilePath
        etc.

    Compatible with tomoscan step-scan workflows.
    """

    cam1 = SubGroup(CamPlugin, prefix="cam1:")
    HDF1 = SubGroup(HDF5Plugin, prefix="HDF1:")

    def __init__(self, *args,
                 gen_tl_producer_path: Optional[str] = None,
                 device_index: int = 0,
                 **kwargs):
        super().__init__(*args, **kwargs)

        # Try to connect to real camera
        acquirer = None
        try:
            from orca.harvesters_orca_fire import OrcaFireAcquirer
            acquirer = OrcaFireAcquirer(
                gen_tl_producer_path=gen_tl_producer_path,
                device_index=device_index,
            )
            acquirer.open()
            logger.info("Real camera connected")
        except Exception as exc:
            logger.warning("No camera available (%s), running in simulation mode", exc)
            acquirer = None

        # Wire up cam1 with acquirer and connect HDF1 as frame callback
        self.cam1.set_acquirer(acquirer)
        self.cam1.register_frame_callback(self.HDF1.on_frame)
