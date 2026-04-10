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
    """A modern GUI for the dummy detector, pystream-style interface."""

    def __init__(self, detector: DummyOrcaFireDetector):
        if tk is None:
            raise ImportError("Tkinter is required for the GUI but is not available.")

        self.detector = detector
        self.root = tk.Tk()
        self.root.title("ORCA Fire Detector Control - pystream Style")
        self.root.geometry("1200x800")
        self.root.configure(bg='#f0f0f0')

        # Set up modern styling
        self._setup_styles()

        self._build_ui()
        self.detector.register_parameter_callback(self._on_parameter_changed)
        self.detector.register_frame_callback(self._on_frame_ready)

        # Initialize image display
        self._update_image_display()

    def _setup_styles(self) -> None:
        """Set up modern tkinter styling."""
        style = ttk.Style()
        style.configure('TNotebook', background='#f0f0f0')
        style.configure('TNotebook.Tab', background='#e0e0e0', padding=[10, 5])
        style.configure('TFrame', background='#f0f0f0')
        style.configure('TLabel', background='#f0f0f0', font=('Arial', 9))
        style.configure('TButton', font=('Arial', 9, 'bold'), padding=[8, 4])
        style.configure('Header.TLabel', font=('Arial', 12, 'bold'), foreground='#2e5c8a')

    def _build_ui(self) -> None:
        """Build the modern tabbed interface."""
        # Main container
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Create notebook (tabs)
        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Create tabs
        self._create_live_tab()
        self._create_parameters_tab()
        self._create_status_tab()

        # Bottom control bar
        self._create_control_bar(main_frame)

    def _create_live_tab(self) -> None:
        """Create the live image display tab."""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="📹 Live View")

        # Image display area
        image_frame = ttk.LabelFrame(frame, text="Image Display", padding=10)
        image_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Canvas for image display
        self.image_canvas = tk.Canvas(image_frame, bg='black', width=800, height=400)
        self.image_canvas.pack(fill=tk.BOTH, expand=True)

        # Image info labels
        info_frame = ttk.Frame(image_frame)
        info_frame.pack(fill=tk.X, pady=(5, 0))

        self.image_info = ttk.Label(info_frame, text="No image acquired yet")
        self.image_info.pack(side=tk.LEFT)

        self.frame_counter = ttk.Label(info_frame, text="Frames: 0")
        self.frame_counter.pack(side=tk.RIGHT)

    def _create_parameters_tab(self) -> None:
        """Create the parameters control tab."""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="⚙️ Parameters")

        # Create scrollable frame
        canvas = tk.Canvas(frame, bg='#f0f0f0')
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # Parameter groups
        self._create_parameter_group(scrollable_frame, "Timing", [
            ("ExposureTime", "Exposure Time (μs)"),
            ("AcquireTime", "Acquire Time (μs)"),
            ("AcquisitionFrameRate", "Frame Rate (Hz)"),
            ("AcquirePeriod", "Acquire Period (ms)"),
        ])

        self._create_parameter_group(scrollable_frame, "Image Size & ROI", [
            ("Width", "Sensor Width"),
            ("Height", "Sensor Height"),
            ("SizeX", "ROI Width"),
            ("SizeY", "ROI Height"),
            ("OffsetX", "X Offset"),
            ("OffsetY", "Y Offset"),
            ("Binning", "Binning Factor"),
        ])

        self._create_parameter_group(scrollable_frame, "Acquisition Control", [
            ("ImageMode", "Mode (0=Cont, 1=Single, 2=Multi)"),
            ("NumImages", "Number of Images"),
            ("TriggerMode", "Trigger Mode"),
            ("TriggerSource", "Trigger Source"),
        ])

        self._create_parameter_group(scrollable_frame, "Image Processing", [
            ("Gain", "Gain"),
            ("Gamma", "Gamma"),
            ("ReadoutSpeed", "Readout Speed"),
        ])

        # Pack scrollable components
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _create_parameter_group(self, parent: ttk.Frame, title: str, params: List[tuple[str, str]]) -> None:
        """Create a group of related parameters."""
        group = ttk.LabelFrame(parent, text=title, padding=10)
        group.pack(fill=tk.X, padx=5, pady=5)

        self.controls: Dict[str, tk.StringVar] = getattr(self, 'controls', {})

        for param_name, display_name in params:
            row_frame = ttk.Frame(group)
            row_frame.pack(fill=tk.X, pady=2)

            label = ttk.Label(row_frame, text=f"{display_name}:", width=20, anchor='w')
            label.pack(side=tk.LEFT)

            current_value = self.detector.get_parameter(param_name)
            var = tk.StringVar(value=str(current_value))

            entry = ttk.Entry(row_frame, textvariable=var, width=15)
            entry.pack(side=tk.LEFT, padx=(0, 10))

            # Add unit labels for some parameters
            if 'Time' in param_name or 'Period' in param_name:
                ttk.Label(row_frame, text="μs", foreground='#666').pack(side=tk.LEFT)
            elif 'Rate' in param_name:
                ttk.Label(row_frame, text="Hz", foreground='#666').pack(side=tk.LEFT)

            self.controls[param_name] = var

    def _create_status_tab(self) -> None:
        """Create the status and logging tab."""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="📊 Status")

        # Status display
        status_frame = ttk.LabelFrame(frame, text="Detector Status", padding=10)
        status_frame.pack(fill=tk.X, padx=5, pady=5)

        self.status_vars = {}
        status_params = [
            ("StatusMessage", "Status"),
            ("Acquire", "Acquiring"),
            ("SensorTemperature", "Temperature (°C)"),
            ("CoolerStatus", "Cooler Status"),
        ]

        for param, display in status_params:
            row_frame = ttk.Frame(status_frame)
            row_frame.pack(fill=tk.X, pady=2)

            ttk.Label(row_frame, text=f"{display}:", width=15, anchor='w').pack(side=tk.LEFT)
            var = tk.StringVar(value=str(self.detector.get_parameter(param)))
            ttk.Label(row_frame, textvariable=var, width=20, anchor='w').pack(side=tk.LEFT)
            self.status_vars[param] = var

        # Log display
        log_frame = ttk.LabelFrame(frame, text="Activity Log", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.status_text = ScrolledText(log_frame, height=20, font=('Consolas', 9))
        self.status_text.pack(fill=tk.BOTH, expand=True)

    def _create_control_bar(self, parent: ttk.Frame) -> None:
        """Create the bottom control bar."""
        control_frame = ttk.Frame(parent)
        control_frame.pack(fill=tk.X, pady=(10, 0))

        # Left side - main controls
        left_frame = ttk.Frame(control_frame)
        left_frame.pack(side=tk.LEFT)

        ttk.Button(left_frame, text="▶️ Start", command=self.detector.start_acquisition).pack(side=tk.LEFT, padx=2)
        ttk.Button(left_frame, text="⏹️ Stop", command=self.detector.stop_acquisition).pack(side=tk.LEFT, padx=2)
        ttk.Button(left_frame, text="📷 Single Frame", command=self._single_frame).pack(side=tk.LEFT, padx=2)

        # Right side - utility controls
        right_frame = ttk.Frame(control_frame)
        right_frame.pack(side=tk.RIGHT)

        ttk.Button(right_frame, text="💾 Save Frame", command=self._save_frame).pack(side=tk.LEFT, padx=2)
        ttk.Button(right_frame, text="🔄 Apply Changes", command=self._apply_parameters).pack(side=tk.LEFT, padx=2)
        ttk.Button(right_frame, text="📋 All Parameters", command=self._show_all_parameters).pack(side=tk.LEFT, padx=2)

    def _apply_parameters(self) -> None:
        """Apply parameter changes from the GUI."""
        for key, var in self.controls.items():
            text = var.get().strip()
            if text == "":
                continue
            try:
                current_value = self.detector.get_parameter(key)
                if isinstance(current_value, int):
                    value = int(float(text))  # Allow decimal input for ints
                elif isinstance(current_value, float):
                    value = float(text)
                else:
                    value = text
                self.detector.set_parameter(key, value)
                self._log(f"✓ {key} = {value}")
            except Exception as exc:
                self._log(f"✗ {key}: {exc}")
                messagebox.showerror("Invalid parameter", f"{key}: {exc}")
                return

        self._log("Parameters applied successfully")

    def _single_frame(self) -> None:
        """Acquire a single frame."""
        try:
            frame = self.detector.acquire_frame()
            self._update_image_display()
            self._log(f"📷 Single frame acquired: {frame.shape}")
        except Exception as e:
            self._log(f"✗ Single frame failed: {e}")

    def _save_frame(self) -> None:
        """Save the last frame to a file."""
        frame = self.detector.get_last_frame()
        if frame is None:
            messagebox.showwarning("No Frame", "No frame available to save")
            return

        try:
            filename = f"frame_{int(time.time())}.tiff"
            # Simple TIFF saving (you might want to use imageio in real implementation)
            import imageio
            imageio.imwrite(filename, frame.astype(np.uint16))
            self._log(f"💾 Frame saved as {filename}")
        except Exception as e:
            self._log(f"✗ Save failed: {e}")

    def _on_parameter_changed(self, name: str, value: Any) -> None:
        """Handle parameter changes."""
        # Update status display if it's a status parameter
        if name in self.status_vars:
            self.status_vars[name].set(str(value))

        # Update control values
        if name in self.controls:
            self.controls[name].set(str(value))

        self._log(f"🔄 {name} = {value}")

    def _on_frame_ready(self, frame: np.ndarray, metadata: Dict[str, Any]) -> None:
        """Handle new frame arrival."""
        self._update_image_display()
        self.frame_counter.config(text=f"Frames: {metadata.get('frame_count', 0)}")
        self._log(f"📹 Frame: {frame.shape}, Exposure: {metadata['ExposureTime']}μs")

    def _update_image_display(self) -> None:
        """Update the image display with the latest frame."""
        frame = self.detector.get_last_frame()
        if frame is None:
            self.image_canvas.delete("all")
            self.image_canvas.create_text(400, 200, text="No image acquired yet",
                                        fill='white', font=('Arial', 14))
            self.image_info.config(text="No image acquired yet")
            return

        # Create a simple preview (downsample for display)
        height, width = frame.shape
        max_display_size = 400

        if width > max_display_size or height > max_display_size:
            scale = min(max_display_size / width, max_display_size / height)
            new_width = int(width * scale)
            new_height = int(height * scale)

            # Simple downsampling (you might want better interpolation)
            preview = frame[::max(1, height // new_height), ::max(1, width // new_width)]
        else:
            preview = frame

        # Normalize to 0-255 for display
        preview_normalized = ((preview.astype(np.float32) - preview.min()) /
                            (preview.max() - preview.min()) * 255).astype(np.uint8)

        # Create PIL-like image for tkinter (simplified)
        # This is a basic implementation - you might want to use PIL/Pillow
        self.image_canvas.delete("all")

        # Create a simple grayscale representation
        for y in range(0, preview_normalized.shape[0], max(1, preview_normalized.shape[0] // 200)):
            for x in range(0, preview_normalized.shape[1], max(1, preview_normalized.shape[1] // 200)):
                intensity = preview_normalized[y, x]
                color = f'#{intensity:02x}{intensity:02x}{intensity:02x}'
                self.image_canvas.create_rectangle(
                    x * 4, y * 4, (x + 1) * 4, (y + 1) * 4,
                    fill=color, outline='')

        self.image_info.config(text=f"Image: {width}×{height}, Range: {frame.min()}-{frame.max()}")

    def _show_all_parameters(self) -> None:
        """Show all parameters in a separate window."""
        param_window = tk.Toplevel(self.root)
        param_window.title("All Detector Parameters")
        param_window.geometry("600x400")

        scrolled = ScrolledText(param_window, font=('Consolas', 9))
        scrolled.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        parameters = self.detector.list_parameters()
        for name, value in sorted(parameters.items()):
            scrolled.insert("end", f"{name:.<25} {value}\n")

        scrolled.configure(state="disabled")

    def _log(self, message: str) -> None:
        """Add a message to the log."""
        timestamp = time.strftime('%H:%M:%S')
        self.status_text.insert("end", f"[{timestamp}] {message}\n")
        self.status_text.see("end")

    def run(self) -> None:
        """Start the GUI main loop."""
        self.root.mainloop()


def main() -> None:
    detector = DummyOrcaFireDetector()
    gui = DummyDetectorGui(detector)
    gui.run()


if __name__ == "__main__":
    main()
