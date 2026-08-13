# Multi-Camera Area Detector Control

Camera-agnostic EPICS areaDetector IOC and Qt GUI for scientific cameras.
Currently supports Hamamatsu and Teledyne — adding new camera types is a
single-file addition.

## Supported Cameras

| Backend | Camera | SDK |
|---------|--------|-----|
| `hamamatsu.orca_fire_dcam` | Hamamatsu ORCA Fire C16240-20UP | DCAM-API + Active Silicon FireBird |
| `hamamatsu.orca_fire_gentl` | Hamamatsu ORCA Fire C16240-20UP | Harvesters/GenTL + Euresys Coaxlink |
| `teledyne.oryx` | Teledyne FLIR Oryx (10GigE / CXP) | Spinnaker C API (direct ctypes — no PySpin) |
| `teledyne.kinetix` | Teledyne Photometrics Kinetix | PVCAM + PyVCAM |
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
       └─────┬─────────┬─────────┬─────────────┘
             ▼         ▼         ▼
        Hamamatsu Teledyne   Simulator
         DCAM   Spinnaker/PVCAM
       (ORCA Fire) (Oryx/Kinetix)
```

The `BaseCamera` abstract class (`detectors/core/base.py`) defines a uniform
interface — every backend implements `open`, `close`, `get_param`, `set_param`,
`acquire_frame`, `start_acquisition`, `stop_acquisition`, etc.

Backends self-register via the `@register` decorator. Adding a new camera is
a single new file in `detectors/cameras/<vendor>/`.

### GUI process model

With `--gui`, `run_ioc.py` launches the Qt GUI as a **separate process** that
talks to the driver process over three channels:

- **Channel Access** — the standard `<prefix>cam1:` / `<prefix>HDF1:` PVs, for
  compatibility with tomoscan and other EPICS clients.
- **pvAccess NTNDArray** — live frames on `<prefix>image1:ArrayData`.
- **Local UNIX-socket IPC** — a JSON-RPC channel at
  `/tmp/detector_ioc_<prefix>.sock` that carries the camera's *full* BaseCamera
  surface (every GenICam node on the Oryx, every DCAM property on the ORCA,
  every PVCAM param on the Kinetix). Lets the GUI enumerate and edit
  features without needing a matching EPICS PV for each one. Falls back to
  the CA gateway transparently if the socket isn't reachable.

Why the process split: some vendor drivers (notably Photometrics PVCAM used
by the Kinetix backend) install signal handlers that Qt's `QApplication`
clobbers when they share a process. In-process Qt then silently starves the
camera's DMA loop. Running Qt in its own process side-steps this uniformly
for every backend — no per-camera branching in the launcher, no threading
hacks.

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
| `teledyne.oryx` | Spinnaker SDK (`libSpinnaker_C.so.2`) — no Python bindings required, direct ctypes |
| `teledyne.kinetix` | PVCAM SDK (`libpvcam.so.2`) — no Python bindings required, direct ctypes |

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

# Oryx: pick a specific camera by serial (matches ADSpinnaker's CAMERA_ID)
python run_ioc.py --camera teledyne.oryx --serial 23605513 --prefix ORYX:

# With Qt GUI (subprocess, connects via CA/PVA + local IPC)
python run_ioc.py --camera simulator --gui

# Show all served PVs
python run_ioc.py --camera simulator --list-pvs

# Custom PVA stream PV name
python run_ioc.py --camera simulator --pva-pv MYDET:image:NTNDArray
```

### Reopening the GUI without restarting the IOC

If you close the Qt window but the IOC is still running (`ps aux | grep
run_ioc` will show it), reattach a fresh GUI without touching the camera:

```bash
python run_ioc.py --gui-only --prefix ORYX:
```

This spawns just the GUI subprocess. Camera state, acquisition, and any
in-flight HDF5 capture are preserved.

### Stopping a running IOC

`Ctrl-C` in the terminal is the intended shutdown — it cleans up the IPC
socket, releases the camera SDK, and terminates the GUI subprocess.

If the terminal is gone or the process is stuck, find and kill it:

```bash
# Find your IOC(s)
ps -u $USER -o pid,stat,cmd | grep run_ioc | grep -v grep

# Graceful termination (recommended — lets the camera close properly)
pkill -TERM -u $USER -f "run_ioc.py.*--prefix ORYX:"

# Only if TERM doesn't work after ~5 seconds
pkill -KILL -u $USER -f "run_ioc.py.*--prefix ORYX:"
```

`pkill -TERM` gives the process a chance to release the camera SDK cleanly.
`-KILL` is a last resort — the vendor SDK may leave the camera in a weird
state and you'll need to power-cycle it.

### Common startup errors

