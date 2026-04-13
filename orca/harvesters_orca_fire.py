import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Generator, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class OrcaFireAcquirer:
    """ORCA Fire acquisition using an Euresys GenTL producer.

    This class uses the Harvesters GenTL wrapper to acquire images from a
    CoaXPress camera attached through an Euresys frame grabber.
    """

    DEFAULT_GENTL_LIBRARY_CANDIDATES = [
        "/opt/euresys/egrabber/lib/x86_64/coaxlink.cti",
        "/opt/euresys/egrabber/lib/coaxlink.cti",
        "/opt/euresys/GenTL/Producer/x86_64/coaxlink.cti",
    ]

    def __init__(
        self,
        gen_tl_producer_path: Optional[str] = None,
        device_index: int = 0,
    ):
        try:
            from harvesters.core import Harvester
        except ImportError as exc:
            raise ImportError(
                "Harvesters is required. Install with `pip install harvesters` "
                "and ensure the Euresys GenTL producer is installed."
            ) from exc

        if gen_tl_producer_path is None:
            gen_tl_producer_path = self._find_default_gentl_producer()

        if not gen_tl_producer_path or not Path(gen_tl_producer_path).exists():
            raise FileNotFoundError(
                "Could not locate the Euresys GenTL producer. "
                "Please pass `gen_tl_producer_path` or install the Euresys GenTL runtime."
            )

        self._harvester = Harvester()
        self._harvester.add_file(gen_tl_producer_path)
        self._harvester.update()
        self._device_index = device_index
        self._image_acquirer = None
        self._device_info = None
        self._is_acquiring = False
        self._frame_callbacks: List[Callable[[np.ndarray], None]] = []
        self._stream_thread: Optional[threading.Thread] = None
        self._stream_event = threading.Event()
        self._frame_counter = 0
        self._trigger_mode = "Off"

    @classmethod
    def _find_default_gentl_producer(cls) -> Optional[str]:
        env_path = os.environ.get("EURESYS_GENTL_PRODUCER")
        if env_path:
            return env_path

        for candidate in cls.DEFAULT_GENTL_LIBRARY_CANDIDATES:
            if Path(candidate).exists():
                return candidate

        return None

    def list_devices(self) -> List[Dict[str, str]]:
        """Return a list of available GenTL devices."""
        devices = []
        for i, info in enumerate(self._harvester.device_info_list):
            devices.append(
                {
                    "index": str(i),
                    "vendor": info.vendor,  # type: ignore[attr-defined]
                    "model": info.model,  # type: ignore[attr-defined]
                    "serial_number": info.serial_number,  # type: ignore[attr-defined]
                }
            )
        return devices

    def open(self) -> None:
        """Open the configured camera device."""
        if self._image_acquirer is not None:
            return

        if self._device_index >= len(self._harvester.device_info_list):
            raise IndexError(
                f"Device index {self._device_index} is out of range. "
                f"Found {len(self._harvester.device_info_list)} devices."
            )

        self._image_acquirer = self._harvester.create(self._device_index)
        self._device_info = self._harvester.device_info_list[self._device_index]

        # Configure data stream to match the camera's actual dimensions.
        # The frame grabber may not auto-detect these when CXP link is in
        # "Detected" (vs "Connected") state.
        try:
            nm = self._image_acquirer.remote_device.node_map
            ds = self._image_acquirer.data_streams[0]
            dsnm = ds.node_map
            dsnm.ImageFormatSource.value = 'DataStream'
            dsnm.ImageFormatSelector.value = 'DataStream'
            dsnm.RemoteWidth.value = nm.Width.value
            dsnm.RemoteHeight.value = nm.Height.value
            dsnm.RemotePixelFormat.value = str(nm.PixelFormat.value)
            logger.info("Stream configured: %dx%d %s",
                        dsnm.Width.value, dsnm.Height.value,
                        dsnm.PixelFormat.value)
        except Exception as exc:
            logger.warning("Could not configure data stream: %s", exc)

    def close(self) -> None:
        """Close the camera and release resources."""
        if self._image_acquirer is not None:
            if self._is_acquiring:
                self.stop_acquisition()
            self._image_acquirer.destroy()
            self._image_acquirer = None
        self._harvester.reset()

    def configure(self, config: Dict[str, float]) -> None:
        """Configure camera parameters by GenICam node names.

        Example config keys: ExposureTime, Width, Height, OffsetX, OffsetY,
        AcquisitionFrameRate, TriggerMode, TriggerSource.
        """
        if self._image_acquirer is None:
            raise RuntimeError("Camera is not open. Call open() before configure().")

        node_map = self._image_acquirer.remote_device.node_map
        for node_name, value in config.items():
            self._set_node_value(node_map, node_name, value)

    def configure_stream(self, config: Dict[str, object]) -> None:
        """Configure frame grabber data stream parameters.

        Supported keys:
            BinningMethod: 'Disable', 'Sum_2x2', 'Mean_2x2', 'Sum_4x4', 'Mean_4x4'
        """
        if self._image_acquirer is None:
            raise RuntimeError("Camera is not open.")
        ds = self._image_acquirer.data_streams[0]
        dsnm = ds.node_map
        for name, value in config.items():
            try:
                node = getattr(dsnm, name)
                node.value = value
                logger.info("Stream %s = %s", name, value)
            except Exception as exc:
                logger.warning("Failed to set stream %s: %s", name, exc)

    # -- Frame grabber trigger control --

    def set_trigger_mode(self, mode: str) -> None:
        """Set trigger mode via frame grabber.

        Args:
            mode: 'Off' (free-run), 'On' (external trigger), 'Software'
        """
        if self._image_acquirer is None:
            raise RuntimeError("Camera is not open.")
        dnm = self._image_acquirer.device.node_map
        if mode == "Off":
            dnm.CameraControlMethod.value = "NC"
            self._trigger_mode = "Off"
        else:
            dnm.CameraControlMethod.value = "RG"
            if mode == "Software":
                dnm.CycleTriggerSource.value = "StartCycle"
            else:  # External
                dnm.CycleTriggerSource.value = "LIN1"
            self._trigger_mode = mode
        logger.info("Trigger mode: %s", mode)

    def configure_external_trigger(self, source: str = "TTLIO11",
                                   activation: str = "RisingEdge") -> None:
        """Configure external trigger input on the frame grabber.

        Args:
            source: I/O line name (TTLIO11, DIN11, IIN11, etc.)
            activation: 'RisingEdge', 'FallingEdge', 'Both'
        """
        if self._image_acquirer is None:
            raise RuntimeError("Camera is not open.")
        ifnm = self._image_acquirer.device.parent.node_map
        ifnm.LineInputToolSelector.value = "LIN1"
        ifnm.LineInputToolSource.value = source
        ifnm.LineInputToolActivation.value = activation
        logger.info("External trigger: %s %s → LIN1", source, activation)

    def software_trigger(self) -> None:
        """Fire a single software trigger."""
        if self._image_acquirer is None:
            raise RuntimeError("Camera is not open.")
        dnm = self._image_acquirer.device.node_map
        dnm.StartCycle.execute()

    def _set_node_value(self, node_map, name: str, value: float) -> None:
        if not hasattr(node_map, name):
            raise AttributeError(f"GenICam node '{name}' not available on the device.")

        node = getattr(node_map, name)
        try:
            node.value = value
        except Exception as exc:
            raise RuntimeError(f"Failed to set GenICam node '{name}' to {value}: {exc}") from exc

    def get_node_value(self, name: str):
        if self._image_acquirer is None:
            raise RuntimeError("Camera is not open. Call open() before get_node_value().")
        node_map = self._image_acquirer.remote_device.node_map
        if not hasattr(node_map, name):
            raise AttributeError(f"GenICam node '{name}' not available on the device.")
        return getattr(node_map, name).value

    def start_acquisition(self) -> None:
        """Start acquisition on the image acquirer."""
        if self._image_acquirer is None:
            raise RuntimeError("Camera is not open. Call open() before start_acquisition().")
        if self._is_acquiring:
            return

        self._image_acquirer.start()
        self._is_acquiring = True

    def register_frame_callback(self, callback: Callable[[np.ndarray, Dict], None]) -> None:
        """Register a callback that will receive each acquired frame and metadata."""
        self._frame_callbacks.append(callback)

    def unregister_frame_callback(self, callback: Callable[[np.ndarray, Dict], None]) -> None:
        """Remove a previously registered frame callback."""
        self._frame_callbacks.remove(callback)

    def start_stream(self, timeout_ms: int = 1000) -> None:
        """Start camera acquisition and stream frames to registered callbacks."""
        self.start_acquisition()
        if self._stream_thread is not None and self._stream_thread.is_alive():
            return
        self._stream_event.set()
        self._stream_thread = threading.Thread(target=self._stream_loop, args=(timeout_ms,), daemon=True)
        self._stream_thread.start()

    def stop_stream(self) -> None:
        """Stop streaming frames and stop acquisition."""
        self._stream_event.clear()
        if self._stream_thread is not None:
            self._stream_thread.join(timeout=2.0)
            self._stream_thread = None
        self.stop_acquisition()

    def _stream_loop(self, timeout_ms: int) -> None:
        while self._stream_event.is_set() and self._is_acquiring:
            try:
                frame = self.acquire_frame(timeout_ms=timeout_ms)
                self._frame_counter += 1
                metadata = {
                    "timestamp": time.time(),
                    "frame_number": self._frame_counter,
                }
            except Exception:
                time.sleep(0.01)
                continue
            for callback in self._frame_callbacks:
                callback(frame, metadata)

    def stop_acquisition(self) -> None:
        """Stop acquisition and release any pending buffers."""
        if self._image_acquirer is None or not self._is_acquiring:
            return
        self._image_acquirer.stop()
        self._is_acquiring = False

    def acquire_frame(self, timeout_ms: int = 1000) -> np.ndarray:
        """Fetch a single image frame from the camera."""
        if self._image_acquirer is None or not self._is_acquiring:
            raise RuntimeError("Acquisition is not running. Call start_acquisition() first.")

        with self._image_acquirer.fetch(timeout=timeout_ms) as buffer:
            component = buffer.payload.components[0]
            array = np.array(component.data, dtype=np.uint16, copy=True)
            height = component.height
            width = component.width
            if array.size != width * height:
                raise RuntimeError(
                    f"Unexpected payload size {array.size}, expected {width * height}"
                )
            return array.reshape((height, width))

    def acquire_frames(
        self,
        frame_count: int,
        timeout_ms: int = 1000,
    ) -> Generator[np.ndarray, None, None]:
        """Acquire a sequence of frames from the camera."""
        self.start_acquisition()
        try:
            for _ in range(frame_count):
                yield self.acquire_frame(timeout_ms=timeout_ms)
        finally:
            self.stop_acquisition()

    def save_frame(self, frame: np.ndarray, filename: str) -> None:
        """Save a single frame to disk as TIFF."""
        try:
            import imageio
        except ImportError as exc:
            raise ImportError(
                "imageio is required to save TIFF files. Install with `pip install imageio`."
            ) from exc

        imageio.imwrite(filename, frame.astype(np.uint16))

    def describe_device(self) -> Dict[str, str]:
        if self._device_info is None:
            raise RuntimeError("Device is not open.")
        return {
            "vendor": self._device_info.vendor,  # type: ignore[attr-defined]
            "model": self._device_info.model,  # type: ignore[attr-defined]
            "serial_number": self._device_info.serial_number,  # type: ignore[attr-defined]
            "id": self._device_info.id,  # type: ignore[attr-defined]
        }


def example():
    acquirer = OrcaFireAcquirer()
    print("Available devices:")
    for idx, device in enumerate(acquirer.list_devices()):
        print(f"  {idx}: {device}")

    acquirer.open()
    print("Opened device:", acquirer.describe_device())

    config = {
        "ExposureTime": 1000.0,
        "Width": 4432,
        "Height": 2368,
        "OffsetX": 0,
        "OffsetY": 0,
        "AcquisitionFrameRate": 30.0,
        "TriggerMode": 0,
        "TriggerSource": 0,
    }
    acquirer.configure(config)

    for i, frame in enumerate(acquirer.acquire_frames(frame_count=5, timeout_ms=2000)):
        filename = f"orca_fire_frame_{i:03d}.tiff"
        acquirer.save_frame(frame, filename)
        print(f"Saved frame {i} -> {filename}")

    acquirer.close()


if __name__ == "__main__":
    example()
