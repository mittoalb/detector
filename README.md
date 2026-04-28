# Multi-Camera Area Detector Control

Camera-agnostic EPICS areaDetector IOC and Qt GUI for scientific cameras.
Currently supports Hamamatsu, Teledyne, and Tucsen — adding new camera types
is a single-file addition.

## Supported Cameras

| Backend | Camera | SDK |
|---------|--------|-----|
| `hamamatsu.orca_fire_dcam` | Hamamatsu ORCA Fire C16240-20UP | DCAM-API + Active Silicon FireBird |
| `hamamatsu.orca_fire_gentl` | Hamamatsu ORCA Fire C16240-20UP | Harvesters/GenTL + Euresys Coaxlink |
| `teledyne.oryx` | Teledyne FLIR Oryx (CXP) | Spinnaker SDK + PySpin |
| `teledyne.kinetix` | Teledyne Photometrics Kinetix | PVCAM + PyVCAM |
| `tucsen.libra_5514pro` | Tucsen Libra 5514 Pro | TUCAM SDK (skeleton) |
| `simulator` | Synthetic frames | none — built in |

## Architecture

```
                          run_ioc.py
                              │
                              ▼
        ┌──────── DetectorIOC (PVGroup) ────────┐
        │  cam1: PVs   HDF1: PVs   image1: PVA  │
        └──────────────────┬────────────────────┘
                           │ BaseCamera interface
                           ▼
       ┌───────────────────────────────────────┐
       │   Camera Registry (auto/explicit)     │
       └─────┬─────────┬─────────┬─────────┬───┘
             ▼         ▼         ▼         ▼
        Hamamatsu Teledyne   Tucsen   Simulator
         DCAM    Spinnaker   TUCAM
       (ORCA Fire) (Oryx)  (Libra 5514)
```

The `BaseCamera` abstract class (`detectors/core/base.py`) defines a uniform
interface — every backend implements `open`, `close`, `get_param`, `set_param`,
`acquire_frame`, `start_acquisition`, `stop_acquisition`, etc.

Backends self-register via the `@register` decorator. Adding a new camera is
a single new file in `detectors/cameras/<vendor>/`.

## Installation

### Base requirements

```bash
conda create -n detector python=3.11
conda activate detector
pip install numpy PyQt5 caproto h5py pvapy
pip install imageio  # optional, for TIFF export
```

### Per-camera SDK requirements

| Backend | Install |
|---------|---------|
| `hamamatsu.orca_fire_dcam` | DCAM-API Lite for Linux + FireBird kernel driver. See `firebird-driver-rhel9-patch/README.md` |
| `hamamatsu.orca_fire_gentl` | `pip install harvesters` + Euresys eGrabber |
| `teledyne.oryx` | Spinnaker SDK + `pip install spinnaker-python` |
| `teledyne.kinetix` | PVCAM + `pip install pyvcam` |
| `tucsen.libra_5514pro` | TUCAM SDK from Tucsen |

## Running

```bash
# List available backends
python run_ioc.py --list-cameras

# Auto-detect
python run_ioc.py --prefix MYDET:

# Specify backend
python run_ioc.py --camera hamamatsu.orca_fire_dcam --prefix ORCA:
python run_ioc.py --camera teledyne.oryx --prefix ORYX:
python run_ioc.py --camera simulator --prefix SIM:

# With Qt GUI
python run_ioc.py --camera simulator --gui

# Show all served PVs
python run_ioc.py --camera simulator --list-pvs

# Custom PVA stream PV name
python run_ioc.py --camera simulator --pva-pv MYDET:image:NTNDArray
```

## Adding a new camera

1. Create `detectors/cameras/<vendor>/<model>.py` implementing `BaseCamera`:

```python
from detectors.core.base import BaseCamera, CameraInfo
from detectors.core.registry import register

@register
class MyNewCamera(BaseCamera):
    camera_type = "vendor.model"
    display_name = "Vendor Model XYZ"

    def open(self):
        # Open camera via vendor SDK
        self._info = CameraInfo(vendor="Vendor", model="XYZ", ...)

    def close(self):
        # Cleanup

    def get_param(self, name):
        # Read parameter

    def set_param(self, name, value):
        # Write parameter

    def list_params(self):
        return ["ExposureTime", "Width", "Height", ...]

    def start_acquisition(self):
        ...

    def stop_acquisition(self):
        ...

    def acquire_frame(self, timeout_ms=5000):
        # Return numpy array (height, width)
        ...
```

2. Add the module path to `detectors/core/registry.py:load_all()`.

That's it — the IOC, GUI, and tomoscan integration work without changes.

## Standard parameter names

