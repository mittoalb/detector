# detectors — agent documentation

Camera-agnostic EPICS areaDetector-style IOC + Qt GUI for scientific
cameras (Hamamatsu ORCA Fire, Teledyne FLIR Oryx, Photometrics
Kinetix, plus a synthetic `simulator` backend for testing).

Two agent-facing entry points, both additive to the existing tooling:

1. **`detectors-cli`** — argparse CLI for INTROSPECTION (list backends,
   probe SDKs, auto-detect, list PVs, echo the IOC command line).
2. **`from detectors import headless`** — the same operations
   in-process.

The **IOC itself** is still launched via `python run_ioc.py …` — this
is a long-running process, not something the agent starts casually.
The `detectors-cli ioc-cmd` subcommand prints the exact command; the
agent (or user) runs it separately.

## What detectors is good for from the agent's perspective

- "What camera backends does this host support?"
- "Is the Kinetix SDK installed here?"
- "What's actually plugged in — try auto-detect."
- "What PVs would the IOC expose under prefix `DET:`?"
- "Give me the command to start the IOC for the Kinetix in GUI mode."

Do NOT use this to actually START the IOC from an agent turn without
explicit permission — starting an IOC is a long-lived process that
grabs the camera hardware. That's a scientist decision, not an agent
one.

## `detectors-cli` — one-shot introspection

```bash
detectors-cli list-backends [--json]
detectors-cli probe [--json]
detectors-cli detect [--json]
detectors-cli pvs --prefix DET: [--json]
detectors-cli ioc-cmd --camera teledyne.kinetix [--prefix K:] [--gui]
```

Every command accepts `--json` for machine parsing. Nonzero exit on
failure. Concrete examples:

**"What backends does this install support?"**
```
$ detectors-cli list-backends
  ✓ hamamatsu.orca_fire_dcam       Hamamatsu ORCA Fire (DCAM)
  ✗ hamamatsu.orca_fire_gentl      hamamatsu.orca_fire_gentl  (ModuleNotFoundError: No module named 'harvesters')
  ✓ teledyne.oryx                  Teledyne FLIR Oryx (Spinnaker)
  ✗ teledyne.kinetix               teledyne.kinetix           (ModuleNotFoundError: No module named 'pyvcam')
  ✓ simulator                      Simulated Camera
```

**"Which vendor SDKs are installed?"**
```
$ detectors-cli probe
Python: 3.10.14
Packages:
  ✓ numpy        1.26.4
  ✓ PyQt5        installed
  ✗ harvesters   (missing)
  ✓ imageio      2.36.1
  ...
GenTL producers found:
  ✓ /opt/euresys/egrabber/lib/x86_64/coaxlink.cti
Backends importable on this host:
  ✓ hamamatsu.orca_fire_dcam ...
```

**"What's plugged in right now?"**
```
$ detectors-cli detect
  ✓ auto-detected: teledyne.kinetix  (Teledyne Photometrics Kinetix (PVCAM))
```
Skips the `simulator` backend deliberately so a fake-data fallback
never masks a real hardware problem.

**"What PVs will the IOC expose?"**
```
$ detectors-cli pvs --prefix DET:
  DET:cam1:AcquireTime
  DET:cam1:Acquire
  DET:cam1:Image:PVA
  DET:HDF1:FileName
  ...
```
Instantiates the IOC in-process against a `simulator` camera so we
get the actual pvproperty names, not a hand-maintained list.

**"How do I start the IOC for the Kinetix?"**
```
$ detectors-cli ioc-cmd --camera teledyne.kinetix --prefix K: --gui
/path/to/python /path/to/run_ioc.py --camera teledyne.kinetix --prefix K: --gui
```
Prints only — does NOT run. Copy-paste to launch, or wrap in
`bash: $(detectors-cli ioc-cmd --camera … )` if you truly want the
agent to start an IOC (only after user confirms).

## Python API

```python
from detectors import headless as h

h.list_backends()          # list of {camera_type, display_name,
                           #  importable, import_error, module}
h.list_importable_types()  # just the camera_type slugs that imported
h.probe_environment()      # dict: python + packages + GenTL + backends
h.try_auto_detect()        # {"found": <type or None>, "reason": ...}
h.list_pvs("DET:")         # ['DET:cam1:AcquireTime', ...]
```

