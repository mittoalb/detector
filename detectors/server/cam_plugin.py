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

    # Image size — defaults are 0 on purpose. set_camera() populates the
    # real values by asking the camera. If you see 0 in the GUI, that
    # means set_camera never ran (or the poller isn't running) — that's
    # the signal, not a Hamamatsu-shaped placeholder.
    SizeX = pvproperty(value=0, dtype=int)
    SizeX_RBV = pvproperty(value=0, dtype=int, read_only=True)
    SizeY = pvproperty(value=0, dtype=int)
    SizeY_RBV = pvproperty(value=0, dtype=int, read_only=True)
    MaxSizeX_RBV = pvproperty(value=0, dtype=int, read_only=True)
    MaxSizeY_RBV = pvproperty(value=0, dtype=int, read_only=True)
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
    SensorCoolerStatus_RBV = pvproperty(value="Off", dtype=str, max_length=40,
                                          read_only=True)
    AcquisitionFrameRate_RBV = pvproperty(value=0.0, dtype=float, precision=3,
                                            read_only=True)

    # Extra areaDetector-standard params exposed for the GUI's Oryx tabs
    OffsetX = pvproperty(value=0, dtype=int)
    OffsetX_RBV = pvproperty(value=0, dtype=int, read_only=True)
    OffsetY = pvproperty(value=0, dtype=int)
    OffsetY_RBV = pvproperty(value=0, dtype=int, read_only=True)
    TriggerActive = pvproperty(value="RisingEdge", dtype=str, max_length=40)
    TriggerActive_RBV = pvproperty(value="RisingEdge", dtype=str, max_length=40,
                                     read_only=True)
    TriggerDelay = pvproperty(value=0.0, dtype=float, precision=6)
    TriggerDelay_RBV = pvproperty(value=0.0, dtype=float, precision=6,
                                    read_only=True)

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

    # NDArray output — max_length caps the CA payload; sized for the
    # largest sensor we currently support (Oryx 31 MP = 6464×4852).
    ArrayData = pvproperty(value=np.zeros(1, dtype=np.uint16),
                           dtype=ChannelType.INT, max_length=6464 * 4852)
    ArraySize0_RBV = pvproperty(value=0, dtype=int, read_only=True)
    ArraySize1_RBV = pvproperty(value=0, dtype=int, read_only=True)
    # tomoscan / areaDetector also use ArraySizeX_RBV / ArraySizeY_RBV
    ArraySizeX_RBV = pvproperty(value=0, dtype=int, read_only=True)
    ArraySizeY_RBV = pvproperty(value=0, dtype=int, read_only=True)
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
        # Background poller for read-only camera values (temp, cooler,
        # achievable frame rate) — the GUI subprocess reads these via CA
        # and displays them in its Status panel.
        self._poller_thread: Optional[threading.Thread] = None
        self._poller_stop = threading.Event()

    def set_camera(self, camera: Optional[BaseCamera]) -> None:
        """Bind a BaseCamera to this plugin."""
        self._camera = camera
        if camera is None:
            return
        info = camera.get_info()
        self.Manufacturer_RBV._data["value"] = info.vendor
        self.Model_RBV._data["value"] = info.model
        # MaxSizeX/Y ALWAYS reflect the physical sensor. SizeX/Y reflect the
        # current ROI — read live below so the GUI shows what the camera is
        # actually configured to, not "full sensor" when the ROI is smaller.
        self.MaxSizeX_RBV._data["value"] = info.sensor_width
        self.MaxSizeY_RBV._data["value"] = info.sensor_height

        # Live camera state: try each param, silently skip anything a given
        # backend doesn't support (SubarrayHPos vs OffsetX, etc.).
        for param, prop, prop_rbv, cast in [
                ("ExposureTime", self.AcquireTime, self.AcquireTime_RBV, float),
                ("Width", self.SizeX, self.SizeX_RBV, int),
                ("Height", self.SizeY, self.SizeY_RBV, int),
                ("OffsetX", self.OffsetX, self.OffsetX_RBV, int),
                ("OffsetY", self.OffsetY, self.OffsetY_RBV, int),
                ("BinningHorizontal", self.BinX, self.BinX_RBV, int),
                ("BinningVertical", self.BinY, self.BinY_RBV, int),
                ("PixelFormat", self.PixelFormat, self.PixelFormat_RBV, str),
                ("TriggerActive", self.TriggerActive,
                 self.TriggerActive_RBV, str),
                ("TriggerDelay", self.TriggerDelay,
                 self.TriggerDelay_RBV, float),
        ]:
            try:
                v = cast(camera.get_param(param))
                prop._data["value"] = v
                prop_rbv._data["value"] = v
            except Exception:
                continue

        # Array-size mirrors current ROI (or full sensor as a fallback)
        w = int(self.SizeX_RBV._data.get("value") or info.sensor_width)
        h = int(self.SizeY_RBV._data.get("value") or info.sensor_height)
        self.ArraySize0_RBV._data["value"] = w
        self.ArraySize1_RBV._data["value"] = h
        self.ArraySizeX_RBV._data["value"] = w
        self.ArraySizeY_RBV._data["value"] = h
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
        # Kick off the RO poller once the IOC event loop exists — before
        # this, self._publish is a no-op (no async loop to schedule onto).
        self._start_status_poller()

    def _start_status_poller(self) -> None:
        if self._poller_thread is not None:
            return
        self._poller_stop.clear()
        self._poller_thread = threading.Thread(
            target=self._status_poller_loop, daemon=True)
        self._poller_thread.start()

    def _status_poller_loop(self) -> None:
        """Poll camera RO values every second and publish to _RBV PVs.

        Runs in the driver process so ca_proxy (in the GUI subprocess)
        gets live values via CA — its Status panel reads these PVs.
        Also refreshes ROI/binning/pixel-format because those can change
        via non-GUI clients or via the backend's own logic.
        """
        while not self._poller_stop.is_set():
            cam = self._camera
            if cam is not None:
                # Sensor readouts
                self._publish_ro("SensorTemperature", self.SensorTemperature_RBV,
                                   float)
                self._publish_ro("SensorCoolerStatus", self.SensorCoolerStatus_RBV,
                                   str)
                self._publish_ro("AcquisitionFrameRate",
                                   self.AcquisitionFrameRate_RBV, float)
                # Trigger config
                self._publish_ro("TriggerActive", self.TriggerActive_RBV, str)
                self._publish_ro("TriggerDelay", self.TriggerDelay_RBV, float)
                # ROI / format — reflect the camera's actual state
                self._publish_ro("Width", self.SizeX_RBV, int)
                self._publish_ro("Height", self.SizeY_RBV, int)
                self._publish_ro("OffsetX", self.OffsetX_RBV, int)
                self._publish_ro("OffsetY", self.OffsetY_RBV, int)
                self._publish_ro("BinningHorizontal", self.BinX_RBV, int)
                self._publish_ro("BinningVertical", self.BinY_RBV, int)
                self._publish_ro("PixelFormat", self.PixelFormat_RBV, str)
                # Exposure — may have been clamped by the camera to a legal
                # value distinct from what the user asked for.
                self._publish_ro("ExposureTime", self.AcquireTime_RBV, float)
            # Polling every 1s matches the GUI's _refresh_timer cadence
            self._poller_stop.wait(1.0)

    def _publish_ro(self, param_name: str, prop, cast) -> None:
        """Read one param from the camera, coerce, publish to a _RBV PV."""
        try:
            val = self._camera.get_param(param_name)
        except Exception:
            return
        if val is None:
            return
        try:
            val = cast(val)
        except Exception:
            return
        self._publish(prop, val)

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

    @SizeX.putter
    async def SizeX(self, instance, value):
        v = int(value)
        if self._camera:
            try:
                self._camera.set_param("Width", v)
            except Exception as exc:
                logger.warning("Failed to set Width: %s", exc)
        await self.SizeX_RBV.write(v)
        return v

    @SizeY.putter
    async def SizeY(self, instance, value):
        v = int(value)
        if self._camera:
            try:
                self._camera.set_param("Height", v)
            except Exception as exc:
                logger.warning("Failed to set Height: %s", exc)
        await self.SizeY_RBV.write(v)
        return v

    @OffsetX.putter
    async def OffsetX(self, instance, value):
        v = int(value)
        if self._camera:
            try:
                self._camera.set_param("OffsetX", v)
            except Exception as exc:
                logger.warning("Failed to set OffsetX: %s", exc)
        await self.OffsetX_RBV.write(v)
        return v

    @OffsetY.putter
    async def OffsetY(self, instance, value):
        v = int(value)
        if self._camera:
            try:
                self._camera.set_param("OffsetY", v)
            except Exception as exc:
                logger.warning("Failed to set OffsetY: %s", exc)
        await self.OffsetY_RBV.write(v)
        return v

    @TriggerActive.putter
    async def TriggerActive(self, instance, value):
        v = str(value)
        if self._camera:
            try:
                self._camera.set_param("TriggerActive", v)
            except Exception as exc:
                logger.warning("Failed to set TriggerActive: %s", exc)
        await self.TriggerActive_RBV.write(v)
        return v

    @TriggerDelay.putter
    async def TriggerDelay(self, instance, value):
        v = float(value)
        if self._camera:
            try:
                self._camera.set_param("TriggerDelay", v)
            except Exception as exc:
                logger.warning("Failed to set TriggerDelay: %s", exc)
        await self.TriggerDelay_RBV.write(v)
        return v

    @PixelFormat.putter
    async def PixelFormat(self, instance, value):
        v = str(value)
        if self._camera:
            try:
                self._camera.set_param("PixelFormat", v)
            except Exception as exc:
                logger.warning("Failed to set PixelFormat: %s", exc)
        await self.PixelFormat_RBV.write(v)
        return v

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
                    # Waiting-for-trigger timeouts are the expected state
                    # when a trigger line is armed but no signal arrives.
                    # Log them once per burst at INFO, and quietly retry —
                    # otherwise the console fills with a WARNING every 5s
                    # that looks like something's broken.
                    msg = str(exc)
                    is_timeout = (
                        "0x80000106" in msg          # DCAMERR_TIMEOUT
                        or "timed out" in msg.lower()
                        or "TIMEOUT" in msg.upper()
                        or "not running" in msg      # stop/loop race
                    )
                    if is_timeout:
                        if not getattr(self, "_wait_trigger_logged", False):
                            logger.info(
                                "Waiting for trigger — no frame yet "
                                "(will keep retrying silently): %s", exc)
                            self._wait_trigger_logged = True
                    else:
                        logger.warning("Frame acquisition failed: %s", exc)
                    continue
                # Fresh frame → reset the once-per-burst log latch
                self._wait_trigger_logged = False

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