Backends should use these names where applicable so the GUI/IOC are uniform
across cameras:

| Name | Type | Description |
|------|------|-------------|
| `ExposureTime` | float | Exposure in seconds |
| `AcquisitionFrameRate` | float | Current frame rate (often read-only) |
| `Width` / `Height` | int | Image dimensions |
| `OffsetX` / `OffsetY` | int | ROI offset |
| `BinningHorizontal` / `BinningVertical` | int | Binning factor |
| `PixelFormat` | str | "Mono16", "Mono12", etc. |
| `TriggerMode` | str | "Off", "On" |
| `TriggerSource` | str | "Internal", "External", "Software" |
| `TriggerActive` | str | "Edge", "Level" |
| `TriggerPolarity` | str | "Positive", "Negative" |
| `SensorMode` | str | "Area", "Lightsheet" |
| `ReadoutSpeed` | str | "Fastest", "Slowest" |
| `ShutterMode` | str | "Rolling", "Global" |
| `SensorTemperature` | float | Celsius (read-only) |
| `SensorCooler` | str | "Off", "On", "Max" |
| `SensorCoolerStatus` | str | "Ready", "Busy", "Off", "Error" |
| `SensorTemperatureTarget` | float | Cooler setpoint |

Backends raise `KeyError` for unsupported parameters; the GUI hides them.

## EPICS PVs

Standard areaDetector PVs are served under `<prefix>cam1:` and `<prefix>HDF1:`.
Run `python run_ioc.py --camera <type> --list-pvs` to see all 65+ PVs.

## tomoscan integration

```
CameraPVPrefix      = <prefix>:
FilePluginPVPrefix  = <prefix>:HDF1:
```

Step-scan and fly-scan workflows are supported. The HDF5 plugin writes frames
to dataset `/exchange/data` with shape `(N, height, width)` uint16.

## PVAccess streaming

Frames are also published as NTNDArray on `<prefix>image1:ArrayData` (default),
throttled to 30 fps for display. Use with `pyqtstream` or any PVA NTNDArray
viewer:

```bash
python pyqtstream.py --pv <prefix>image1:ArrayData
```

## Directory layout

```
detectors/
├── core/
│   ├── base.py              # BaseCamera ABC + CameraInfo
│   └── registry.py          # Camera type registry / factory
├── cameras/
│   ├── hamamatsu/
│   │   ├── orca_fire_dcam.py     # ORCA Fire via DCAM
│   │   └── orca_fire_gentl.py    # ORCA Fire via Harvesters
│   ├── teledyne/
│   │   ├── oryx.py               # Oryx via Spinnaker
│   │   └── kinetix.py            # Kinetix via PVCAM
│   ├── tucsen/
│   │   └── libra.py              # Libra 5514 Pro (skeleton)
│   └── simulator.py              # Built-in simulator
├── server/
│   ├── ioc.py               # DetectorIOC (top-level)
│   ├── cam_plugin.py        # cam1: PVs
│   ├── hdf5_plugin.py       # HDF1: PVs
│   └── ntnda_server.py      # PVAccess NTNDArray
└── gui/
    └── qt_gui.py            # Camera-agnostic Qt GUI

run_ioc.py                   # Main entry point
check_environment.py         # Environment / hardware checker
firebird-driver-rhel9-patch/ # Patched Active Silicon driver source

orca/                        # OLD ORCA-only code (kept for reference)
run_orca_ioc.py              # OLD entry point
```

## Migration from the old `orca/` package

The original ORCA-only code in `orca/` is still functional and works with
`run_orca_ioc.py`. New work should use the camera-agnostic structure:

- `orca/dcam_acquirer.py` → `detectors/cameras/hamamatsu/orca_fire_dcam.py`
- `orca/harvesters_orca_fire.py` → `detectors/cameras/hamamatsu/orca_fire_gentl.py`
- `orca/epics_ad_server.py` → `detectors/server/{cam_plugin,hdf5_plugin,ntnda_server,ioc}.py`
- `orca/qt_detector.py` → `detectors/gui/qt_gui.py`
- `run_orca_ioc.py` → `run_ioc.py --camera hamamatsu.orca_fire_dcam`

## DCAM (Hamamatsu) notes

The Hamamatsu DCAM backend requires DCAM-API Lite for Linux installed at
`/usr/local/hamamatsu_dcam/` and a supported frame grabber driver. On systems
where `/usr/local` is read-only, see `firebird-driver-rhel9-patch/README.md`
for the bind-mount workaround.

Critical: the DCAM library must be loaded with `ctypes.RTLD_GLOBAL` mode (the
backend handles this) — otherwise the FireBird module can't resolve symbols
and DCAM returns NOCAMERA.