`list_backends()` is safe on any host — reports every backend the
package knows about with per-backend import status. Missing vendor
SDKs show up as `importable=False` with the exact `ImportError`.

`try_auto_detect()` will actually TALK to hardware (opens the camera
long enough to identify it, then closes). Cheap on a working camera,
takes a few seconds if it has to time out on a missing one. Safe to
call — never raises, never holds the device.

## Camera backends shipped

| `camera_type`                    | Camera                              | SDK / driver              |
|----------------------------------|-------------------------------------|---------------------------|
| `hamamatsu.orca_fire_dcam`       | Hamamatsu ORCA Fire (C16240-20UP)   | DCAM-API + FireBird       |
| `hamamatsu.orca_fire_gentl`      | Hamamatsu ORCA Fire                 | Harvesters / GenTL / Euresys Coaxlink |
| `teledyne.oryx`                  | Teledyne FLIR Oryx (10GigE / CXP)   | Spinnaker C API (ctypes; no PySpin) |
| `teledyne.kinetix`               | Teledyne Photometrics Kinetix       | PVCAM + PyVCAM            |
| `simulator`                      | Synthetic frames                    | none — built in           |

At **APS 32-ID** specifically, this package is what `ioc32idbSP1`
(ORYX), `ioc32idbSP2` (Blackfly), and `ioc32Kinetix` are running on
their respective hosts (see `iocs_monitor` for host / start-stop
info, and `~/.pystream/procedures/bl32ID/iocs/summary.md` for the
IP + hardware map).

## Running the IOC (out of scope for this doc, but for reference)

```bash
# Auto-detect what's plugged in
python /path/to/run_ioc.py --prefix DET:

# Force a specific backend
python /path/to/run_ioc.py --camera teledyne.kinetix --prefix K: --gui

# Simulator, no hardware needed
python /path/to/run_ioc.py --camera simulator --prefix SIM: --gui
```

The IOC is long-running (blocks until Ctrl-C or SIGTERM). To manage
it as a service, use `iocs_monitor` — the SP1/SP2/Kinetix entries
in the 32-ID `iocs_monitor` config point at scripts that ultimately
invoke this run_ioc.py.

## Install

```bash
pip install -e /home/beams/AMITTONE/Software/detectors
# Add whichever camera extras you need:
pip install -e ".[camera]"    # harvesters (GenTL cameras)
pip install -e ".[epics]"     # caproto + h5py (needed for the IOC)
pip install -e ".[save]"      # imageio + tifffile (TIFF / PNG export)
```

That installs both the `detectors-cli` console script and makes
`from detectors import headless` importable.

## Common gotchas

- **`import_error` on a backend** usually means the vendor SDK
  isn't installed on this host (DCAM, Spinnaker, PVCAM, Harvesters).
  Not a bug — installing the SDK fixes it. Report the specific
  `ImportError` back to the user; don't guess.
- **`detect` never returns `simulator`.** Deliberate — a fake-data
  fallback masking a real hardware problem is worse than a clean
  "no camera" result. Pass `--camera simulator` explicitly if you
  want it.
- **`list_pvs` needs `caproto` + `h5py`.** Those are `[epics]`
  optional deps. Missing them → the command raises ImportError.
- **The IOC grabs the camera exclusively.** If auto-detect / probe
  fails with "device in use", another IOC or vendor tool has the
  camera open. Stop that first.
- **`run_ioc.py` isn't a console script (yet).** Invoke as
  `python /path/to/run_ioc.py` or use `detectors-cli ioc-cmd` to
  print the exact command line.

## Files touched by an agent

- Read: nothing on the filesystem (all queries are in-memory).
- Write: nothing.
- Fire-and-forget: `try_auto_detect()` briefly opens each camera
  backend during probing (opens + closes; never holds the device).
- `list_pvs()` instantiates the IOC's PV database in-process against
  the simulator backend — no ports bound, no CA server started.
