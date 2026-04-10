import logging
import threading
import time
from typing import Optional

import numpy as np

try:
    from caproto.server import PVGroup, pvproperty, run
    from caproto import ChannelType
except ImportError as exc:
    raise ImportError(
        "caproto is required for EPICS PV support. "
        "Install it in your conda env: `conda activate detector && pip install caproto`."
    ) from exc

from orca.harvesters_orca_fire import OrcaFireAcquirer


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class OrcaFireAreaDetector(PVGroup):
    """EPICS-like AreaDetector PV group for ORCA Fire with NDArray streaming."""

    # Control parameters
    ExposureTime = pvproperty(value=1000.0, dtype=float, precision=3)
    AcquireTime = pvproperty(value=1000.0, dtype=float, precision=3)
    AcquisitionFrameRate = pvproperty(value=30.0, dtype=float, precision=3)
    Width = pvproperty(value=4432, dtype=int)
    Height = pvproperty(value=2369, dtype=int)
    Binning = pvproperty(value=1, dtype=int)
    ImageMode = pvproperty(value=0, dtype=int)
    NumImages = pvproperty(value=10, dtype=int)
    Acquire = pvproperty(value=0, dtype=int)
    StatusMessage = pvproperty(value="Idle", dtype=str)
    ImageCounter = pvproperty(value=0, dtype=int)
    FrameRate = pvproperty(value=30.0, dtype=float, precision=3)
    CameraModel = pvproperty(value="ORCA Fire", dtype=str)
    CameraSerial = pvproperty(value="Unknown", dtype=str)

    # NDArray attributes for pystream compatibility
    ArrayData = pvproperty(value=np.array([]), dtype=ChannelType.INT, max_length=4432*2369)
    ArraySize = pvproperty(value=np.array([4432*2369], dtype=np.int32), dtype=ChannelType.LONG)
    ArraySize0 = pvproperty(value=4432, dtype=int)  # Width
    ArraySize1 = pvproperty(value=2369, dtype=int)  # Height
    ArraySize2 = pvproperty(value=0, dtype=int)    # Depth (0 for 2D)
    ColorMode = pvproperty(value=0, dtype=int)     # 0 = mono
    DataType = pvproperty(value=1, dtype=int)      # 1 = uint16
    UniqueId = pvproperty(value=0, dtype=int)
    TimeStamp = pvproperty(value=np.array([0, 0], dtype=np.int32), dtype=ChannelType.LONG)

    def __init__(self, *args, gen_tl_producer_path: Optional[str] = None, device_index: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.acquirer = None
        try:
            self.acquirer = OrcaFireAcquirer(gen_tl_producer_path=gen_tl_producer_path, device_index=device_index)
            self.acquirer.open()
            self.acquirer.register_frame_callback(self._on_frame)
            logger.info("Camera initialized successfully")
        except Exception as e:
            logger.warning(f"Could not initialize camera: {e}. Running in simulation mode.")
            self.acquirer = None
        self._acquirer = self.acquirer  # Keep reference for cleanup
        self._acquire_thread: Optional[threading.Thread] = None
        self._run_acquisition = threading.Event()

    def _on_frame(self, frame: np.ndarray, metadata: dict) -> None:
        image_counter = int(self.ImageCounter.value) + 1
        self.ImageCounter.put(image_counter)
        self.UniqueId.put(image_counter)
        self.StatusMessage.put("Acquiring")

        # Publish NDArray data for pystream
        self.ArrayData.put(frame.flatten())
        self.ArraySize.put(np.array([frame.size], dtype=np.int32))
        self.ArraySize0.put(frame.shape[1])  # Width
        self.ArraySize1.put(frame.shape[0])  # Height
        self.ArraySize2.put(0 if frame.ndim == 2 else frame.shape[2])  # Depth

        # Update timestamp (simplified)
        import time
        current_time = time.time()
        seconds = int(current_time)
        nanoseconds = int((current_time - seconds) * 1e9)
        self.TimeStamp.put(np.array([seconds, nanoseconds], dtype=np.int32))

        logger.info("Frame %d acquired: %s", image_counter, frame.shape)

    @Acquire.putter
    async def Acquire(self, instance, value):
        if value == 1:
            self.start_acquisition()
            return 1
        self.stop_acquisition()
        return 0

    @ExposureTime.putter
    async def ExposureTime(self, instance, value):
        if self.acquirer:
            self.acquirer.configure({"ExposureTime": float(value)})
        return float(value)

    @AcquireTime.putter
    async def AcquireTime(self, instance, value):
        if self.acquirer:
            self.acquirer.configure({"AcquireTime": float(value)})
        return float(value)

    @AcquisitionFrameRate.putter
    async def AcquisitionFrameRate(self, instance, value):
        if self.acquirer:
            self.acquirer.configure({"AcquisitionFrameRate": float(value)})
        self.FrameRate.put(float(value))
        return float(value)

    @Width.putter
    async def Width(self, instance, value):
        if self.acquirer:
            self.acquirer.configure({"Width": int(value)})
        self.ArraySize0.put(int(value))
        return int(value)

    @Height.putter
    async def Height(self, instance, value):
        if self.acquirer:
            self.acquirer.configure({"Height": int(value)})
        self.ArraySize1.put(int(value))
        return int(value)

    @Binning.putter
    async def Binning(self, instance, value):
        if self.acquirer:
            self.acquirer.configure({"Binning": int(value)})
        return int(value)

    @ImageMode.putter
    async def ImageMode(self, instance, value):
        if self.acquirer:
            self.acquirer.configure({"ImageMode": int(value)})
        return int(value)

    @NumImages.putter
    async def NumImages(self, instance, value):
        if self.acquirer:
            self.acquirer.configure({"NumImages": int(value)})
        return int(value)

    def start_acquisition(self) -> None:
        if self._acquire_thread is not None and self._acquire_thread.is_alive():
            return
        self._run_acquisition.set()
        self._acquire_thread = threading.Thread(target=self._acquisition_loop, daemon=True)
        self._acquire_thread.start()
        self.StatusMessage.put("Acquiring")

    def stop_acquisition(self) -> None:
        self._run_acquisition.clear()
        if self.acquirer:
            self.acquirer.stop_stream()
        self.StatusMessage.put("Idle")

    def _acquisition_loop(self) -> None:
        if self.acquirer:
            self.acquirer.start_stream()
            while self._run_acquisition.is_set():
                time.sleep(0.1)
            self.acquirer.stop_stream()
        else:
            # Simulation mode: generate dummy frames
            frame_interval = 1.0 / max(1.0, float(self.AcquisitionFrameRate.value))
            while self._run_acquisition.is_set():
                self._generate_simulation_frame()
                time.sleep(frame_interval)

    def _generate_simulation_frame(self) -> None:
        """Generate a dummy frame for simulation mode."""
        width = int(self.Width.value)
        height = int(self.Height.value)
        
        # Create a simple test pattern
        frame = np.zeros((height, width), dtype=np.uint16)
        # Add some pattern
        y, x = np.ogrid[:height, :width]
        frame[y % 20 < 10] = 1000
        frame[x % 20 < 10] = 2000
        # Add some noise
        frame += np.random.randint(0, 100, frame.shape, dtype=np.uint16)
        
        # Simulate the frame callback
        metadata = {
            "timestamp": time.time(),
            "frame_number": int(self.ImageCounter.value) + 1,
        }
        self._on_frame(frame, metadata)


def main():
    server = OrcaFireAreaDetector.pvdb(prefix="ORCA:")
    run(server, interfaces=["127.255.255.255"])


if __name__ == "__main__":
    main()
