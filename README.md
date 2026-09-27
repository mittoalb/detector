# detectors

Camera-agnostic EPICS areaDetector IOC and Qt GUI for scientific cameras.

**Supported cameras**

| Backend | Camera |
|---|---|
| `hamamatsu.orca_fire_dcam` / `hamamatsu.orca_fire_gentl` | Hamamatsu ORCA Fire |
| `teledyne.oryx` | Teledyne FLIR Oryx (10 GigE / CXP) |
| `teledyne.kinetix` | Teledyne Photometrics Kinetix |
| `simulator` | Synthetic frames (built in) |

## Quickstart

```bash
conda create -n detector python=3.11
conda activate detector
pip install numpy PyQt5 caproto h5py pvapy
pip install -e .

python run_ioc.py --list-cameras
python run_ioc.py --camera simulator --gui       # try without hardware
python run_ioc.py --camera teledyne.oryx --prefix ORYX:
```

Camera SDKs (Spinnaker, PVCAM, DCAM) install at system level from the
vendor. Backends bind directly via `ctypes` — no vendor Python wrappers.

## Documentation

Full docs (installation, per-camera notes, PVs, tomoscan integration,
architecture, extending): **[detectors.readthedocs.io]([https://detectors.readthedocs.io](https://detector.readthedocs.io/en/latest/))**

Build locally:

```bash
pip install -r docs/requirements.txt
cd docs && make html
xdg-open _build/html/index.html
```

## License

See `LICENSE`.
