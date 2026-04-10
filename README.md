# ORCA Fire Detector System

This package provides EPICS areaDetector-like access to Hamamatsu ORCA Fire cameras using the Euresys GenTL producer and harvesters library.

## Components

- `orca/dummy_detector.py`: Software-only detector simulation for testing
- `orca/harvesters_orca_fire.py`: Real camera acquisition using harvesters/GenTL
- `orca/harvesters_epics_area_detector.py`: EPICS PV server with NDArray streaming
- `run_orca_epics.py`: Runner script for the EPICS server

## Setup

1. Create and activate the conda environment:
   ```bash
   conda create -n detector python=3.11
   conda activate detector
   ```

2. Install dependencies:
   ```bash
   pip install harvesters caproto numpy imageio
   ```

3. Install Euresys GenTL producer (for real camera access):
   - Download from Euresys website
   - Install to `/opt/euresys/` or set `EURESYS_GENTL_PRODUCER` environment variable

## Usage

### Testing with Dummy Detector

```bash
python orca/dummy_detector.py
```

This opens a GUI for testing parameter control without hardware.

### Real Camera with EPICS (Simulation Mode)

If the Euresys GenTL producer is not available, the server will run in simulation mode with generated test patterns:

```bash
python run_orca_epics.py
```

### Real Camera with EPICS

1. Install Euresys GenTL producer to `/opt/euresys/` or set `EURESYS_GENTL_PRODUCER` environment variable
2. Start the EPICS server:
   ```bash
   python run_orca_epics.py
   ```

3. In another terminal, view the stream with pystream:
   ```bash
   pystream ORCA:
   ```

### Available PVs

The server creates these PVs (with prefix `ORCA:` by default):

**Control PVs:**
- `Acquire`: Start/stop acquisition (0=stop, 1=start)
- `ExposureTime`: Exposure time in microseconds
- `AcquireTime`: Same as ExposureTime
- `AcquisitionFrameRate`: Target frame rate
- `Width/Height`: Image dimensions
- `Binning`: Binning factor
- `ImageMode`: Acquisition mode
- `NumImages`: Number of images to acquire

**Status PVs:**
- `StatusMessage`: Current status
- `ImageCounter`: Frame counter
- `FrameRate`: Actual frame rate
- `CameraModel/Serial`: Camera info

**NDArray PVs (for pystream):**
- `ArrayData`: Flattened image data (uint16)
- `ArraySize`: Total array size
- `ArraySize0/1/2`: Dimensions (width, height, depth)
- `ColorMode`: 0=mono
- `DataType`: 1=uint16
- `UniqueId`: Frame ID
- `TimeStamp`: Frame timestamp

## Architecture

The system uses:
- **harvesters**: GenTL consumer for camera access
- **caproto**: EPICS PV server framework
- **Threading**: Asynchronous acquisition with callbacks
- **NTNDArray**: EPICS standard for image data streaming

Frames are acquired in a background thread and published to EPICS PVs via callbacks, enabling real-time streaming to viewers like pystream.

**Simulation Mode**: When camera hardware is not available, the server generates synthetic test pattern frames for testing the EPICS interface and pystream integration.