| Symptom | Cause | Fix |
|---------|-------|-----|
| `IPC socket … in use by another IOC` | Another IOC with the same prefix is already running under your user | `ps -u $USER \| grep run_ioc` — either use it or kill it first |
| `Cannot remove stale IPC socket … Operation not permitted` | Socket file was left by a different user (shouldn't happen with per-UID paths, but stale from an older build might) | `sudo rm /tmp/detector_ioc_*.sock`, then retry |
| `spinError=-1005` (Oryx, access denied) | Camera is already opened by another process (ADSpinnaker EPICS IOC, another Python session) | Stop the other process first |
| GUI shows sensor size but no live frames | `<prefix>image1:ArrayData` PVA channel unreachable (firewall / different subnet) | Check `<prefix>image1:ArrayData` is reachable with `pvget` |

### Logging colors

The console uses ANSI-256 colors on level (INFO cyan, WARNING amber, ERROR
red, CRITICAL bold magenta). Disable / force via standard env vars:

- `NO_COLOR=1` — disable regardless of TTY (https://no-color.org)
- `FORCE_COLOR=1` or `CLICOLOR_FORCE=1` — enable even when piped (`| tee`)
- default: auto-detect via `sys.stderr.isatty()`

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
Backends may *also* expose vendor-specific names (e.g. every GenICam node
on the Oryx) — the GUI shows those in auto-grouped tabs alongside the
standard names.

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
│   └── simulator.py              # Built-in simulator
├── core/
│   ├── base.py              # BaseCamera ABC + CameraInfo
│   ├── registry.py          # Camera type registry / factory
│   └── log_util.py          # ANSI color logging shared by driver + GUI
├── server/
│   ├── ioc.py               # DetectorIOC (top-level; wires plugins + IPC)
│   ├── cam_plugin.py        # cam1: PVs (+ background poller for RO values)
│   ├── hdf5_plugin.py       # HDF1: PVs
│   ├── tiff_plugin.py       # TIF1: PVs
│   ├── ipc.py               # UNIX-socket JSON-RPC to the GUI
│   └── ntnda_server.py      # PVAccess NTNDArray
└── gui/
    ├── qt_gui.py            # Camera-agnostic Qt GUI (also standalone entry:
    │                        #   `python -m detectors.gui.qt_gui --prefix P:`)
    ├── ca_proxy.py          # CA/PVA-backed IocProxy — lets qt_gui.py run
    │                        # against just an IOC prefix (subprocess mode)
    └── ipc_client.py        # UNIX-socket client for full-fidelity access
                             # to the camera's list_params/get/set surface

run_ioc.py                   # Main entry point
check_environment.py         # Environment / hardware checker
firebird-driver-rhel9-patch/ # Patched Active Silicon driver source
```

## Spinnaker (Oryx) notes

Direct-`ctypes` binding to `libSpinnaker_C.so.2`. No `PySpin` dependency
(Spinnaker Python wheels lag behind Python versions; ctypes works on 3.13).

- The library is picked up from `LD_LIBRARY_PATH` first, then falls back to
  the vendor path shipped with ADSpinnaker at
  `/APSshare/epics/synApps_6_2_1/support/areaDetector-R3-12-1/ADSpinnaker/spinnakerSupport/os/linux-x86_64/`.
  The loader pre-loads transitive C++ dependencies
  (`libLog_*`, `libMathParser_*`, `libGenApi_*`, `libSpinnaker.so.2`) with
  `RTLD_GLOBAL` so no shell-side `LD_LIBRARY_PATH` setup is required.
- Spinnaker claims the camera exclusively per process — stop any running
  ADSpinnaker C++ IOC (e.g. `32idbSP1`) before starting this Python one.
- Select a specific camera by serial: `--serial 23605513` (matches the
  ADSpinnaker `CAMERA_ID` convention).
- The backend exposes the *full* GenICam node map through `list_params()`
  (2000+ nodes on the Oryx). The GUI auto-groups leftovers by name prefix
  (Analog/Gain, Trigger, Chunk Data, Transport Layer, Device Info, …) so
  it stays browsable.
- ROI-affecting nodes (`Width`, `Height`, `OffsetX/Y`, `Binning*`,
  `PixelFormat`) are locked during acquisition by the SDK. `set_param` on
  those pauses acquisition, applies the write, and restarts — writes go
  through mid-run without the caller having to know.
- Pixel formats: only unpacked `Mono8` / `Mono16` are supported for
  acquisition. `Mono12Packed` raises a clear error at `start_acquisition` —
  set `PixelFormat=Mono16` from the GUI or via CA before starting.
- `SensorWidth`/`SensorHeight` (true sensor size) are preferred over
  `WidthMax`/`HeightMax` when populating `CameraInfo` — WidthMax is the
  max ROI *in the current binning mode* and misreports when binning > 1.

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