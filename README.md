# ORCA Fire Detector Control

Qt-based parameter control GUI for Hamamatsu ORCA Fire cameras using Euresys CoaXPress frame grabbers. Automatically falls back to simulation mode when no camera hardware is available.

## Setup

```bash
conda create -n detector python=3.11
conda activate detector
pip install numpy PyQt5

# For real camera support:
pip install harvesters

# For TIFF export:
pip install imageio
```

## Usage

```bash
python orca/qt_detector.py
```

With a specific GenTL producer path:

```bash
python orca/qt_detector.py --gentl-path /opt/euresys/GenTL/Producer/libEuresysGenTL.so
```

The GUI shows a green/red indicator for camera connection status. When no hardware is found, simulation mode generates test pattern frames.

## Files

| File | Purpose |
|------|---------|
| `orca/qt_detector.py` | Qt GUI for parameter control and status monitoring |
| `orca/dummy_detector.py` | Detector backend (real camera + simulation fallback) |
| `orca/harvesters_orca_fire.py` | Low-level camera acquisition via GenTL/harvesters |
