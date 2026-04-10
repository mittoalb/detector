import threading
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

try:
    import tkinter as tk
    from tkinter import ttk
    from tkinter import messagebox
    from tkinter.scrolledtext import ScrolledText
except ImportError:
    tk = None


class DummyOrcaFireDetector:
    """A software-only detector that simulates an ORCA Fire camera."""

    DEFAULT_PARAMETERS = {
        # Basic AreaDetector and Hamamatsu-like imaging parameters
        "ExposureTime": 1000.0,
        "AcquireTime": 1000.0,
        "AcquirePeriod": 33.3,
        "AcquisitionFrameRate": 30.0,
        "Gain": 1.0,
        "Gamma": 1.0,
        "PixelType": "Mono16",
        "NDDataType": 3,
        "BufferCount": 10,

        # Size and ROI
        "Width": 4432,
        "Height": 2369,
        "MinX": 0,
        "MinY": 0,
        "SizeX": 4432,
        "SizeY": 2369,
        "MaxSizeX": 4432,
        "MaxSizeY": 2369,
        "OffsetX": 0,
        "OffsetY": 0,
        "Binning": 1,

        # Trigger and acquisition mode
        "TriggerMode": 0,  # 0=internal, 1=external, 2=software
        "TriggerSource": 0,
        "TriggerActive": 0,
        "TriggerPolarity": 0,
        "TriggerDelay": 0.0,
        "TriggerTimes": 1,
        "ImageMode": 0,  # 0=Continuous, 1=Single, 2=Multiple
        "NumImages": 10,
        "Acquire": 0,

        # Status / diagnostics
        "Status": 0,
        "StatusMessage": "Idle",
        "SensorTemperature": 20.0,
        "CoolerStatus": "Unknown",
        "ReadoutSpeed": 2,
    }

    def __init__(self):
        self._parameters: Dict[str, Any] = dict(self.DEFAULT_PARAMETERS)
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._frame_callbacks: List[Callable[[np.ndarray, Dict[str, Any]], None]] = []
        self._param_callbacks: List[Callable[[str, Any], None]] = []
        self._last_frame: Optional[np.ndarray] = None

    def list_parameters(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._parameters)

    def get_parameter(self, name: str) -> Any:
        with self._lock:
            if name not in self._parameters:
                raise KeyError(f"Unknown parameter: {name}")
            return self._parameters[name]

    def set_parameter(self, name: str, value: Any) -> None:
        with self._lock:
            if name not in self._parameters:
                raise KeyError(f"Unknown parameter: {name}")
            self._parameters[name] = value
        self._notify_parameter_change(name, value)

    pv_get = get_parameter
    pv_set = set_parameter

    def register_frame_callback(self, callback: Callable[[np.ndarray, Dict[str, Any]], None]) -> None:
        self._frame_callbacks.append(callback)

    def register_parameter_callback(self, callback: Callable[[str, Any], None]) -> None:
        self._param_callbacks.append(callback)

    def start_acquisition(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._parameters["Acquire"] = 1
            self._notify_parameter_change("Acquire", 1)
            self._thread = threading.Thread(target=self._run_acquisition, daemon=True)
            self._thread.start()

    def stop_acquisition(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._parameters["Acquire"] = 0
            self._notify_parameter_change("Acquire", 0)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def acquire_frame(self) -> np.ndarray:
        frame = self._generate_image()
        self._last_frame = frame
        return frame

    def acquire_frames(self, count: int) -> List[np.ndarray]:
        return [self.acquire_frame() for _ in range(count)]

    def _run_acquisition(self) -> None:
        frame_rate = float(self.get_parameter("AcquisitionFrameRate"))
        interval = 1.0 / max(frame_rate, 1.0)
        num_images = int(self.get_parameter("NumImages"))
        mode = int(self.get_parameter("ImageMode"))
        image_counter = 0

        while self._running:
            if mode == 1 and image_counter >= 1:
                break
            if mode == 2 and image_counter >= num_images:
                break
            frame = self.acquire_frame()
            image_counter += 1
            self._dispatch_frame(frame)
            time.sleep(interval)

        self.stop_acquisition()

    def _generate_image(self) -> np.ndarray:
        width = int(self.get_parameter("SizeX") / max(self.get_parameter("Binning"), 1))
        height = int(self.get_parameter("SizeY") / max(self.get_parameter("Binning"), 1))
        width = max(1, width)
        height = max(1, height)
        x = np.linspace(0, 65535, width, dtype=np.uint32)
        y = np.linspace(0, 65535, height, dtype=np.uint32)
        image = np.outer(y, np.ones_like(x, dtype=np.uint32)) // 256
        offset = np.uint32(int(time.time() * 10) % 65536)
        image = ((image + offset) % 65536).astype(np.uint16)
        return image

    def _dispatch_frame(self, frame: np.ndarray) -> None:
        metadata = self.list_parameters()
        self._last_frame = frame
        for callback in self._frame_callbacks:
            callback(frame, metadata)

    def _notify_parameter_change(self, name: str, value: Any) -> None:
        for callback in self._param_callbacks:
            callback(name, value)

    def get_last_frame(self) -> Optional[np.ndarray]:
        return self._last_frame


class DummyDetectorGui:
    """A simple GUI for the dummy detector."""

    def __init__(self, detector: DummyOrcaFireDetector):
        if tk is None:
            raise ImportError("Tkinter is required for the GUI but is not available.")

        self.detector = detector
        self.root = tk.Tk()
        self.root.title("Dummy ORCA Fire Detector")
        self._build_ui()
        self.detector.register_parameter_callback(self._on_parameter_changed)
        self.detector.register_frame_callback(self._on_frame_ready)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding="12")
        frame.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.controls: Dict[str, tk.StringVar] = {}
        parameters = [
            "ExposureTime",
            "AcquireTime",
            "AcquisitionFrameRate",
            "Gain",
            "Gamma",
            "PixelType",
            "Width",
            "Height",
            "MinX",
            "MinY",
            "SizeX",
            "SizeY",
            "Binning",
            "TriggerMode",
            "TriggerSource",
            "TriggerActive",
            "TriggerPolarity",
            "TriggerDelay",
            "TriggerTimes",
            "ImageMode",
            "NumImages",
            "ReadoutSpeed",
            "StatusMessage",
        ]

        for index, key in enumerate(parameters):
            label = ttk.Label(frame, text=key)
            label.grid(row=index, column=0, sticky="w", pady=2)
            value = str(self.detector.get_parameter(key))
            var = tk.StringVar(value=value)
            entry = ttk.Entry(frame, textvariable=var, width=18)
            entry.grid(row=index, column=1, sticky="ew", pady=2)
            self.controls[key] = var

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(parameters), column=0, columnspan=2, pady=10)
        ttk.Button(buttons, text="Apply Parameters", command=self._apply_parameters).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Start Acquisition", command=self.detector.start_acquisition).grid(row=0, column=1, padx=4)
        ttk.Button(buttons, text="Stop Acquisition", command=self.detector.stop_acquisition).grid(row=0, column=2, padx=4)
        ttk.Button(buttons, text="Single Frame", command=self._single_frame).grid(row=0, column=3, padx=4)
        ttk.Button(buttons, text="Show All Parameters", command=self._show_all_parameters).grid(row=0, column=4, padx=4)

        self.status_text = ScrolledText(frame, height=12, width=64, state="disabled")
        self.status_text.grid(row=len(parameters) + 1, column=0, columnspan=2, pady=8, sticky="nsew")
        frame.rowconfigure(len(parameters) + 1, weight=1)

    def _apply_parameters(self) -> None:
        for key, var in self.controls.items():
            text = var.get().strip()
            if text == "":
                continue
            try:
                current_value = self.detector.get_parameter(key)
                if isinstance(current_value, int):
                    value = int(text)
                elif isinstance(current_value, float):
                    value = float(text)
                else:
                    value = text
                self.detector.set_parameter(key, value)
            except Exception as exc:
                messagebox.showerror("Invalid parameter", f"{key}: {exc}")
                return

    def _single_frame(self) -> None:
        frame = self.detector.acquire_frame()
        self._log(f"Single frame acquired: {frame.shape}")

    def _on_parameter_changed(self, name: str, value: Any) -> None:
        self._log(f"Parameter changed: {name} = {value}")

    def _on_frame_ready(self, frame: np.ndarray, metadata: Dict[str, Any]) -> None:
        self._log(f"Frame ready: {frame.shape}, Exposure={metadata['ExposureTime']}, Mode={metadata['ImageMode']}" )

    def _show_all_parameters(self) -> None:
        param_window = tk.Toplevel(self.root)
        param_window.title("All Detector Parameters")
        scrolled = ScrolledText(param_window, width=80, height=30, state="normal")
        scrolled.pack(fill="both", expand=True)
        parameters = self.detector.list_parameters()
        for name, value in sorted(parameters.items()):
            scrolled.insert("end", f"{name}: {value}\n")
        scrolled.configure(state="disabled")

    def _log(self, message: str) -> None:
        self.status_text.configure(state="normal")
        self.status_text.insert("end", f"{time.strftime('%H:%M:%S')} - {message}\n")
        self.status_text.see("end")
        self.status_text.configure(state="disabled")

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    detector = DummyOrcaFireDetector()
    gui = DummyDetectorGui(detector)
    gui.run()


if __name__ == "__main__":
    main()
