"""
Camera-agnostic Qt GUI.

Builds parameter controls dynamically from the camera's `list_params()` so
any BaseCamera backend works without GUI changes.
"""

import logging
from typing import Any, Dict

import numpy as np

try:
    from PyQt5 import QtCore, QtWidgets
except ImportError:
    from PySide6 import QtCore, QtWidgets

logger = logging.getLogger(__name__)


# Hint the GUI which params should use a combo vs free-form text edit
ENUM_CHOICES = {
    "TriggerMode": ["Off", "On"],
    "TriggerSource": ["Internal", "External", "Software"],
    "TriggerActive": ["Edge", "Level", "SyncReadout"],
    "TriggerPolarity": ["Positive", "Negative"],
    "ShutterMode": ["Rolling", "Global"],
    "SensorMode": ["Area", "Lightsheet", "SplitView"],
    "ReadoutSpeed": ["Fastest", "Slowest"],
    "SensorCooler": ["Off", "On", "Max"],
    "BinningHorizontal": [1, 2, 4],
    "BinningVertical": [1, 2, 4],
    "PixelFormat": ["Mono8", "Mono12", "Mono16"],
}

# Params that should never be editable in the GUI
READ_ONLY_PARAMS = {
    "Width", "Height", "PixelFormat", "SensorTemperature",
    "SensorCoolerStatus", "AcquisitionFrameRate",
}


class DetectorGui(QtWidgets.QMainWindow):
    """Generic camera control window."""

    def __init__(self, ioc, parent=None):
        super().__init__(parent)
        self.ioc = ioc
        self.camera = ioc.camera
        info = self.camera.get_info() if self.camera else None

        self.setWindowTitle(
            f"Detector: {info.vendor} {info.model}" if info else "Detector")
        self.resize(900, 700)

        self.controls: Dict[str, QtWidgets.QWidget] = {}
        self._build_ui()
        self._populate_values()

        # Refresh values periodically (for SensorTemperature etc.)
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_readonly)
        self._refresh_timer.start(1000)

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QHBoxLayout(central)

        # Left: parameters
        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)

        info_box = QtWidgets.QGroupBox("Camera")
        info_layout = QtWidgets.QFormLayout(info_box)
        if self.camera:
            info = self.camera.get_info()
            info_layout.addRow("Vendor:", QtWidgets.QLabel(info.vendor))
            info_layout.addRow("Model:", QtWidgets.QLabel(info.model))
            if info.serial_number:
                info_layout.addRow("Serial:", QtWidgets.QLabel(info.serial_number))
            info_layout.addRow("Sensor:",
                               QtWidgets.QLabel(f"{info.sensor_width} x {info.sensor_height}"))
        left_layout.addWidget(info_box)

        params_box = QtWidgets.QGroupBox("Parameters")
        params_form = QtWidgets.QFormLayout(params_box)
        params_form.setLabelAlignment(QtCore.Qt.AlignRight)

        if self.camera:
            for name in self.camera.list_params():
                widget = self._make_widget(name)
                if widget:
                    self.controls[name] = widget
                    params_form.addRow(name + ":", widget)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(params_box)
        left_layout.addWidget(scroll, 1)

        layout.addWidget(left, 2)

        # Right: actions and PVA info
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)

        actions_box = QtWidgets.QGroupBox("Actions")
        actions_layout = QtWidgets.QVBoxLayout(actions_box)
        self.start_btn = QtWidgets.QPushButton("Start Acquisition")
        self.stop_btn = QtWidgets.QPushButton("Stop Acquisition")
        self.trigger_btn = QtWidgets.QPushButton("Software Trigger")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.trigger_btn.clicked.connect(self._trigger)
        actions_layout.addWidget(self.start_btn)
        actions_layout.addWidget(self.stop_btn)
        actions_layout.addWidget(self.trigger_btn)
        right_layout.addWidget(actions_box)

        # PVA info
        pva_box = QtWidgets.QGroupBox("PVAccess Stream")
        pva_layout = QtWidgets.QFormLayout(pva_box)
        pva_pv = ""
        if self.ioc._pva_server is not None:
            pva_pv = self.ioc._pva_server._pv_name
        pva_edit = QtWidgets.QLineEdit(pva_pv)
        pva_edit.setReadOnly(True)
        pva_layout.addRow("PV name:", pva_edit)
        right_layout.addWidget(pva_box)

        # Log
        log_box = QtWidgets.QGroupBox("Log")
        log_layout = QtWidgets.QVBoxLayout(log_box)
        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        log_layout.addWidget(self.log_edit)
        right_layout.addWidget(log_box, 1)

        layout.addWidget(right, 1)

    def _make_widget(self, name: str) -> QtWidgets.QWidget:
        try:
            value = self.camera.get_param(name)
        except Exception:
            return None

        if name in ENUM_CHOICES:
            combo = QtWidgets.QComboBox()
            for choice in ENUM_CHOICES[name]:
                combo.addItem(str(choice), choice)
            idx = combo.findData(value)
            if idx < 0:
                idx = combo.findText(str(value))
            if idx >= 0:
                combo.setCurrentIndex(idx)
            if name in READ_ONLY_PARAMS:
                combo.setEnabled(False)
            else:
                combo.currentIndexChanged.connect(
                    lambda _, k=name: self._apply_single(k))
            return combo

        edit = QtWidgets.QLineEdit(str(value))
        if name in READ_ONLY_PARAMS:
            edit.setReadOnly(True)
        else:
            edit.returnPressed.connect(lambda k=name: self._apply_single(k))
        return edit

    def _populate_values(self):
        for name, widget in self.controls.items():
            try:
                val = self.camera.get_param(name)
                self._set_widget_value(widget, val)
            except Exception:
                pass

    def _refresh_readonly(self):
        if not self.camera:
            return
        for name in READ_ONLY_PARAMS:
            if name in self.controls:
                try:
                    val = self.camera.get_param(name)
                    self._set_widget_value(self.controls[name], val)
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

    def _apply_single(self, name: str):
        widget = self.controls[name]
        try:
            if isinstance(widget, QtWidgets.QComboBox):
                value = widget.currentData()
                if value is None:
                    value = widget.currentText()
            else:
                text = widget.text().strip()
                if not text:
                    return
                current = self.camera.get_param(name)
                if isinstance(current, bool):
                    value = text.lower() in ("1", "true", "yes", "on")
                elif isinstance(current, int):
                    value = int(float(text))
                elif isinstance(current, float):
                    value = float(text)
                else:
                    value = text
            old = self.camera.get_param(name)
            if value != old:
                self.camera.set_param(name, value)
                self._log(f"{name}: {old} → {value}")
        except Exception as exc:
            self._log(f"{name}: {exc}")
            QtWidgets.QMessageBox.warning(self, "Set parameter failed",
                                          f"{name}: {exc}")

    def _start(self):
        try:
            self.ioc.cam1.Acquire._data["value"] = 1
            self.ioc.cam1._start_acquisition()
            self._log("Started acquisition")
        except Exception as exc:
            self._log(f"Start failed: {exc}")

    def _stop(self):
        try:
            self.ioc.cam1._stop_acquisition()
            self._log("Stopped acquisition")
        except Exception as exc:
            self._log(f"Stop failed: {exc}")

    def _trigger(self):
        try:
            self.camera.software_trigger()
            self._log("Software trigger fired")
        except Exception as exc:
            self._log(f"Trigger failed: {exc}")

    def _log(self, msg: str):
        self.log_edit.appendPlainText(msg)

    def closeEvent(self, event):
        self._refresh_timer.stop()
        super().closeEvent(event)
