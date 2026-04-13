# ORCA Fire Detector Control

Hamamatsu ORCA Fire (C16240-20UP) camera control via Euresys Coaxlink Quad CXP-12
frame grabber, using Harvesters/GenTL. No DCAM-SDK required. Includes a standalone
Qt GUI, an areaDetector-compatible EPICS IOC for tomoscan, and PVAccess NTNDArray
streaming for pystream.

## Architecture

```
tomoscan / EPICS clients ──► caproto IOC (cam1: + HDF1: PVs)
                                │
pystream ──────────────────► pvapy NTNDArray server (image1:ArrayData)
                                │
Qt GUI ────────────────────► IOCBackend adapter
                                │
                           OrcaFireAcquirer
                                │
                           Harvesters / GenTL
                                │
                           Euresys coaxlink.cti
                                │
                           Coaxlink Quad CXP-12 frame grabber
                                │
                           CoaXPress link (CXP-6 x4)
                                │
                           Hamamatsu ORCA Fire C16240-20UP
```

## Setup

```bash
conda create -n detector python=3.11
conda activate detector
pip install numpy PyQt5 harvesters caproto h5py pvapy

# Optional:
pip install imageio   # TIFF export from GUI
pip install pyqtgraph # GUI plotting
```

### Hardware requirements

- **Frame grabber**: Euresys Coaxlink Quad CXP-12 (PCIe card)
  - 6-pin PCIe auxiliary power cable MUST be connected
  - eGrabber driver + GenTL producer installed at `/opt/euresys/egrabber/`
  - Firmware must be current (run `coaxlink-firmware update` if status shows "TooOld")
- **Camera**: Hamamatsu ORCA Fire C16240-20UP
  - 4 CoaXPress cables connecting camera ports 1-4 to frame grabber ports A-D
  - External power supply connected (camera does not use PoCXP)
- **CXP cables**: CXP-12 rated cables recommended. CXP-6 cables work for data
  transfer but the link stays in "Detected" state, which limits register write
  access (trigger mode, binning via camera). Frame grabber FPGA binning and
  exposure time control work regardless.

## Running

### EPICS IOC (headless, for tomoscan)

```bash
python run_orca_ioc.py --prefix ORCA:
```

### EPICS IOC + Qt GUI

```bash
python run_orca_ioc.py --prefix ORCA: --gui
```

### Standalone Qt GUI (no EPICS)

```bash
python orca/qt_detector.py
```

### List all served PVs

```bash
python run_orca_ioc.py --list-pvs
```

## EPICS PVs

### cam1: PVs (areaDetector camera driver)

| PV | Type | Description |
|----|------|-------------|
| `cam1:Acquire` | int | 1=start, 0=stop |
| `cam1:AcquireTime` | float | Exposure time in seconds |
| `cam1:AcquireTime_RBV` | float | Readback |
| `cam1:ImageMode` | str | "Single", "Multiple", "Continuous" |
| `cam1:NumImages` | int | Frames to acquire (Multiple mode) |
| `cam1:NumImagesCounter_RBV` | int | Frames acquired so far |
| `cam1:ArrayCounter_RBV` | int | Total frame counter |
| `cam1:ArraySize0_RBV` | int | Image width (pixels) |
| `cam1:ArraySize1_RBV` | int | Image height (pixels) |
| `cam1:TriggerMode` | str | "Off" (free-run), "On" (external) |
| `cam1:TriggerSoftware` | int | Write 1 to fire software trigger |
| `cam1:BinX` / `cam1:BinY` | int | Binning factor (1, 2, or 4) |
| `cam1:ArrayData` | int[] | Raw frame data (CA, flattened) |

### HDF1: PVs (areaDetector HDF5 file writer)

| PV | Type | Description |
|----|------|-------------|
| `HDF1:Capture` | int | 1=start capture, 0=stop |
| `HDF1:FilePath` | str | Output directory |
| `HDF1:FileName` | str | Base filename |
| `HDF1:FileNumber` | int | Current file number |
| `HDF1:NumCapture` | int | Frames to capture |
| `HDF1:NumCaptured_RBV` | int | Frames written so far |
| `HDF1:FullFileName_RBV` | str | Full path of current file |
| `HDF1:FilePathExists_RBV` | int | 1 if output directory exists |

HDF5 files are written with dataset `/exchange/data` of shape `(N, height, width)`, dtype `uint16`.

### PVAccess (pystream)

Frames are published as NTNDArray on:

```
ORCA:image1:ArrayData
```

Use with pystream:
```bash
python pyqtstream.py --pv ORCA:image1:ArrayData
```

