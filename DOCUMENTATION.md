# ORCA Fire Detector System - Complete Documentation

## Overview

This package provides a complete EPICS areaDetector-like system for Hamamatsu ORCA Fire cameras using Euresys CoaXPress frame grabbers. The system includes:

- **Real-time camera acquisition** via GenTL/harvesters
- **EPICS PV server** with areaDetector-compatible interface
- **NTNDArray streaming** for live viewing with pystream
- **Simulation mode** for testing without hardware
- **Dummy detector GUI** for parameter testing

**Key Features:**
- Full areaDetector parameter control (exposure, binning, ROI, etc.)
- Real-time image streaming to EPICS clients
- pystream-compatible NTNDArray output
- Hardware simulation for development/testing
- Threaded acquisition with callback-based streaming

## Quick Start

### Simulation Mode (No Hardware Required)
```bash
# Install dependencies
conda create -n detector python=3.11
conda activate detector
pip install harvesters caproto numpy imageio pystream

# Run EPICS server with simulation
python run_orca_epics.py

# View stream in another terminal
pystream ORCA:
```

### Real Hardware Setup
```bash
# 1. Install Euresys eGrabber software
# 2. Connect ORCA Fire camera to CoaXPress frame grabber
# 3. Run the server
python run_orca_epics.py
```

## Components

### Core Files

| File | Purpose |
|------|---------|
| `orca/dummy_detector.py` | GUI-based dummy detector for testing |
| `orca/harvesters_orca_fire.py` | Real camera acquisition using GenTL |
| `orca/harvesters_epics_area_detector.py` | EPICS PV server with NDArray streaming |
| `run_orca_epics.py` | Command-line runner for EPICS server |
| `README.md` | Basic usage instructions |

### Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  ORCA Fire      │    │  Euresys Frame   │    │  Target Machine  │
│  Camera         │────│  Grabber (PCIe) │────│  (Linux)         │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                                       │
┌─────────────────┐    ┌──────────────────┐           │
│  GenTL Producer │    │  harvesters      │           │
│  (Euresys)      │────│  (Python)        │           │
└─────────────────┘    └──────────────────┘           │
                                                       │
┌─────────────────┐    ┌──────────────────┐    ┌──────┴─────┐
│  caproto        │    │  EPICS PVs       │    │  pystream  │
│  (PV Server)    │────│  (areaDetector)  │────│  (Viewer)  │
└─────────────────┘    └──────────────────┘    └────────────┘
```

## Hardware Requirements

### Minimum Hardware
- **ORCA Fire Camera** (Hamamatsu)
- **Euresys CoaXPress Frame Grabber** (PCIe card)
- **Linux Machine** with PCIe slot for frame grabber
- **CoaXPress Cables** (camera to frame grabber)

### Software Requirements
- **Linux OS** (Ubuntu 20.04+, CentOS 7+, etc.)
- **Python 3.11+**
- **Euresys eGrabber Software** (GenTL producer + drivers)
- **PCIe Drivers** for frame grabber card

## Installation

### 1. System Dependencies
```bash
# Install system packages (Ubuntu/Debian)
sudo apt-get update
sudo apt-get install python3-dev build-essential

# Install system packages (CentOS/RHEL)
sudo yum groupinstall "Development Tools"
sudo yum install python3-devel
```

### 2. Euresys eGrabber Installation
```bash
# Download from: https://www.euresys.com/en/Products/Software/eGrabber
# Install with default options (installs to /opt/euresys/)

# Verify installation
/opt/euresys/eGrabber/bin/get_genicam_version
/opt/euresys/eGrabber/bin/enumerate_devices
```

### 3. Python Environment
```bash
# Create conda environment
conda create -n detector python=3.11
conda activate detector

# Install Python packages
pip install harvesters caproto numpy imageio pystream

# Optional: Install additional tools
pip install matplotlib  # For debugging
pip install opencv-python  # For image processing
```

### 4. Code Installation
```bash
# Clone or copy the detector code to target machine
cd /path/to/detectors

