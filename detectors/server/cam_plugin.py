"""
Camera plugin (cam1:) — areaDetector-compatible PVs for camera control.

Camera-agnostic: works with any BaseCamera subclass.
"""

import asyncio
import logging
import threading
import time
from typing import Optional

import numpy as np

from caproto import ChannelType
from caproto.server import PVGroup, pvproperty

from detectors.core.base import BaseCamera

logger = logging.getLogger(__name__)


class CamPlugin(PVGroup):
    """areaDetector cam1: PVs.

    Camera-agnostic — uses a BaseCamera instance for actual hardware access.
    """

    # Identity
    Manufacturer_RBV = pvproperty(value="", dtype=str, max_length=40, read_only=True)
    Model_RBV = pvproperty(value="", dtype=str, max_length=40, read_only=True)
    PortName_RBV = pvproperty(value="CAM1", dtype=str, max_length=40, read_only=True)

    # Acquisition (enum PVs accept both string and int values from clients)
    Acquire = pvproperty(value="Done", dtype=ChannelType.ENUM,
                          enum_strings=["Done", "Acquire"])
    AcquireBusy = pvproperty(value="Done", dtype=ChannelType.ENUM,
                              enum_strings=["Done", "Busy"], read_only=True)
    ImageMode = pvproperty(value="Continuous", dtype=ChannelType.ENUM,
                            enum_strings=["Single", "Multiple", "Continuous"])
    NumImages = pvproperty(value=1, dtype=int)
    NumImagesCounter_RBV = pvproperty(value=0, dtype=int, read_only=True)
    ArrayCounter_RBV = pvproperty(value=0, dtype=int, read_only=True)

    # Exposure
    AcquireTime = pvproperty(value=0.01, dtype=float, precision=6)
    AcquireTime_RBV = pvproperty(value=0.01, dtype=float, precision=6, read_only=True)
    AcquirePeriod = pvproperty(value=0.0, dtype=float, precision=6)
    AcquirePeriod_RBV = pvproperty(value=0.0, dtype=float, precision=6, read_only=True)

    # Binning
    BinX = pvproperty(value=1, dtype=int)
    BinX_RBV = pvproperty(value=1, dtype=int, read_only=True)
    BinY = pvproperty(value=1, dtype=int)
    BinY_RBV = pvproperty(value=1, dtype=int, read_only=True)

    # Image size
    SizeX = pvproperty(value=4432, dtype=int)
    SizeX_RBV = pvproperty(value=4432, dtype=int, read_only=True)
    SizeY = pvproperty(value=2368, dtype=int)
    SizeY_RBV = pvproperty(value=2368, dtype=int, read_only=True)
    MaxSizeX_RBV = pvproperty(value=4432, dtype=int, read_only=True)
    MaxSizeY_RBV = pvproperty(value=2368, dtype=int, read_only=True)
    MinX = pvproperty(value=0, dtype=int)
    MinY = pvproperty(value=0, dtype=int)

    # Trigger
    TriggerMode = pvproperty(value="Off", dtype=ChannelType.ENUM,
                              enum_strings=["Off", "On"])
    TriggerMode_RBV = pvproperty(value="Off", dtype=ChannelType.ENUM,
                                  enum_strings=["Off", "On"], read_only=True)
    TriggerSource = pvproperty(value="Internal", dtype=ChannelType.ENUM,
                                enum_strings=["Internal", "External", "Software",
                                              "MasterPulse"])
    TriggerSoftware = pvproperty(value=0, dtype=int)
    TriggerOverlap = pvproperty(value="Off", dtype=ChannelType.ENUM,
                                 enum_strings=["Off", "ReadOut"])
    ExposureMode = pvproperty(value="Timed", dtype=str, max_length=40)
    FrameRateEnable = pvproperty(value=0, dtype=int)

    # Pixel format
    PixelFormat = pvproperty(value="Mono16", dtype=str, max_length=40)
    PixelFormat_RBV = pvproperty(value="Mono16", dtype=str, max_length=40, read_only=True)

    # Sensor
    SensorTemperature_RBV = pvproperty(value=0.0, dtype=float, read_only=True)

    # Plugin support
    WaitForPlugins = pvproperty(value="No", dtype=str, max_length=40)
    NDAttributesFile = pvproperty(value="", dtype=str, max_length=256)
    NDAttributesMacros = pvproperty(value="", dtype=str, max_length=256)
    UniqueIdMode = pvproperty(value="Camera", dtype=ChannelType.ENUM,
                               enum_strings=["Driver", "User", "Camera",
                                             "FileNumber"])
    ArrayCallbacks = pvproperty(value="Enable", dtype=ChannelType.ENUM,
                                 enum_strings=["Disable", "Enable"])

    # FrameType: tomoscan sets this to "Projection", "FlatField", or
    # "DarkField" before each scan phase. The HDF5 plugin reads it
    # (via the per-frame metadata) to route frames to /exchange/data,
    # /exchange/data_white, or /exchange/data_dark respectively.
    FrameType = pvproperty(value="Projection", dtype=ChannelType.ENUM,
                            enum_strings=["Projection", "FlatField",
                                           "DarkField"])

    # NDArray output
    ArrayData = pvproperty(value=np.zeros(1, dtype=np.uint16),
                           dtype=ChannelType.INT, max_length=4480 * 2368)
    ArraySize0_RBV = pvproperty(value=4432, dtype=int, read_only=True)
    ArraySize1_RBV = pvproperty(value=2368, dtype=int, read_only=True)
    # tomoscan / areaDetector also use ArraySizeX_RBV / ArraySizeY_RBV
    ArraySizeX_RBV = pvproperty(value=4432, dtype=int, read_only=True)
    ArraySizeY_RBV = pvproperty(value=2368, dtype=int, read_only=True)
    NDimensions_RBV = pvproperty(value=2, dtype=int, read_only=True)
    ColorMode_RBV = pvproperty(value=0, dtype=int, read_only=True)
    DataType_RBV = pvproperty(value="UInt16", dtype=str, max_length=40, read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._camera: Optional[BaseCamera] = None
        self._acquiring = False
        self._acq_thread = None
        self._frame_counter = 0
        self._frame_callbacks = []
        self._end_callbacks = []
        self._software_trigger_event = threading.Event()
        self._async_loop = None
        # Throttle CA ArrayData publishes (heavy serialization).
        # Use PVA image1:ArrayData for full-rate live viewing.
        self._array_data_max_fps = 5.0
        self._array_data_last_t = 0.0

    def set_camera(self, camera: Optional[BaseCamera]) -> None:
        """Bind a BaseCamera to this plugin."""
        self._camera = camera
        if camera is None:
            return
        info = camera.get_info()
        self.Manufacturer_RBV._data["value"] = info.vendor
        self.Model_RBV._data["value"] = info.model
        self.SizeX_RBV._data["value"] = info.sensor_width
        self.SizeY_RBV._data["value"] = info.sensor_height
        self.MaxSizeX_RBV._data["value"] = info.sensor_width
        self.MaxSizeY_RBV._data["value"] = info.sensor_height
        self.ArraySize0_RBV._data["value"] = info.sensor_width
        self.ArraySize1_RBV._data["value"] = info.sensor_height
        self.ArraySizeX_RBV._data["value"] = info.sensor_width
        self.ArraySizeY_RBV._data["value"] = info.sensor_height
        # Read live ExposureTime
        try:
            exp = camera.get_param("ExposureTime")
            self.AcquireTime._data["value"] = float(exp)
            self.AcquireTime_RBV._data["value"] = float(exp)
        except Exception:
            pass
        # Sync trigger PVs to the camera's actual state so GUI/EPICS clients
        # see the hardware defaults (TriggerSource=External etc.) on startup.
        try:
            src = str(camera.get_param("TriggerSource"))
            self.TriggerSource._data["value"] = src
            # TriggerMode "On" iff TriggerSource is External
            tmode = "On" if src == "External" else "Off"
            self.TriggerMode._data["value"] = tmode
            self.TriggerMode_RBV._data["value"] = tmode
        except Exception:
            pass
        logger.info("CamPlugin bound to %s %s (%dx%d)",
                    info.vendor, info.model,
                    info.sensor_width, info.sensor_height)

    def register_frame_callback(self, callback) -> None:
        self._frame_callbacks.append(callback)

    def register_end_callback(self, callback) -> None:
        """Register a callback called when acquisition stops (any reason)."""
        self._end_callbacks.append(callback)

    def _publish(self, prop, value):
        """Thread-safe PV update with monitor notification."""
        if self._async_loop is not None:
            asyncio.run_coroutine_threadsafe(prop.write(value), self._async_loop)

    @ArrayCallbacks.startup
    async def ArrayCallbacks(self, instance, async_lib):
        self._async_loop = asyncio.get_running_loop()

    @Acquire.putter
    async def Acquire(self, instance, value):
        if value in (1, "Acquire") and not self._acquiring:
            # Publish AcquireBusy=1 SYNCHRONOUSLY before returning. Tomoscan's
            # wait_camera_done() reads the cached monitor value right after
            # its own Acquire.put returns; if AcquireBusy is still the stale
            # 0 from the previous phase, it returns immediately and the
            # current phase (e.g. dark fields) is skipped entirely.
            await self.AcquireBusy.write(1)
            self._start_acquisition()
            return 1
        elif value in (0, "Done") or (value not in (1, "Acquire") and self._acquiring):
            self._stop_acquisition()
            return 0
        return value

    @AcquireTime.putter
    async def AcquireTime(self, instance, value):
        v = float(value)
        if self._camera:
            try:
                self._camera.set_param("ExposureTime", v)
            except Exception as exc:
                logger.warning("Failed to set ExposureTime: %s", exc)
        await self.AcquireTime_RBV.write(v)
        return v

    @TriggerMode.putter
    async def TriggerMode(self, instance, value):
        v = str(value)
        if self._camera:
            try:
                self._camera.set_param("TriggerMode", v)
            except Exception as exc:
                logger.warning("Failed to set TriggerMode: %s", exc)
        await self.TriggerMode_RBV.write(v)
        return v

    @TriggerSoftware.putter
    async def TriggerSoftware(self, instance, value):
        if value == 1:
            self._software_trigger_event.set()
            if self._camera:
                try:
                    self._camera.software_trigger()
                except Exception:
                    pass
        return value

    @TriggerSource.putter
    async def TriggerSource(self, instance, value):
        v = str(value)
        if self._camera:
            try:
                self._camera.set_param("TriggerSource", v)
            except Exception as exc:
                logger.warning("Failed to set TriggerSource: %s", exc)
        return v

    @ExposureMode.putter
    async def ExposureMode(self, instance, value):
        # Standard areaDetector enum, mostly informational on DCAM cameras
        return str(value)

    @TriggerOverlap.putter
    async def TriggerOverlap(self, instance, value):
        # areaDetector compat: "Off" or "ReadOut". On DCAM cameras this is
        # controlled via TriggerGlobalExposure ("DelayedReadout" maps to
        # ReadOut overlap).
        v = str(value)
        if self._camera:
            try:
                if v in ("ReadOut", "Readout"):
                    self._camera.set_param("TriggerGlobalExposure",
                                            "DelayedReadout")
            except Exception as exc:
                logger.warning("Failed to set TriggerOverlap: %s", exc)
        return v

    @FrameRateEnable.putter
    async def FrameRateEnable(self, instance, value):
        return int(value)

    @BinX.putter
    async def BinX(self, instance, value):
        v = int(value)
        if self._camera:
            try:
                self._camera.set_param("BinningHorizontal", v)
                self._camera.set_param("BinningVertical", v)
            except Exception as exc:
                logger.warning("Failed to set Binning: %s", exc)
        await self.BinX_RBV.write(v)
        await self.BinY_RBV.write(v)
        return v

    def _start_acquisition(self):
        if self._acq_thread is not None and self._acq_thread.is_alive():
            return
        self._acquiring = True
        self._frame_counter = 0
        # Publish counters as 0 immediately. Otherwise NumImagesCounter_RBV
        # carries the previous phase's value (e.g. "20/721" right after a
        # 20-frame flat-field collection) until the first new frame arrives,
        # which confuses tomoscan's per-phase progress readback.
        self._publish(self.NumImagesCounter_RBV, 0)
        self._publish(self.ArrayCounter_RBV, 0)
        self._acq_thread = threading.Thread(
            target=self._acquisition_loop, daemon=True)
        self._acq_thread.start()

    def _stop_acquisition(self):
        self._acquiring = False
        self._software_trigger_event.set()
        if self._acq_thread is not None:
            self._acq_thread.join(timeout=3.0)
            self._acq_thread = None

    def _acquisition_loop(self):
        try:
            if self._camera:
                try:
                    self._camera.set_param("ExposureTime",
                                           float(self.AcquireTime.value))
                    self._camera.start_acquisition()
                except Exception as exc:
                    # DO NOT null out self._camera here. A transient
                    # start_acquisition failure must not silently switch
                    # the IOC to synthetic-frame mode for the rest of the
                    # session (looks like a "parasite simulator" to users
                    # downstream — fake data written to HDF5 looks real).
                    logger.error("Failed to start acquisition: %s", exc)
                    self._acquiring = False
                    self._publish(self.AcquireBusy, 0)
                    self._publish(self.Acquire, 0)
                    return

            self._publish(self.AcquireBusy, 1)

            while self._acquiring:
                image_mode = str(self.ImageMode.value)
                num_images = int(self.NumImages.value)

                if image_mode == "Single":
                    target = 1
                elif image_mode == "Multiple":
                    target = num_images
                else:
                    target = 0

                if target > 0 and self._frame_counter >= target:
                    break

                if self._camera is None:
                    # No camera bound. Bail out — do NOT synthesize frames.
                    # If you want simulator behavior, run with
                    # `--camera simulator`, which provides a real
                    # SimulatorCamera instance.
                    logger.error("No camera bound; aborting acquisition loop")
                    break
                try:
                    frame = self._camera.acquire_frame(timeout_ms=5000)
                except Exception as exc:
                    logger.warning("Frame acquisition failed: %s", exc)
                    continue

                if frame is None:
                    continue

                self._frame_counter += 1
                # Read FrameType once per frame so the HDF5 plugin can route
                # to the correct /exchange dataset (data / data_white /
                # data_dark) without subscribing to the PV itself.
                try:
                    frame_type = str(self.FrameType.value)
                except Exception:
                    frame_type = "Projection"
                metadata = {
                    "timestamp": time.time(),
                    "frame_number": self._frame_counter,
                    "width": frame.shape[1],
                    "height": frame.shape[0],
                    "frame_type": frame_type,
                }

                # Update counters every frame (small, cheap)
                self._publish(self.NumImagesCounter_RBV, self._frame_counter)
                self._publish(self.ArrayCounter_RBV, self._frame_counter)
                # Size PVs only on shape change
                if (frame.shape[1] != self.ArraySize0_RBV._data["value"] or
                        frame.shape[0] != self.ArraySize1_RBV._data["value"]):
                    self._publish(self.ArraySize0_RBV, frame.shape[1])
                    self._publish(self.ArraySize1_RBV, frame.shape[0])
                    self._publish(self.ArraySizeX_RBV, frame.shape[1])
                    self._publish(self.ArraySizeY_RBV, frame.shape[0])
                # CA ArrayData throttled — full-rate live view via PVA.
                # 10M-element CA writes can't keep up with sensor rate.
                now = time.time()
                if (self._array_data_max_fps <= 0 or
                        now - self._array_data_last_t >=
                        1.0 / self._array_data_max_fps):
                    self._array_data_last_t = now
                    try:
                        self._publish(self.ArrayData, frame.flatten())
                    except Exception:
                        pass

                for cb in self._frame_callbacks:
                    try:
                        cb(frame, metadata)
                    except Exception as exc:
                        logger.error("Frame callback error: %s", exc)

        finally:
            if self._camera:
                try:
                    self._camera.stop_acquisition()
                except Exception:
                    pass
            self._acquiring = False
            self._publish(self.AcquireBusy, 0)
            self._publish(self.Acquire, 0)
            logger.info("Acquisition stopped after %d frames", self._frame_counter)
            # Notify any plugins (e.g. HDF5) that acquisition has ended
            for cb in self._end_callbacks:
                try:
                    cb()
                except Exception as exc:
                    logger.error("End callback error: %s", exc)
