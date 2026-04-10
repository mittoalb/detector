# ORCA Fire Detector Control

Hamamatsu ORCA Fire camera control via Euresys CoaXPress frame grabbers, using harvesters/GenTL (no DCAM-SDK). Includes a standalone Qt GUI and an areaDetector-compatible EPICS IOC for tomoscan integration.

## Setup

```bash
conda create -n detector python=3.11
conda activate detector
pip install numpy PyQt5 harvesters

# For EPICS IOC / tomoscan support:
pip install caproto h5py

# For TIFF export from GUI:
pip install imageio
```

## Standalone Qt GUI

```bash
python orca/qt_detector.py
```

Falls back to simulation mode when no camera is connected. The GUI shows a green/red indicator for camera status.

## EPICS IOC for tomoscan

Start the areaDetector-compatible PV server:

```bash
python run_orca_ioc.py --prefix ORCA:
```

This serves standard `cam1:` and `HDF1:` PVs that tomoscan expects:

```bash
# List all PVs
python run_orca_ioc.py --list-pvs

# Example PVs served:
#   ORCA:cam1:Acquire           Start/stop acquisition
#   ORCA:cam1:AcquireTime       Exposure time (seconds)
#   ORCA:cam1:TriggerMode       Off / On (external)
#   ORCA:cam1:TriggerSoftware   Software trigger (for step scan)
#   ORCA:HDF1:Capture           Start/stop HDF5 capture
#   ORCA:HDF1:FilePath          Output directory
#   ORCA:HDF1:FileName          Output filename base
```

### tomoscan configuration

In the tomoscan EPICS database substitutions:

```
CameraPVPrefix      = ORCA:
FilePluginPVPrefix  = ORCA:HDF1:
```

Step scan mode (`TomoScanSTEP`) uses `cam1:TriggerSoftware` to fire each frame.

## Environment check

Verify all dependencies and hardware on the target machine:

```bash
python check_environment.py
```

## Files

| File | Purpose |
|------|---------|
| `orca/qt_detector.py` | Qt GUI for parameter control and status monitoring |
| `orca/dummy_detector.py` | Detector backend (real camera + simulation fallback) |
| `orca/harvesters_orca_fire.py` | Low-level camera acquisition via GenTL/harvesters |
| `orca/epics_ad_server.py` | areaDetector-compatible EPICS PV server (cam1 + HDF1) |
| `run_orca_ioc.py` | EPICS IOC runner script |
| `check_environment.py` | Pre-flight environment checker |