# Make scripts executable
chmod +x run_orca_epics.py
```

## Usage

### Command Line Options

```bash
python run_orca_epics.py --help
```

Options:
- `--prefix PREFIX`: EPICS PV prefix (default: ORCA:)
- `--gentl-path PATH`: Path to GenTL producer library
- `--device-index N`: Camera device index (default: 0)
- `--log-level LEVEL`: Logging level (DEBUG, INFO, WARNING, ERROR)

### EPICS PV Reference

#### Control PVs
| PV Name | Type | Description |
|---------|------|-------------|
| `{prefix}:Acquire` | int | Start/stop acquisition (0=stop, 1=start) |
| `{prefix}:ExposureTime` | float | Exposure time (microseconds) |
| `{prefix}:AcquireTime` | float | Same as ExposureTime |
| `{prefix}:AcquisitionFrameRate` | float | Target frame rate (Hz) |
| `{prefix}:Width` | int | Image width (pixels) |
| `{prefix}:Height` | int | Image height (pixels) |
| `{prefix}:Binning` | int | Binning factor |
| `{prefix}:ImageMode` | int | Acquisition mode |
| `{prefix}:NumImages` | int | Number of images to acquire |

#### Status PVs
| PV Name | Type | Description |
|---------|------|-------------|
| `{prefix}:StatusMessage` | str | Current status ("Idle", "Acquiring") |
| `{prefix}:ImageCounter` | int | Frame counter |
| `{prefix}:FrameRate` | float | Actual frame rate |
| `{prefix}:CameraModel` | str | Camera model name |
| `{prefix}:CameraSerial` | str | Camera serial number |

#### NDArray PVs (for pystream)
| PV Name | Type | Description |
|---------|------|-------------|
| `{prefix}:ArrayData` | uint16[] | Flattened image data |
| `{prefix}:ArraySize` | int32[] | Array size [total_elements] |
| `{prefix}:ArraySize0` | int | Width |
| `{prefix}:ArraySize1` | int | Height |
| `{prefix}:ArraySize2` | int | Depth (0 for 2D) |
| `{prefix}:ColorMode` | int | Color mode (0=mono) |
| `{prefix}:DataType` | int | Data type (1=uint16) |
| `{prefix}:UniqueId` | int | Frame ID |
| `{prefix}:TimeStamp` | int32[] | Frame timestamp [seconds, nanoseconds] |

### Example Usage

#### 1. Basic Simulation
```bash
# Start server in simulation mode
python run_orca_epics.py

# Server output:
# INFO: Starting ORCA Fire EPICS server with prefix 'ORCA:'
# WARNING: Could not initialize camera: Could not locate GenTL producer. Running in simulation mode.
# INFO: EPICS server starting... Connect with pystream: pystream ORCA:
```

#### 2. Real Hardware
```bash
# Specify GenTL path if not in default location
python run_orca_epics.py --gentl-path /opt/euresys/GenTL/Producer/libEuresysGenTL.so

# Use different PV prefix
python run_orca_epics.py --prefix CAM1:
```

#### 3. Viewing with pystream
```bash
# Basic viewing
pystream ORCA:

# With custom settings
pystream --pv-prefix ORCA: --update-rate 30
```

#### 4. EPICS CA Client Access
```bash
# Check PVs with caget
caget ORCA:Acquire ORCA:ExposureTime ORCA:ImageCounter

# Set parameters with caput
caput ORCA:ExposureTime 1000
caput ORCA:Acquire 1

# Monitor values
camonitor ORCA:ImageCounter
```

### Dummy Detector GUI

For testing parameter control without EPICS:

```bash
python orca/dummy_detector.py
```

**New Modern Interface Features:**
- **📹 Live View Tab**: Real-time image display with downsampled preview
- **⚙️ Parameters Tab**: Organized parameter controls in logical groups:
  - Timing (Exposure, Frame Rate, etc.)
  - Image Size & ROI (Width, Height, Binning, etc.)
  - Acquisition Control (Trigger modes, Image count)
  - Image Processing (Gain, Gamma, Readout speed)
- **📊 Status Tab**: Live status display and activity logging
- **🎛️ Control Bar**: Quick access buttons for Start/Stop/Single Frame
- **💾 Save Frame**: Export acquired frames as TIFF files
- **🔄 Real-time Updates**: Parameters and status update automatically

**Key Improvements:**
- Modern tabbed interface similar to pystream
- Live image preview with intensity scaling
- Organized parameter groups with units
- Comprehensive logging and status display
- Save functionality for captured frames

## Architecture Details

### Data Flow

1. **Camera Acquisition**: harvesters uses GenTL to communicate with frame grabber
2. **Frame Processing**: Images acquired in background thread with callbacks
3. **EPICS Publishing**: caproto server publishes frames as NTNDArray PVs
4. **Client Viewing**: pystream subscribes to PVs and displays live stream

### Threading Model

- **Main Thread**: EPICS PV server, parameter handling
- **Acquisition Thread**: Camera frame acquisition loop
- **Callback Thread**: Frame processing and PV updates

### Memory Management

- **Frame Buffers**: Managed by harvesters/GenTL producer
- **PV Backlog**: Limited to prevent memory issues (default: 10 frames)
- **ArrayData PV**: Large waveform (4432×2369×2 = ~20MB per frame)

### Simulation Mode

When hardware unavailable:
- Generates synthetic test patterns
- Maintains same PV interface
- Allows full software testing
- Frame rate matches AcquisitionFrameRate setting

## Troubleshooting

### Common Issues

#### 1. "Could not locate GenTL producer"
**Cause**: Euresys software not installed or not in expected location
**Solution**:
```bash
# Check installation
ls -la /opt/euresys/GenTL/Producer/

