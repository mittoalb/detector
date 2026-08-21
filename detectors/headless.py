"""Headless (non-IOC, non-GUI) API for the `detectors` package.

Everything an AI agent (or a script) needs to introspect the camera
backends, check which SDKs are actually installed on this host, and
attempt an auto-detect — without launching the EPICS IOC or the Qt
GUI. The IOC (`run_ioc.py`) and the Qt GUI are unchanged; this
module gives the agent one obvious import surface.

    from detectors import headless as h

    h.list_backends()          # every registered camera type
    h.probe_environment()      # which SDKs / packages are available
    h.try_auto_detect()        # which backend can actually open a camera
    h.list_pvs("DET:")         # PV names the IOC would expose
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional

from detectors.core import registry


# Camera backends the package ships. Kept in sync with registry.load_all()
# so we can report which ones failed to import (missing SDK, etc.)
_KNOWN_MODULES = [
    ("hamamatsu.orca_fire_dcam",  "detectors.cameras.hamamatsu.orca_fire_dcam"),
    ("hamamatsu.orca_fire_gentl", "detectors.cameras.hamamatsu.orca_fire_gentl"),
    ("teledyne.oryx",             "detectors.cameras.teledyne.oryx"),
    ("teledyne.kinetix",          "detectors.cameras.teledyne.kinetix"),
    ("simulator",                 "detectors.cameras.simulator"),
]


# ── backend enumeration ────────────────────────────────────────────

def list_backends() -> List[Dict[str, Any]]:
    """Return every camera backend the package knows about, with
    per-backend info about whether it imported successfully. Each
    entry:

        {"camera_type", "display_name", "module", "importable",
         "import_error"}

    `importable=False` almost always means the vendor SDK isn't
    installed on this host — Hamamatsu DCAM, Euresys GenTL,
    Spinnaker, or PVCAM. Read `import_error` for the exact cause."""
    out = []
    for ctype, mod_path in _KNOWN_MODULES:
        entry = {
            "camera_type":   ctype,
            "module":        mod_path,
            "display_name":  ctype,           # replaced below if import ok
            "importable":    False,
            "import_error":  None,
        }
        try:
            importlib.import_module(mod_path)
            entry["importable"] = True
            # Now the class has self-registered — pick up display_name
            try:
                cls = registry.get_class(ctype)
                entry["display_name"] = getattr(cls, "display_name", ctype)
            except KeyError:
                pass
        except Exception as e:
            entry["import_error"] = f"{type(e).__name__}: {e}"
        out.append(entry)
    return out


def list_importable_types() -> List[str]:
    """Convenience: just the `camera_type` slugs whose backend module
    imports successfully on this host."""
    return [b["camera_type"] for b in list_backends() if b["importable"]]


# ── SDK / dep environment ──────────────────────────────────────────

def probe_environment() -> Dict[str, Any]:
    """Report which Python packages + vendor SDKs are available.
    Same information as `check_environment.py` but as a dict so an
    agent can act on it programmatically.

    Shape:
        {
          "python":     "<version string>",
          "packages":   {"numpy": "1.26.4", "PyQt5": "5.15.10", ...,
                         "harvesters": None},   # None = missing
          "gentl_producers": [<path>, ...],     # paths that exist
          "backends":   [<same as list_backends()>],
        }"""
    import sys

    pkg_checks = [
        ("numpy",       "numpy"),
        ("PyQt5",       "PyQt5.QtCore"),
        ("harvesters",  "harvesters"),
        ("imageio",     "imageio"),
        ("tifffile",    "tifffile"),
        ("h5py",        "h5py"),
        ("caproto",     "caproto"),
    ]
    packages: Dict[str, Optional[str]] = {}
    for name, import_path in pkg_checks:
        try:
            mod = importlib.import_module(import_path)
            v = getattr(mod, "__version__", None) or "installed"
            packages[name] = v
        except Exception:
            packages[name] = None

    # Common GenTL producer paths — the file existing means the
    # Euresys / Matrox / Basler runtime is installed.
    import os
    gentl_candidates = [
        "/opt/euresys/egrabber/lib/x86_64/coaxlink.cti",
        "/opt/euresys/egrabber/lib/x86_64/grablink.cti",
        "/opt/matrix-vision/mvIMPACT_Acquire/lib/x86_64/mvGenTLProducer.cti",
        "/opt/pleora/ebus_sdk/RHEL-9-x86_64/lib/genicam/bin/Linux64_x64/PvGenTLProducer.cti",
    ]
    gentl_present = [p for p in gentl_candidates if os.path.isfile(p)]

    return {
        "python":           sys.version.split()[0],
        "packages":         packages,
        "gentl_producers":  gentl_present,
        "backends":         list_backends(),
    }


# ── auto-detect (opens a camera; safe to abort if none present) ────

def try_auto_detect(camera_types: Optional[List[str]] = None) -> Dict[str, Any]:
    """Attempt `registry.auto_detect()` and report which backend (if
    any) successfully opened a camera. Never raises — returns
    `{"found": None, "reason": <error>}` on any failure.

    The auto-detect logic in `registry.auto_detect` deliberately
    skips the `simulator` backend so a fake-data fallback never masks
    a real hardware problem."""
    # Ensure all camera modules are loaded / registered
    registry.load_all()
    try:
        cam = registry.auto_detect(camera_types=camera_types)
    except Exception as e:
        return {"found": None, "reason": f"{type(e).__name__}: {e}"}
    if cam is None:
        return {"found": None,
                "reason": "no backend could open a camera "
                          "(SDK missing, no device, or auth issue)"}
    # Close so we don't hold the device from the probe
    try:
        cam.close()
    except Exception:
        pass
    ctype = getattr(type(cam), "camera_type", "unknown")
    return {"found": ctype,
            "display_name": getattr(type(cam), "display_name", ctype),
            "reason": ""}


# ── PV-schema preview (no IOC launched) ────────────────────────────

def list_pvs(prefix: str = "DET:") -> List[str]:
    """Return the sorted list of PV names the IOC would expose under
    the given prefix. Instantiates the `DetectorIOC` in-process
    (with a dummy `simulator` camera when no camera is passed) so
    we get the actual pvproperty names, not a hand-maintained list.

    If instantiating the IOC fails (e.g. `caproto` not installed),
    raises the underlying import error — the caller can catch and
    report."""
    from detectors.server.ioc import DetectorIOC
    registry.load_all()
    cam = registry.create("simulator")
    ioc = DetectorIOC(prefix=prefix, camera=cam)
    pvs = sorted(str(pv.pvspec.name) for pv in ioc.pvdb.values())
    try:
        cam.close()
    except Exception:
        pass
    return pvs


__all__ = [
    "list_backends", "list_importable_types",
    "probe_environment",
    "try_auto_detect",
    "list_pvs",
]
