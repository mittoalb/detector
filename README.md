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

### GUI process model

With `--gui`, `run_ioc.py` launches the Qt GUI as a **separate process** that
connects to the IOC via ChannelAccess and pvAccess. The GUI talks to the IOC
purely over the network (`caproto.threading.client` + `pvaccess`) through a
proxy layer (`detectors/gui/ca_proxy.py`) that mirrors the in-process IOC
interface the GUI was written against.

Why: some vendor drivers (notably Photometrics PVCAM used by the Kinetix
backend) install signal handlers that Qt's `QApplication` clobbers when they
share a process. In-process Qt then silently starves the camera's DMA loop.
Running Qt in its own process side-steps this uniformly for every backend —
no per-camera branching in the launcher, no threading hacks.

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
| `teledyne.kinetix` | PVCAM SDK (`libpvcam.so.2`) — no Python bindings required, direct ctypes |
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
    ├── qt_gui.py            # Camera-agnostic Qt GUI (also standalone entry:
    │                        #   `python -m detectors.gui.qt_gui --prefix P:`)
    └── ca_proxy.py          # CA/PVA-backed IocProxy — lets qt_gui.py run
                             # against just an IOC prefix (subprocess mode)

run_ioc.py                   # Main entry point
check_environment.py         # Environment / hardware checker
firebird-driver-rhel9-patch/ # Patched Active Silicon driver source
```

## PVCAM (Kinetix) notes

Direct-`ctypes` binding to `libpvcam.so.2`. No `pyvcam` dependency.

- The library is picked up from `LD_LIBRARY_PATH` first, then falls back to
  the vendor path shipped with ADKinetix
  (`/opt/pvcam/library/x86_64/libpvcam.so.2` on typical installs).
- PVCAM claims the camera exclusively — stop any running ADKinetix C++ IOC
  before starting this Python one.
- On the first open, the backend calls `pl_pp_reset`, forces `PARAM_EXP_RES`
  to microseconds (Kinetix defaults to seconds — 10 ms exposure would round
  to 0), selects a valid speed-table entry, and runs one prime setup+start+stop
  cycle. Without any of these, `pl_exp_start_cont` returns silently with
  `PL_ERR_CONFIGURATION_INVALID` or `PL_ERR_NONE`.
- Acquisition uses the standard PVCAM EOF callback registered on the main
  thread, with a `threading.Event` handoff so `acquire_frame()` releases the
  GIL while waiting for the next frame — same pattern DCAM uses via
  `dcamwait_start`.
- Device ACL: `/dev/pvcamPCIE_0` typically has `group: users`. Users not in
  that group need an explicit ACL entry (`setfacl -m u:<user>:rw`).

## DCAM (Hamamatsu) notes

The Hamamatsu DCAM backend requires DCAM-API Lite for Linux installed at
`/usr/local/hamamatsu_dcam/` and a supported frame grabber driver. On systems
where `/usr/local` is read-only, see `firebird-driver-rhel9-patch/README.md`
for the bind-mount workaround.

Critical: the DCAM library must be loaded with `ctypes.RTLD_GLOBAL` mode (the
backend handles this) — otherwise the FireBird module can't resolve symbols
and DCAM returns NOCAMERA.