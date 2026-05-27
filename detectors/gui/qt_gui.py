"""
Camera-agnostic Qt GUI with tabs and dark theme.

Builds parameter controls dynamically from the camera's `list_params()`,
arranged into logical tabs. Works with any BaseCamera backend.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from PyQt5 import QtCore, QtWidgets
    from PyQt5.QtCore import pyqtSignal as Signal
except ImportError:
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtCore import Signal

from detectors.core.base import BaseCamera

logger = logging.getLogger(__name__)


# Logical tab groupings. Parameters not appearing in any group go into "Other".
PARAM_TABS: List[Tuple[str, List[Tuple[str, str]]]] = [
    ("Exposure & Acquisition", [
        ("ExposureTime", "Exposure Time (s)"),
        ("AcquisitionFrameRate", "Frame Rate (Hz)"),
        ("AcquisitionFrameCount", "Frame Count"),
        ("AcquisitionMode", "Acquisition Mode"),
    ]),
    ("Image Format & ROI", [
        ("Width", "Width (px)"),
        ("Height", "Height (px)"),
        ("Binning", "Binning (digital)"),
        ("SubarrayMode", "Subarray Mode"),
        ("SubarrayHPos", "Subarray H Pos"),
        ("SubarrayHSize", "Subarray H Size"),
        ("SubarrayVPos", "Subarray V Pos"),
        ("SubarrayVSize", "Subarray V Size"),
        ("BitsPerChannel", "Bits Per Channel"),
        ("PixelFormat", "Pixel Format"),
    ]),
    ("Readout & Sensor", [
        ("SensorMode", "Sensor Mode"),
        ("ReadoutSpeed", "Readout Speed"),
        ("ReadoutDirection", "Readout Direction"),
        ("ReadoutUnit", "Readout Unit"),
        ("ShutterMode", "Shutter Mode"),
        ("MechanicalShutter", "Mechanical Shutter"),
        ("Sensitivity", "Sensitivity"),
        ("LightMode", "Light Mode"),
    ]),
    ("Trigger Input", [
        ("TriggerMode", "Trigger Mode"),
        ("TriggerSource", "Trigger Source"),
        ("TriggerActive", "Trigger Active"),
        ("TriggerPolarity", "Trigger Polarity"),
        ("TriggerConnector", "Trigger Connector"),
        ("TriggerDelay", "Trigger Delay (s)"),
        ("TriggerTimes", "Trigger Count"),
        ("TriggerFirstExposure", "First Exposure"),
        ("TriggerGlobalExposure", "Global Exposure"),
        ("InternalTriggerHandling", "Internal Trigger Handling"),
        ("TriggerEnableActive", "Trigger Enable Active"),
        ("TriggerEnablePolarity", "Trigger Enable Polarity"),
    ]),
    ("Cooling", [
        ("SensorTemperature", "Temperature (°C)"),
        ("SensorCooler", "Cooler"),
        ("SensorTemperatureTarget", "Target Temp (°C)"),
        ("SensorCoolerStatus", "Cooler Status"),
        ("SensorCoolerFan", "Cooler Fan"),
        ("SensorTemperatureMin", "Temp Min (°C)"),
        ("SensorTemperatureMax", "Temp Max (°C)"),
        ("SensorTemperatureStatus", "Temp Status"),
    ]),
    ("Output Trigger", [
        ("OutputTriggerSource", "Output Source"),
        ("OutputTriggerKind", "Output Kind"),
        ("OutputTriggerPolarity", "Output Polarity"),
        ("OutputTriggerActive", "Output Active"),
        ("OutputTriggerDelay", "Output Delay (s)"),
        ("OutputTriggerPeriod", "Output Period (s)"),
        ("OutputTriggerBaseSensor", "Base Sensor"),
    ]),
    ("Master Pulse", [
        ("MasterPulseMode", "Mode"),
        ("MasterPulseInterval", "Interval (s)"),
        ("MasterPulseBurstTimes", "Burst Times"),
    ]),
    ("Image Processing", [
        ("DefectCorrectMode", "Defect Correction"),
        ("HotPixelCorrectLevel", "Hot Pixel Level"),
        ("ContrastGain", "Contrast Gain"),
        ("ContrastOffset", "Contrast Offset"),
        ("HighDynamicRangeMode", "HDR Mode"),
        ("RecursiveFilter", "Recursive Filter"),
        ("RecursiveFilterFrames", "Recursive Frames"),
        ("SpotNoiseReducer", "Spot Noise Reducer"),
        ("SensorGapCorrectMode", "Gap Correction"),
        ("FrameAveragingMode", "Frame Averaging Mode"),
        ("FrameAveragingFrames", "Frame Averaging Frames"),
        ("InterFrameAluEnable", "Inter-Frame ALU"),
        ("OutputIntensity", "Output Intensity"),
        ("DigitalBinningMethod", "Digital Binning Method"),
        ("TestPatternKind", "Test Pattern"),
    ]),
    ("Timing Info", [
        ("TimingReadoutTime", "Readout Time (s)"),
        ("TimingExposure", "Exposure Status"),
        ("InternalFrameRate", "Internal Frame Rate (Hz)"),
        ("InternalFrameInterval", "Frame Interval (s)"),
        ("InternalLineRate", "Line Rate (Hz)"),
        ("InternalLineInterval", "Line Interval (s)"),
    ]),
]

# Enum value choices for combo boxes
ENUM_CHOICES: Dict[str, list] = {
    "ImageMode": ["Continuous", "Single", "Multiple"],
    "PixelFormat": ["Mono8", "Mono12", "Mono16"],
    "SubarrayMode": ["OFF", "ON"],
    "SensorMode": ["Area", "Lightsheet", "SplitView", "DualLightsheet",
                    "PartialArea"],
    "ReadoutSpeed": ["Fastest", "Slowest"],
    "ReadoutDirection": ["Forward", "Backward", "Bidirectional", "Reverse",
                          "Diverge", "ReverseBidirectional"],
    "ShutterMode": ["Rolling", "Global"],
    "MechanicalShutter": ["Auto", "Open", "Close"],
    "TriggerMode": ["Off", "On"],
    "TriggerSource": ["Internal", "External", "Software", "MasterPulse"],
    "TriggerActive": ["Edge", "Level", "SyncReadout", "Point"],
    "TriggerPolarity": ["Positive", "Negative"],
    "TriggerConnector": ["Interface", "BNC", "Multi"],
    "TriggerFirstExposure": ["NewFrame", "Current"],
    "TriggerGlobalExposure": ["None", "AlwaysOpen", "DelayedReadout",
                                "Emulate", "GlobalReset"],
    "OutputTriggerKind": ["Low", "ExposureTiming", "ProgramAble",
                           "TriggerReady", "High", "AnyRowExposureTiming",
                           "ReadoutEnd"],
    "OutputTriggerPolarity": ["Positive", "Negative"],
    "OutputTriggerActive": ["Edge", "Level"],
    "SensorCooler": ["Off", "On", "Max"],
    "SensorCoolerFan": ["Off", "On"],
    "DefectCorrectMode": ["Off", "On"],
    "HotPixelCorrectLevel": ["Standard", "Minimum", "Aggressive"],
    "RecursiveFilter": ["Off", "On"],
    "SpotNoiseReducer": ["Off", "On"],
    "HighDynamicRangeMode": ["Off", "On"],
    "MasterPulseMode": ["Continuous", "Start", "Burst"],
    "FrameAveragingMode": ["Off", "On"],
    "InterFrameAluEnable": ["Off", "On"],
    "Binning": [1, 2, 4],
}

# Parameters that should never be editable in the GUI
READ_ONLY_PARAMS = {
    "Width", "Height", "PixelFormat", "BitsPerChannel",
    "SensorTemperature", "SensorCoolerStatus",
    "SensorTemperatureMin", "SensorTemperatureMax", "SensorTemperatureStatus",
    "AcquisitionFrameRate", "InternalFrameRate", "InternalFrameInterval",
    "InternalLineRate", "InternalLineInterval",
    "TimingReadoutTime", "TimingExposure", "TimingCyclicTriggerPeriod",
    "ReadoutUnit", "ImageRowBytes", "ImageFrameBytes",
}

DARK_STYLESHEET = """
    QMainWindow, QWidget, QDialog { background-color: #202124; color: #f1f3f4; }
    QLineEdit { background-color: #212325; color: #f1f3f4; border: 1px solid #3c4043; padding: 4px; }
    QComboBox { background-color: #212325; color: #f1f3f4; border: 1px solid #3c4043; padding: 4px; }
    QComboBox::drop-down { border: none; }
    QComboBox::down-arrow { image: none; border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 6px solid #9aa0a6; margin-right: 6px; }
    QComboBox QAbstractItemView { background-color: #2a2d32; color: #f1f3f4; selection-background-color: #3c4043; border: 1px solid #5f6368; }
    QPushButton { background-color: #3c4043; color: #f1f3f4; border: 1px solid #5f6368; padding: 6px 12px; border-radius: 3px; }
    QPushButton:hover { background-color: #5f6368; }
    QPushButton:pressed { background-color: #3c4043; }
    QLabel { color: #f1f3f4; }
    QGroupBox { color: #8ab4f8; border: 1px solid #3c4043; border-radius: 4px; padding-top: 8px; margin-top: 6px; }
    QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
    QTabWidget, QTabBar { background-color: #202124; color: #f1f3f4; }
    QTabWidget::pane { border: 1px solid #3c4043; }
    QTabBar::tab { background-color: #2a2d32; color: #f1f3f4; padding: 6px 12px; }
    QTabBar::tab:selected { background-color: #3c4043; }
    QPlainTextEdit { background-color: #111315; color: #e8eaed; border: 1px solid #3c4043; }
    QScrollArea { border: none; background-color: #202124; }
    QScrollBar { background-color: #2a2d32; }
    QScrollBar:vertical { width: 12px; }
    QScrollBar::handle { background-color: #5f6368; border-radius: 6px; }
    QScrollBar::handle:hover { background-color: #9aa0a6; }
"""


class DetectorGui(QtWidgets.QMainWindow):
    """Camera-agnostic detector control GUI with tabs and dark theme."""

    parameter_changed = Signal(str, object)
    frame_ready = Signal(object, dict)

    def __init__(self, ioc, parent=None):
        super().__init__(parent)
        self.ioc = ioc
        self.camera: Optional[BaseCamera] = ioc.camera
        info = self.camera.get_info() if self.camera else None

        title = (f"{info.vendor} {info.model}"
                 if info else "Detector Control")
        self.setWindowTitle(f"Detector Control — {title}")
        self.resize(1200, 720)
        self.setStyleSheet(DARK_STYLESHEET)

        self.controls: Dict[str, Any] = {}
        self.status_labels: Dict[str, QtWidgets.QLabel] = {}
        self.last_frame = None

        # Measured fps state
        self._fps_count = 0
        self._fps_last_t = 0.0
        self._measured_fps = 0.0

        self._build_ui()
        self._populate_values()

        # Refresh read-only values periodically (e.g. SensorTemperature)
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_readonly)
        self._refresh_timer.start(1000)

        # Wire frame callback to update status
        self.ioc.cam1.register_frame_callback(self._on_frame)

    # ---------- UI construction ----------

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Toolbar
        toolbar = QtWidgets.QHBoxLayout()
        layout.addLayout(toolbar)

        title = QtWidgets.QLabel("Detector Control")
        title.setStyleSheet("font-size: 14pt; font-weight: bold;")
        toolbar.addWidget(title)
        toolbar.addStretch(1)

        self.start_btn = QtWidgets.QPushButton("Start")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.apply_btn = QtWidgets.QPushButton("Apply Changes")
        self.single_btn = QtWidgets.QPushButton("Single Frame")
        self.trigger_btn = QtWidgets.QPushButton("Software Trigger")
        for btn in [self.start_btn, self.stop_btn, self.apply_btn,
                    self.single_btn, self.trigger_btn]:
            btn.setMinimumWidth(120)
            toolbar.addWidget(btn)
        self.start_btn.clicked.connect(self._start_acquisition)
        self.stop_btn.clicked.connect(self._stop_acquisition)
        self.apply_btn.clicked.connect(self._apply_all)
        self.single_btn.clicked.connect(self._single_frame)
        self.trigger_btn.clicked.connect(self._software_trigger)

        # Body: tabs on left, status panel on right
        body = QtWidgets.QHBoxLayout()
        layout.addLayout(body, 1)

        # Left: parameter tabs
        self.param_tabs = QtWidgets.QTabWidget()
        body.addWidget(self.param_tabs, 2)
        self._create_tabs()

        # Right: status, actions, log
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        body.addWidget(right, 1)

        # Status panel
        status_box = QtWidgets.QGroupBox("Status")
        status_form = QtWidgets.QFormLayout(status_box)
        status_form.setLabelAlignment(QtCore.Qt.AlignRight)

        # Camera connection indicator
        cam_row = QtWidgets.QHBoxLayout()
        self.cam_indicator = QtWidgets.QLabel("●")
        connected = self.camera is not None and self.camera.is_open()
        color = "#81c995" if connected else "#ff6b6b"
        self.cam_indicator.setStyleSheet(f"color: {color}; font-size: 16px;")
        cam_text = "Connected" if connected else "Disconnected"
        if connected:
            info = self.camera.get_info()
            cam_text = f"{info.vendor} {info.model}"
        self.cam_label = QtWidgets.QLabel(cam_text)
        cam_row.addWidget(self.cam_indicator)
        cam_row.addWidget(self.cam_label)
        cam_row.addStretch()
        status_form.addRow("Camera:", cam_row)

        # Live status fields
        for name, label in [
            ("FrameCount", "Frames"),
            ("AcquisitionFrameRate", "FPS"),
            ("SensorTemperature", "Temp (°C)"),
            ("SensorCoolerStatus", "Cooler"),
        ]:
            lbl = QtWidgets.QLabel("-")
            status_form.addRow(label + ":", lbl)
            self.status_labels[name] = lbl

        # PVA stream PV (read-only display)
        self.pva_edit = QtWidgets.QLineEdit("")
        self.pva_edit.setReadOnly(True)
        self.pva_edit.setToolTip("PVAccess NTNDArray PV name for pystream")
        if self.ioc and getattr(self.ioc, "_pva_server", None) is not None:
            self.pva_edit.setText(self.ioc._pva_server._pv_name)
        status_form.addRow("PVA Stream:", self.pva_edit)

        right_layout.addWidget(status_box)

        # HDF5 file save panel
        save_box = QtWidgets.QGroupBox("HDF5 File Save")
        save_form = QtWidgets.QFormLayout(save_box)
        save_form.setLabelAlignment(QtCore.Qt.AlignRight)

        path_row = QtWidgets.QHBoxLayout()
        self.save_path_edit = QtWidgets.QLineEdit()
        self.save_path_edit.setPlaceholderText("/tmp/detector/")
        self.save_path_edit.setText(
            str(self.ioc.HDF1.FilePath._data["value"]) or "/tmp/detector/")
        browse_btn = QtWidgets.QPushButton("Browse…")
        browse_btn.setMaximumWidth(80)
        browse_btn.clicked.connect(self._browse_save_path)
        path_row.addWidget(self.save_path_edit)
        path_row.addWidget(browse_btn)
        save_form.addRow("Directory:", path_row)

        self.save_name_edit = QtWidgets.QLineEdit("scan")
        self.save_name_edit.setText(
            str(self.ioc.HDF1.FileName._data["value"]) or "scan")
        save_form.addRow("File name:", self.save_name_edit)

        self.save_number_edit = QtWidgets.QLineEdit("1")
        self.save_number_edit.setText(
            str(int(self.ioc.HDF1.FileNumber._data["value"])))
        save_form.addRow("File number:", self.save_number_edit)

        # Don't seed from NumCapture._data — its pvproperty default of 1
        # (now 1000) would silently auto-close the file after one frame
        # if the user clicked Start Capture without changing it.
        self.save_count_edit = QtWidgets.QLineEdit("1000")
        save_form.addRow("Frames to save:", self.save_count_edit)

        self.save_status_label = QtWidgets.QLabel("Idle")
        save_form.addRow("Status:", self.save_status_label)

        # Received: frames handed from camera to HDF5 plugin.
        # Captured: frames actually committed to disk.
        # Gap between them = how far the writer is behind = how saturated
        # the in-RAM buffer is (also shown explicitly as Buffer below).
        self.save_received_label = QtWidgets.QLabel("0")
        save_form.addRow("Received:", self.save_received_label)
        self.save_captured_label = QtWidgets.QLabel("0")
        save_form.addRow("Captured:", self.save_captured_label)
        self.save_writer_label = QtWidgets.QLabel("Idle")
        save_form.addRow("Writer:", self.save_writer_label)
        self.save_buffer_label = QtWidgets.QLabel("0 / 0")
        save_form.addRow("Buffer:", self.save_buffer_label)

        save_btn_row = QtWidgets.QHBoxLayout()
        self.capture_btn = QtWidgets.QPushButton("Start Capture")
        self.capture_btn.clicked.connect(self._toggle_capture)
        save_btn_row.addWidget(self.capture_btn)
        save_form.addRow("", save_btn_row)

        right_layout.addWidget(save_box)

        # Activity log
        log_box = QtWidgets.QGroupBox("Activity Log")
        log_layout = QtWidgets.QVBoxLayout(log_box)
        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        log_layout.addWidget(self.log_edit)
        right_layout.addWidget(log_box, 1)

        self.statusBar().showMessage("Ready")

    def _create_tabs(self):
        if not self.camera:
            return
        supported = set(self.camera.list_params())

        # Place every supported param into the matching tab; unmatched go to "Other"
        used: set = set()
        for tab_title, items in PARAM_TABS:
            tab_items = [(k, l) for k, l in items if k in supported]
            if not tab_items:
                continue
            self._add_tab(tab_title, tab_items)
            for k, _ in tab_items:
                used.add(k)

        leftover = sorted(supported - used)
        if leftover:
            self._add_tab("Other", [(k, k) for k in leftover])

        # Always add a Device Info tab if we have CameraInfo
        info = self.camera.get_info()
        info_items = [
            ("Vendor", info.vendor),
            ("Model", info.model),
            ("Serial Number", info.serial_number or "-"),
            ("Firmware", info.firmware_version or "-"),
            ("Sensor", f"{info.sensor_width} × {info.sensor_height}"),
            ("Pixel Size", f"{info.pixel_size_um} µm"),
            ("Bits/pixel", str(info.bits_per_pixel)),
            ("Max FPS", f"{info.max_frame_rate}"),
            ("Binning", str(info.supported_binning)),
            ("Trigger Modes", ", ".join(info.supported_trigger_modes)),
        ]
        info_tab = QtWidgets.QWidget()
        info_layout = QtWidgets.QFormLayout(info_tab)
        info_layout.setLabelAlignment(QtCore.Qt.AlignRight)
        for label, value in info_items:
            lbl = QtWidgets.QLabel(str(value))
            info_layout.addRow(label + ":", lbl)
        self.param_tabs.addTab(info_tab, "Device Info")

    def _add_tab(self, title: str, items: List[Tuple[str, str]]):
        tab = QtWidgets.QWidget()
        tab_layout = QtWidgets.QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        tab_layout.addWidget(scroll)

        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        form = QtWidgets.QFormLayout(content)
        form.setLabelAlignment(QtCore.Qt.AlignRight)
        form.setSpacing(8)

        for key, label in items:
            widget = self._make_widget(key)
            if widget:
                self.controls[key] = widget
                form.addRow(label + ":", widget)

        self.param_tabs.addTab(tab, title)

    def _make_widget(self, key: str) -> Optional[QtWidgets.QWidget]:
        try:
            value = self.camera.get_param(key)
        except Exception:
            return None

        if key in ENUM_CHOICES:
            combo = QtWidgets.QComboBox()
            for choice in ENUM_CHOICES[key]:
                combo.addItem(str(choice), choice)
            idx = combo.findData(value)
            if idx < 0:
                idx = combo.findText(str(value))
            if idx >= 0:
                combo.setCurrentIndex(idx)
            if key in READ_ONLY_PARAMS:
                combo.setEnabled(False)
            else:
                combo.currentIndexChanged.connect(
                    lambda _, k=key: self._apply_single(k))
            return combo

        edit = QtWidgets.QLineEdit(str(value))
        if key in READ_ONLY_PARAMS:
            edit.setReadOnly(True)
        else:
            edit.returnPressed.connect(lambda k=key: self._apply_single(k))
        return edit

    # ---------- Value sync ----------

    def _populate_values(self):
        for key, widget in self.controls.items():
            try:
                val = self.camera.get_param(key)
                self._set_widget_value(widget, val)
            except Exception:
                pass

    def _refresh_readonly(self):
        if not self.camera:
            return
        for key in READ_ONLY_PARAMS:
            if key in self.controls:
                try:
                    val = self.camera.get_param(key)
                    self._set_widget_value(self.controls[key], val)
                except Exception:
                    pass
        # Sensor temperature / cooler status / frame rate from camera API.
        # AcquisitionFrameRate is aliased to DCAM InternalFrameRate
        # (camera's achievable rate given current exposure/binning/ROI),
        # which is meaningful even when not acquiring.
        for key in ("SensorTemperature", "SensorCoolerStatus",
                    "AcquisitionFrameRate"):
            if key in self.status_labels:
                try:
                    val = self.camera.get_param(key)
                    if isinstance(val, float):
                        self.status_labels[key].setText(f"{val:.2f}")
                    else:
                        self.status_labels[key].setText(str(val))
                except Exception:
                    pass

        # HDF5 capture status
        try:
            hdf = self.ioc.HDF1
            if hdf._capturing:
                self.save_status_label.setText("Capturing")
                self.capture_btn.setText("Stop Capture")
            else:
                self.save_status_label.setText("Idle")
                self.capture_btn.setText("Start Capture")
            recv = getattr(hdf, "_frames_received", 0)
            self.save_received_label.setText(str(recv))
            self.save_captured_label.setText(str(hdf._frames_captured))
            # Writer thread state — direct read from the plugin attribute
            # rather than going through the PV, so it stays responsive.
            q = hdf._write_queue
            depth = q.qsize() if q is not None else 0
            qmax = q.maxsize if q is not None else 0
            fbytes = getattr(hdf, "_frame_bytes", 0)
            if qmax and fbytes:
                used_mb = depth * fbytes / 1e6
                cap_mb = qmax * fbytes / 1e6
                pct = 100.0 * depth / qmax
                self.save_buffer_label.setText(
                    f"{used_mb:,.0f} / {cap_mb:,.0f} MB  ({pct:4.1f}%)")
            elif qmax:
                pct = 100.0 * depth / qmax
                self.save_buffer_label.setText(
                    f"{depth} / {qmax} frames  ({pct:4.1f}%)")
            else:
                self.save_buffer_label.setText("0 MB")
            # Drops only show up in the log otherwise — surface them inline.
            drops = getattr(hdf, "_dropped_frames", 0)
            if drops:
                cur = self.save_buffer_label.text()
                self.save_buffer_label.setText(f"{cur}   dropped: {drops}")
            # Infer state without polling the PV: queue draining = Writing,
            # queue empty + not capturing = Idle, queue empty + capturing = Waiting
            if depth > 0:
                state = "Writing"
            elif hdf._capturing:
                state = "Waiting for frames"
            else:
                state = "Idle"
            self.save_writer_label.setText(state)
            # Update file number from plugin (auto-increments)
            self.save_number_edit.setText(
                str(int(hdf.FileNumber._data["value"])))
        except Exception:
            pass

    def _set_widget_value(self, widget, value):
        if isinstance(widget, QtWidgets.QComboBox):
            idx = widget.findData(value)
            if idx < 0:
                idx = widget.findText(str(value))
            if idx >= 0:
                widget.setCurrentIndex(idx)
        else:
            widget.setText(str(value))

    # ---------- Actions ----------

    def _read_widget_value(self, key: str, widget) -> Any:
        if isinstance(widget, QtWidgets.QComboBox):
            value = widget.currentData()
            if value is None:
                value = widget.currentText()
            return value
        text = widget.text().strip()
        if not text:
            return None
        # Type-coerce based on current value
        current = self.camera.get_param(key)
        if isinstance(current, bool):
            return text.lower() in ("1", "true", "yes", "on")
        if isinstance(current, int) and not isinstance(current, bool):
            return int(float(text))
        if isinstance(current, float):
            return float(text)
        return text

    def _apply_single(self, key: str):
        widget = self.controls[key]
        try:
            value = self._read_widget_value(key, widget)
            if value is None:
                return
            old = self.camera.get_param(key)
            if value != old:
                self.camera.set_param(key, value)
                self._log(f"{key}: {old} → {value}")
                # Push to corresponding cam1: PV when relevant
                self._sync_to_pv(key, value)
        except Exception as exc:
            self._log(f"✗ {key}: {exc}")

    def _apply_all(self):
        changed = []
        for key, widget in self.controls.items():
            if key in READ_ONLY_PARAMS:
                continue
            try:
                value = self._read_widget_value(key, widget)
                if value is None:
                    continue
                old = self.camera.get_param(key)
                if value != old:
                    self.camera.set_param(key, value)
                    self._sync_to_pv(key, value)
                    changed.append(f"{key}: {old} → {value}")
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "Invalid parameter",
                                              f"{key}: {exc}")
                self._log(f"✗ {key}: {exc}")
                return
        if changed:
            for c in changed:
                self._log(f"  {c}")
            self._log(f"Applied {len(changed)} parameter(s)")
        else:
            self._log("No parameters changed")
        self.statusBar().showMessage("Parameters applied", 3000)

    def _sync_to_pv(self, key: str, value: Any):
        """Mirror common parameter changes into the corresponding cam1: PV."""
        cam = self.ioc.cam1
        try:
            if key == "ExposureTime":
                cam.AcquireTime._data["value"] = float(value)
                cam.AcquireTime_RBV._data["value"] = float(value)
            elif key == "BinningHorizontal":
                cam.BinX._data["value"] = int(value)
                cam.BinX_RBV._data["value"] = int(value)
            elif key == "BinningVertical":
                cam.BinY._data["value"] = int(value)
                cam.BinY_RBV._data["value"] = int(value)
            elif key == "TriggerMode":
                cam.TriggerMode._data["value"] = str(value)
                cam.TriggerMode_RBV._data["value"] = str(value)
            elif key == "TriggerSource":
                cam.TriggerSource._data["value"] = str(value)
        except Exception:
            pass

    def _force_internal_trigger(self):
        """Switch the camera to Internal trigger / TriggerMode=Off.

        The hardware default is External (set in orca_fire_dcam.open() so
        tomoscan-driven scans work out of the box). For GUI-driven runs
        — both "Start" and "Start Capture" — we want free-running, so we
        override it here. Tomoscan re-sets External itself when it takes
        over, so this doesn't leak.
        """
        if self.camera:
            for prop, val in (("TriggerMode", "Off"),
                              ("TriggerSource", "Internal")):
                try:
                    self.camera.set_param(prop, val)
                except Exception:
                    pass
        self.ioc.cam1.TriggerMode._data["value"] = "Off"
        self.ioc.cam1.TriggerMode_RBV._data["value"] = "Off"
        self.ioc.cam1.TriggerSource._data["value"] = "Internal"

    def _start_acquisition(self):
        try:
            self._fps_count = 0
            self._fps_last_t = 0.0
            self._measured_fps = 0.0
            self._force_internal_trigger()
            # Free-running until user clicks Stop
            self.ioc.cam1.ImageMode._data["value"] = "Continuous"
            self.ioc.cam1.NumImages._data["value"] = 0
            self.ioc.cam1._start_acquisition()
            # Refresh widget values
            self._populate_values()
            self._log("Acquisition started (continuous, free-run)")
        except Exception as exc:
            self._log(f"Start failed: {exc}")

    def _stop_acquisition(self):
        try:
            self.ioc.cam1._stop_acquisition()
            self._measured_fps = 0.0
            self._log("Acquisition stopped")
        except Exception as exc:
            self._log(f"Stop failed: {exc}")

    def _single_frame(self):
        try:
            if not self.camera.is_acquiring():
                self.camera.start_acquisition()
                frame = self.camera.acquire_frame(timeout_ms=5000)
                self.camera.stop_acquisition()
            else:
                frame = self.camera.acquire_frame(timeout_ms=5000)
            self.last_frame = frame
            self._log(f"Single frame: {frame.shape} mean={frame.mean():.1f}")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Frame Error", str(exc))
            self._log(f"Single frame failed: {exc}")

    def _software_trigger(self):
        try:
            self.camera.software_trigger()
            self._log("Software trigger fired")
        except Exception as exc:
            self._log(f"Trigger failed: {exc}")

    # ---------- HDF5 capture ----------

    def _browse_save_path(self):
        current = self.save_path_edit.text() or "/tmp"
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose save directory", current)
        if directory:
            self.save_path_edit.setText(directory)

    def _toggle_capture(self):
        hdf = self.ioc.HDF1
        if hdf._capturing:
            self._stop_capture()
        else:
            self._start_capture()

    def _start_capture(self):
        hdf = self.ioc.HDF1
        try:
            path = self.save_path_edit.text().strip()
            name = self.save_name_edit.text().strip() or "scan"
            number = int(self.save_number_edit.text())
            num = int(self.save_count_edit.text())
            if num <= 0:
                QtWidgets.QMessageBox.warning(self, "Invalid",
                                              "Frames to save must be > 0")
                return
            if not path:
                QtWidgets.QMessageBox.warning(self, "Invalid",
                                              "Choose a directory first")
                return
            import os
            os.makedirs(path, exist_ok=True)

            # Push to HDF5 plugin's PVs
            hdf.FilePath._data["value"] = path
            hdf.FilePath_RBV._data["value"] = path
            hdf.FilePathExists_RBV._data["value"] = 1
            hdf.FileName._data["value"] = name
            hdf.FileName_RBV._data["value"] = name
            hdf.FileNumber._data["value"] = number
            hdf.NumCapture._data["value"] = num

            hdf._start_capture()
            full = str(hdf.FullFileName_RBV._data["value"])
            self._log(f"Capturing {num} frames to {full}")

            # Optionally start acquisition if not already running
            if not self.ioc.cam1._acquiring:
                # Force Internal trigger — the hardware default is External
                # (for tomoscan); GUI-driven captures need to free-run.
                self._force_internal_trigger()
                # Set image mode to Multiple with NumImages = num
                self.ioc.cam1.ImageMode._data["value"] = "Multiple"
                self.ioc.cam1.NumImages._data["value"] = num
                self.ioc.cam1._start_acquisition()
                self._log(f"Acquisition started ({num} frames)")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Capture failed", str(exc))
            self._log(f"Capture start failed: {exc}")

    def _stop_capture(self):
        try:
            self.ioc.HDF1._stop_capture()
            captured = self.ioc.HDF1._frames_captured
            self._log(f"Capture stopped ({captured} frames written)")
        except Exception as exc:
            self._log(f"Stop capture failed: {exc}")

    # ---------- Frame callback ----------

    def _on_frame(self, frame, metadata: dict):
        self.last_frame = frame
        # Update frame count display
        if "FrameCount" in self.status_labels:
            self.status_labels["FrameCount"].setText(
                str(metadata.get("frame_number", "-")))
        # Measure fps from frame timing
        now = time.time()
        self._fps_count += 1
        if self._fps_last_t == 0.0:
            self._fps_last_t = now
        elapsed = now - self._fps_last_t
        if elapsed >= 1.0:
            self._measured_fps = self._fps_count / elapsed
            self._fps_count = 0
            self._fps_last_t = now

    # ---------- Misc ----------

    def _log(self, message: str):
        ts = time.strftime("%H:%M:%S")
        self.log_edit.appendPlainText(f"[{ts}] {message}")

    def closeEvent(self, event):
        self._refresh_timer.stop()
        super().closeEvent(event)
