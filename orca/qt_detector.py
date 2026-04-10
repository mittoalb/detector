#!/usr/bin/env python3
"""
ORCA Fire Qt-based detector parameter control GUI.

A modern PyQt5 interface for controlling ORCA Fire detector parameters.
Provides tabbed parameter organization, real-time status updates, and
camera connection status indication.

This tool does NOT display live camera images; use pystream for that.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, Optional

from orca.dummy_detector import DummyOrcaFireDetector

try:
    from PyQt5 import QtCore, QtWidgets
    from PyQt5.QtCore import pyqtSignal as Signal
except ImportError:
    try:
        from PySide6 import QtCore, QtWidgets
        from PySide6.QtCore import Signal
    except ImportError as exc:
        raise ImportError(
            "PyQt5 or PySide6 is required. Install with: pip install PyQt5"
        ) from exc

class QtOrcaFireDetectorGui(QtWidgets.QMainWindow):
    """
    Qt-based ORCA Fire detector parameter control interface.
    
    Provides:
    - Tabbed parameter organization (Timing, Image Size, Acquisition, Processing, Diagnostics)
    - Real-time status monitoring with camera connection indicator
    - Parameter acquisition commands (Start, Stop, Single Frame)
    - Frame export to TIFF format
    - Activity logging
    
    Does NOT include live image viewing; use pystream or similar EPICS viewer for image display.
    """

    parameter_changed = Signal(str, object)
    frame_ready = Signal(object, dict)

    # Allowed values for enum-style parameters (GenICam enumerations)
    ENUM_CHOICES: Dict[str, list] = {
        "AcquisitionMode": ["Continuous", "SingleFrame", "MultiFrame"],
        "PixelFormat": ["Mono8", "Mono16"],
        "SubarrayMode": ["OFF", "ON"],
        "SensorMode": ["Area", "Lightsheet", "SplitView", "DualLightsheet"],
        "ReadoutSpeed": ["StandardScan", "SlowScan"],
        "ReadoutDirection": ["Forward", "Backward", "Bidirectional", "ReverseBidirectional"],
        "ShutterMode": ["Rolling", "GlobalReset"],
        "TriggerGlobalExposure": ["DelayedReadout", "GlobalReset"],
        "TriggerMode": ["Off", "On"],
        "TriggerSource": ["Internal", "External", "Software", "MasterPulse"],
        "TriggerActive": ["Edge", "Level", "SyncReadout", "StartTrigger"],
        "TriggerPolarity": ["Positive", "Negative"],
        "TriggerConnector": ["Interface", "BNC"],
        "OutputTriggerKind": ["ExposureTiming", "ReadoutEnd", "TriggerReady",
                              "AnyRowExposureTiming", "Programmable", "High", "Low"],
        "OutputTriggerPolarity": ["Positive", "Negative"],
        "OutputTriggerActive": ["Edge", "Level"],
        "SensorCooler": ["Off", "On", "Max"],
        "DefectCorrectMode": ["Off", "On"],
        "HotPixelCorrectLevel": ["Off", "Standard", "Aggressive"],
        "HighDynamicRangeMode": ["Off", "On"],
        "RecursiveFilter": ["Off", "On"],
        "SpotNoiseReducer": ["Off", "On"],
        "SensorGapCorrectMode": ["Off", "On"],
        "MasterPulseMode": ["Off", "On"],
        "ReverseX": [False, True],
        "ReverseY": [False, True],
    }

    def __init__(self, detector: DummyOrcaFireDetector) -> None:
        super().__init__()
        self.detector = detector
        self.controls: Dict[str, Any] = {}  # QLineEdit or QComboBox
        self.status_labels: Dict[str, QtWidgets.QLabel] = {}
        self.last_frame: Optional[Any] = None

        self.setWindowTitle("ORCA Fire Qt Detector Control")
        self.resize(1200, 700)
        self._apply_dark_theme()

        self._build_ui()
        self._connect_signals()

        self.detector.register_parameter_callback(self._emit_parameter_changed)
        self.detector.register_frame_callback(self._emit_frame_ready)
        
        self._update_camera_status()

    def _apply_dark_theme(self) -> None:
        """Apply a dark theme to the application."""
        dark_stylesheet = """
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
            QGroupBox { color: #8ab4f8; border: 1px solid #3c4043; border-radius: 4px; padding-top: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
            QTabWidget, QTabBar { background-color: #202124; color: #f1f3f4; }
            QTabBar::tab { background-color: #2a2d32; color: #f1f3f4; padding: 6px 12px; }
            QTabBar::tab:selected { background-color: #3c4043; }
            QPlainTextEdit { background-color: #111315; color: #e8eaed; border: 1px solid #3c4043; }
            QScrollArea { border: none; background-color: #202124; }
            QScrollBar { background-color: #2a2d32; }
            QScrollBar:vertical { width: 12px; }
            QScrollBar::handle { background-color: #5f6368; border-radius: 6px; }
            QScrollBar::handle:hover { background-color: #9aa0a6; }
        """
        self.setStyleSheet(dark_stylesheet)

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        toolbar = QtWidgets.QHBoxLayout()
        layout.addLayout(toolbar)

        title = QtWidgets.QLabel("ORCA Fire Qt Detector Control")
        toolbar.addWidget(title)
        toolbar.addStretch(1)

        self.start_button = QtWidgets.QPushButton("Start")
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.apply_button = QtWidgets.QPushButton("Apply Changes")
        self.single_button = QtWidgets.QPushButton("Single Frame")
        self.save_button = QtWidgets.QPushButton("Save Last Frame")

        for widget in [self.start_button, self.stop_button, self.apply_button, self.single_button, self.save_button]:
            widget.setMinimumWidth(120)
            toolbar.addWidget(widget)

        body = QtWidgets.QHBoxLayout()
        layout.addLayout(body, 1)

        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        body.addWidget(left_panel, 1)

        self.param_tabs = QtWidgets.QTabWidget()
        left_layout.addWidget(self.param_tabs, 1)
        self._create_parameter_tabs()

        right_panel = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        body.addWidget(right_panel, 0)

        status_box = QtWidgets.QGroupBox("Status")
        status_layout = QtWidgets.QFormLayout(status_box)
        status_layout.setLabelAlignment(QtCore.Qt.AlignRight)
        
        camera_status_row = QtWidgets.QHBoxLayout()
        self.camera_status_indicator = QtWidgets.QLabel("●")
        self.camera_status_indicator.setStyleSheet("color: #ff6b6b; font-size: 16px;")
        self.camera_status_label = QtWidgets.QLabel("Disconnected (Simulation)")
        camera_status_row.addWidget(self.camera_status_indicator)
        camera_status_row.addWidget(self.camera_status_label)
        camera_status_row.addStretch()
        status_layout.addRow("Camera:", camera_status_row)
        
        for name, label in [
            ("StatusMessage", "Status"),
            ("Acquire", "Acquiring"),
            ("ImageCounter", "Image Counter"),
            ("SensorTemperature", "Temperature (°C)"),
            ("SensorCoolerStatus", "Cooler Status"),
        ]:
            status_label = QtWidgets.QLabel(str(self.detector.get_parameter(name)))
            status_layout.addRow(label + ":", status_label)
            self.status_labels[name] = status_label
        right_layout.addWidget(status_box)

        controls_panel = QtWidgets.QGroupBox("Quick Actions")
        controls_layout = QtWidgets.QVBoxLayout(controls_panel)
        controls_layout.addWidget(self.start_button)
        controls_layout.addWidget(self.stop_button)
        controls_layout.addWidget(self.apply_button)
        controls_layout.addWidget(self.single_button)
        controls_layout.addWidget(self.save_button)
        right_layout.addWidget(controls_panel)

        log_box = QtWidgets.QGroupBox("Activity Log")
        log_layout = QtWidgets.QVBoxLayout(log_box)
        self.log_text = QtWidgets.QPlainTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)
        right_layout.addWidget(log_box, 1)

        self.statusBar().showMessage("Ready")


    def _connect_signals(self) -> None:
        self.start_button.clicked.connect(self.detector.start_acquisition)
        self.stop_button.clicked.connect(self.detector.stop_acquisition)
        self.apply_button.clicked.connect(self._apply_parameters)
        self.single_button.clicked.connect(self._single_frame)
        self.save_button.clicked.connect(self._save_frame)

        self.parameter_changed.connect(self._handle_parameter_changed)
        self.frame_ready.connect(self._handle_frame_ready)

    # Parameters that should be read-only in the GUI
    READ_ONLY_PARAMS = {
        "SensorWidth", "SensorHeight", "SensorPixelWidth", "SensorPixelHeight",
        "DeviceModelName", "DeviceFamilyName", "DeviceSerialNumber",
        "DeviceFirmwareVersion", "BitsPerChannel",
        "NumberOfOutputTriggerConnector",
        "TimingReadoutTime", "TimingCyclicTriggerPeriod", "InternalFrameRate",
        "SensorCoolerStatus", "SensorTemperature",
        "ImageCounter", "StatusMessage",
    }

    def _create_parameter_tabs(self) -> None:
        """Create tabbed parameter groups."""
        groups = {
            "Exposure & Acquisition": [
                ("ExposureTime", "Exposure Time (µs)"),
                ("AcquisitionMode", "Acq. Mode"),
                ("AcquisitionFrameRate", "Frame Rate (Hz)"),
                ("AcquisitionFrameCount", "Frame Count"),
                ("Acquire", "Acquire"),
                ("ImageCounter", "Image Counter"),
            ],
            "Image Format & ROI": [
                ("Width", "Width (px)"),
                ("Height", "Height (px)"),
                ("OffsetX", "X Offset (px)"),
                ("OffsetY", "Y Offset (px)"),
                ("BinningHorizontal", "Binning H"),
                ("BinningVertical", "Binning V"),
                ("SubarrayMode", "Subarray Mode"),
                ("PixelFormat", "Pixel Format"),
                ("ReverseX", "Reverse X"),
                ("ReverseY", "Reverse Y"),
            ],
            "Readout & Sensor": [
                ("SensorMode", "Sensor Mode"),
                ("ReadoutSpeed", "Readout Speed"),
                ("ReadoutDirection", "Readout Direction"),
                ("ShutterMode", "Shutter Mode"),
                ("TriggerGlobalExposure", "Global Exposure"),
            ],
            "Trigger Input": [
                ("TriggerMode", "Trigger Mode"),
                ("TriggerSource", "Trigger Source"),
                ("TriggerActive", "Trigger Active"),
                ("TriggerPolarity", "Trigger Polarity"),
                ("TriggerConnector", "Trigger Connector"),
                ("TriggerDelay", "Trigger Delay (µs)"),
                ("TriggerTimes", "Trigger Count"),
            ],
            "Output Trigger": [
                ("NumberOfOutputTriggerConnector", "Output Connectors"),
                ("OutputTriggerKind", "Output Kind"),
                ("OutputTriggerPolarity", "Output Polarity"),
                ("OutputTriggerActive", "Output Active"),
                ("OutputTriggerDelay", "Output Delay (s)"),
                ("OutputTriggerPeriod", "Output Period (s)"),
            ],
            "Cooling": [
                ("SensorTemperature", "Temperature (°C)"),
                ("SensorCooler", "Cooler"),
                ("SensorTemperatureTarget", "Target Temp (°C)"),
                ("SensorCoolerStatus", "Cooler Status"),
            ],
            "Correction & Processing": [
                ("DefectCorrectMode", "Defect Correction"),
                ("HotPixelCorrectLevel", "Hot Pixel Level"),
                ("ContrastGain", "Contrast Gain"),
                ("ContrastOffset", "Contrast Offset"),
                ("HighDynamicRangeMode", "HDR Mode"),
                ("RecursiveFilter", "Recursive Filter"),
                ("RecursiveFilterFrames", "Recursive Frames"),
                ("SpotNoiseReducer", "Spot Noise Reducer"),
                ("SensorGapCorrectMode", "Gap Correction"),
            ],
            "Master Pulse": [
                ("MasterPulseMode", "Mode"),
                ("MasterPulseInterval", "Interval (s)"),
                ("MasterPulseBurstTimes", "Burst Count"),
            ],
            "Timing Info": [
                ("TimingReadoutTime", "Readout Time (s)"),
                ("TimingCyclicTriggerPeriod", "Cyclic Trig Period (s)"),
                ("InternalFrameRate", "Internal Frame Rate (Hz)"),
            ],
            "Device Info": [
                ("DeviceModelName", "Model"),
                ("DeviceFamilyName", "Family"),
                ("DeviceSerialNumber", "Serial Number"),
                ("DeviceFirmwareVersion", "Firmware"),
                ("SensorWidth", "Sensor Width (px)"),
                ("SensorHeight", "Sensor Height (px)"),
                ("SensorPixelWidth", "Pixel Width (m)"),
                ("SensorPixelHeight", "Pixel Height (m)"),
                ("BitsPerChannel", "Bits Per Channel"),
            ],
        }

        for title, items in groups.items():
            tab_widget = QtWidgets.QWidget()
            tab_layout = QtWidgets.QVBoxLayout(tab_widget)
            tab_layout.setContentsMargins(8, 8, 8, 8)
            tab_layout.setSpacing(10)

            scroll_area = QtWidgets.QScrollArea()
            scroll_area.setWidgetResizable(True)
            scroll_area.setStyleSheet("QScrollArea { border: none; }")
            tab_layout.addWidget(scroll_area)

            scroll_content = QtWidgets.QWidget()
            scroll_area.setWidget(scroll_content)
            scroll_layout = QtWidgets.QVBoxLayout(scroll_content)
            scroll_layout.setContentsMargins(0, 0, 0, 0)
            scroll_layout.setSpacing(0)

            form_layout = QtWidgets.QFormLayout()
            form_layout.setLabelAlignment(QtCore.Qt.AlignRight)
            form_layout.setSpacing(8)

            for key, label_text in items:
                current_value = self.detector.get_parameter(key)
                if key in self.ENUM_CHOICES:
                    combo = QtWidgets.QComboBox()
                    for choice in self.ENUM_CHOICES[key]:
                        combo.addItem(str(choice), choice)
                    idx = combo.findData(current_value)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
                    if key in self.READ_ONLY_PARAMS:
                        combo.setEnabled(False)
                    self.controls[key] = combo
                    form_layout.addRow(label_text + ":", combo)
                else:
                    edit = QtWidgets.QLineEdit(str(current_value))
                    if key in self.READ_ONLY_PARAMS:
                        edit.setReadOnly(True)
                    self.controls[key] = edit
                    form_layout.addRow(label_text + ":", edit)

            scroll_layout.addLayout(form_layout)
            scroll_layout.addStretch()
            self.param_tabs.addTab(tab_widget, title)

    def _emit_parameter_changed(self, name: str, value: Any) -> None:
        self.parameter_changed.emit(name, value)

    def _emit_frame_ready(self, frame: Any, metadata: Dict[str, Any]) -> None:
        self.frame_ready.emit(frame, metadata)

    def _handle_parameter_changed(self, name: str, value: Any) -> None:
        if name in self.controls:
            widget = self.controls[name]
            if isinstance(widget, QtWidgets.QComboBox):
                idx = widget.findData(value)
                if idx >= 0:
                    widget.setCurrentIndex(idx)
            else:
                widget.setText(str(value))
        if name in self.status_labels:
            self.status_labels[name].setText(str(value))

        self._log(f"{name} = {value}")

    def _handle_frame_ready(self, frame: Any, metadata: Dict[str, Any]) -> None:
        self.last_frame = frame
        self._update_status_from_metadata(metadata)
        self._log(f"Frame ready: {frame.shape}, Exposure {metadata['ExposureTime']} μs")

    def _update_status_from_metadata(self, metadata: Dict[str, Any]) -> None:
        if "ImageCounter" in self.status_labels:
            self.status_labels["ImageCounter"].setText(str(metadata.get("ImageCounter", "-")))
        if "StatusMessage" in self.status_labels:
            self.status_labels["StatusMessage"].setText(str(metadata.get("StatusMessage", "Acquiring")))

    def _apply_parameters(self) -> None:
        for key, widget in self.controls.items():
            if key in self.READ_ONLY_PARAMS:
                continue
            try:
                if isinstance(widget, QtWidgets.QComboBox):
                    value = widget.currentData()
                else:
                    text = widget.text().strip()
                    if not text:
                        continue
                    current_value = self.detector.get_parameter(key)
                    if isinstance(current_value, bool):
                        value = text.lower() in ("1", "true", "yes", "on")
                    elif isinstance(current_value, int):
                        value = int(float(text))
                    elif isinstance(current_value, float):
                        value = float(text)
                    else:
                        value = text
                self.detector.set_parameter(key, value)
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "Invalid parameter", f"{key}: {exc}")
                self._log(f"✗ {key}: {exc}")
                return
        self._log("Parameters applied successfully")
        self.statusBar().showMessage("Parameters applied", 3000)

    def _single_frame(self) -> None:
        try:
            frame = self.detector.acquire_frame()
            self._log(f"Single frame acquired: {frame.shape}")
            self.last_frame = frame
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Frame Error", str(exc))
            self._log(f"Single frame failed: {exc}")

    def _save_frame(self) -> None:
        if self.last_frame is None:
            QtWidgets.QMessageBox.information(self, "No Frame", "No frame available to save.")
            return
        filename = f"frame_{int(time.time())}.tiff"
        try:
            import imageio
            imageio.imwrite(filename, self.last_frame.astype("uint16"))
            self._log(f"Frame saved as {filename}")
            self.statusBar().showMessage(f"Saved {filename}", 3000)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Save Failed", str(exc))
            self._log(f"Save failed: {exc}")

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.appendPlainText(f"[{timestamp}] {message}")

    def _update_camera_status(self) -> None:
        """Update the camera connection status indicator."""
        if self.detector.is_camera_connected():
            self.camera_status_indicator.setStyleSheet("color: #81c995; font-size: 16px;")
            self.camera_status_label.setText("Connected (Real Camera)")
            self._log("Real camera connected")
        else:
            self.camera_status_indicator.setStyleSheet("color: #ff6b6b; font-size: 16px;")
            self.camera_status_label.setText("Disconnected (Simulation)")
            self._log("Running in simulation mode")


def main() -> None:
    """Launch the Qt detector control GUI.

    Tries to connect to real ORCA Fire hardware via GenTL.
    Falls back to simulation mode when no camera is available.
    """
    import argparse

    parser = argparse.ArgumentParser(description="ORCA Fire Detector Control")
    parser.add_argument("--gentl-path", help="Path to Euresys GenTL producer library")
    parser.add_argument("--device-index", type=int, default=0, help="Camera device index (default: 0)")
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    detector = DummyOrcaFireDetector(
        gen_tl_producer_path=args.gentl_path,
        device_index=args.device_index,
    )
    window = QtOrcaFireDetectorGui(detector)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