Throttled to 30 fps for display. Full-speed acquisition continues independently.

## tomoscan integration

### EPICS database substitutions

```
CameraPVPrefix      = ORCA:
FilePluginPVPrefix  = ORCA:HDF1:
```

### Step-scan workflow

tomoscan controls the camera through standard areaDetector PVs:

1. Set `cam1:ImageMode` = "Multiple"
2. Set `cam1:NumImages` = N (number of projections)
3. Set `cam1:AcquireTime` = exposure time in seconds
4. Set `HDF1:FilePath`, `HDF1:FileName`, `HDF1:NumCapture`
5. Set `HDF1:Capture` = 1
6. Set `cam1:Acquire` = 1
7. Monitor `cam1:Acquire` — goes to 0 when done
8. Set `HDF1:Capture` = 0 to close file

### Trigger modes

| Mode | `cam1:TriggerMode` | Behavior |
|------|-------------------|----------|
| Free-run | "Off" | Continuous acquisition at max frame rate |
| External | "On" | Frame grabber gates to external trigger on LIN1 (TTLIO11) |
| Software | via `TriggerSoftware` | Fire individual frames with `caput cam1:TriggerSoftware 1` |

External trigger input is configured on the Euresys frame grabber's TTLIO11 line
(default, rising edge). The camera can also accept triggers directly on its BNC
trigger port.

## Camera parameters

### Controllable (live, no restart needed)

| Parameter | Range | Notes |
|-----------|-------|-------|
| ExposureTime | ~7.3 us - 10 s | Set in seconds, applied to camera GenICam node |
| Binning | 1, 2, 4 | Via frame grabber FPGA (Mean method). Stops/restarts acquisition to apply |

### Read-only (hardware fixed)

| Parameter | Value | Notes |
|-----------|-------|-------|
| Width | 4480 px | Full sensor, changes with binning (4480/2240/1120) |
| Height | 2368 px | Full sensor, changes with binning (2368/1184/592) |
| PixelFormat | Mono16 | 16-bit unsigned |
| Max frame rate | ~110 fps | At full resolution, CXP-6 x4 |

### Not available via CXP/GenTL

The following parameters require DCAM-SDK and are not controllable through the
CoaXPress GenTL interface:

- Sensor temperature readout
- Cooler control
- Readout speed / direction
- Shutter mode (rolling/global)
- Sub-array / ROI
- Camera-side binning
- Advanced trigger configuration (polarity, delay, multi-gate)

The camera's CXP GenICam XML (`HPK_C16240-20UP_Rev001.xml`) only exposes 8 nodes:
Width, Height, PixelFormat, ExposureTime, AcquisitionMode, DeviceScanType,
TapGeometry, Img1StreamId.

## Frame grabber setup notes

### Firmware update

If `check_environment.py` or `gentl info` reports firmware status "TooOld":

```bash
sudo /opt/euresys/egrabber-linux-x86_64-25.12.1.16/firmware/coaxlink-firmware update --card=coaxlink:0
```

Requires a full power cycle (not just reboot) after update.

### CXP link status

Check link status:
```bash
/opt/euresys/egrabber/bin/x86_64/gentl genapi --module=if --get CxpConnectionState[A]
```

- **Connected**: Full CXP link — all features available
- **Detected**: Degraded link (CXP-6 cables) — data streaming works, camera register writes blocked

### Data stream configuration

When the CXP link is in "Detected" state, the frame grabber may not auto-detect
the camera's resolution. The acquirer automatically configures the data stream
by setting `ImageFormatSource=DataStream` and matching `RemoteWidth`/`RemoteHeight`
to the camera's reported dimensions.

## Environment check

```bash
python check_environment.py
```

Verifies: Python packages, GenTL producer, frame grabber driver, camera detection.

## Files

| File | Purpose |
|------|---------|
| `run_orca_ioc.py` | EPICS IOC entry point (headless or with GUI) |
| `orca/epics_ad_server.py` | caproto PV server: CamPlugin + HDF5Plugin + NTNDArrayServer + OrcaFireIOC |
| `orca/ioc_backend.py` | Adapter between IOC and Qt GUI (IOCBackend) |
| `orca/qt_detector.py` | Qt GUI for parameter control and monitoring |
| `orca/dummy_detector.py` | Detector backend with simulation fallback (DummyOrcaFireDetector) |
| `orca/harvesters_orca_fire.py` | Low-level camera control via Harvesters/GenTL (OrcaFireAcquirer) |
| `check_environment.py` | Pre-flight hardware and software checker |