# Specify path explicitly
python run_orca_epics.py --gentl-path /path/to/libEuresysGenTL.so
```

#### 2. "No devices found"
**Cause**: Camera not connected or frame grabber not detected
**Solution**:
```bash
# Check hardware
/opt/euresys/eGrabber/bin/enumerate_devices

# Verify PCIe card
lspci | grep Euresys
```

#### 3. pystream connection issues
**Cause**: Network configuration or PV prefix mismatch
**Solution**:
```bash
# Check EPICS server is running
netstat -tlnp | grep 5064

# Verify PVs exist
caget ORCA:Acquire
```

#### 4. Memory issues
**Cause**: Large frame sizes with high backlog
**Solution**:
- Reduce image size (Width/Height)
- Use binning to reduce resolution
- Monitor memory usage

### Debug Mode

```bash
# Enable debug logging
python run_orca_epics.py --log-level DEBUG

# Check GenTL devices
python -c "
from orca.harvesters_orca_fire import OrcaFireAcquirer
acq = OrcaFireAcquirer()
print('Devices:', acq.list_devices())
"
```

### Performance Tuning

- **Frame Rate**: Limited by camera capabilities and processing
- **Network**: Use dedicated network for EPICS traffic
- **CPU**: Multi-core recommended for real-time processing
- **Memory**: 8GB+ recommended for full resolution

## API Reference

### OrcaFireAcquirer Class

```python
class OrcaFireAcquirer:
    def __init__(self, gen_tl_producer_path=None, device_index=0)
    def open(self) -> None
    def close(self) -> None
    def configure(self, config: Dict[str, float]) -> None
    def start_acquisition(self) -> None
    def stop_acquisition(self) -> None
    def acquire_frame(self, timeout_ms=1000) -> np.ndarray
    def register_frame_callback(self, callback: Callable[[np.ndarray, Dict], None]) -> None
    def start_stream(self, timeout_ms=1000) -> None
    def stop_stream(self) -> None
```

### OrcaFireAreaDetector Class

```python
class OrcaFireAreaDetector(PVGroup):
    # Control PVs
    Acquire: pvproperty
    ExposureTime: pvproperty
    Width: pvproperty
    Height: pvproperty
    # ... (see PV reference above)

    def __init__(self, gen_tl_producer_path=None, device_index=0)
    def start_acquisition(self) -> None
    def stop_acquisition(self) -> None
```

## Development

### Code Structure

```
detectors/
├── orca/
│   ├── __init__.py
│   ├── dummy_detector.py          # GUI testing tool
│   ├── harvesters_orca_fire.py    # Camera backend
│   └── harvesters_epics_area_detector.py  # EPICS server
├── run_orca_epics.py              # CLI runner
└── README.md                      # Basic docs
```

### Adding New Features

1. **New PVs**: Add to OrcaFireAreaDetector class
2. **New Parameters**: Update configure() methods
3. **New Callbacks**: Extend frame processing
4. **GUI Elements**: Modify dummy_detector.py

### Testing

```bash
# Unit tests (if implemented)
python -m pytest

# Integration test
python run_orca_epics.py &
pystream ORCA:
```

## Support

### Resources
- **Euresys Documentation**: https://www.euresys.com/en/Support
- **caproto Documentation**: https://caproto.github.io/
- **pystream**: https://github.com/epics-modules/pystream
- **harvesters**: https://github.com/genicam/harvesters

### Version Information
- **Python**: 3.11+
- **caproto**: 1.3.0+
- **harvesters**: Latest
- **Euresys eGrabber**: Compatible with ORCA Fire

---

**Last Updated**: April 10, 2026
**Authors**: GitHub Copilot / AMITTONE Software Team</content>
<parameter name="filePath">/home/beams0/AMITTONE/Software/detectors/DOCUMENTATION.md