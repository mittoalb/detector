"""`detectors-cli` — argparse CLI for the detectors headless API.

Introspection + probe operations only. The IOC itself is still
launched via `python run_ioc.py …` (or `detectors-ioc` if that
console-script is registered in pyproject); this CLI is the
agent-friendly way to answer "what cameras does this host support",
"what's actually plugged in", "what PVs will the IOC expose".

    detectors-cli list-backends [--json]     — every registered backend
    detectors-cli probe [--json]             — SDK / GenTL environment
    detectors-cli detect [--json]            — try auto_detect() on this host
    detectors-cli pvs --prefix DET: [--json] — PV schema for a prefix
    detectors-cli ioc-cmd --camera <type> [--prefix DET:] [--gui]
                                             — echoes the exact `run_ioc.py`
                                               command line for that backend
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


def _emit(payload: Any, as_json: bool, human=None) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return
    if human is not None:
        human(payload)
    else:
        print(payload)


# ── read commands ────────────────────────────────────────────────────

def _cmd_list_backends(args) -> int:
    from detectors import headless as h
    rows = h.list_backends()
    if args.json:
        _emit(rows, True); return 0
    w = max(len(r["camera_type"]) for r in rows)
    for r in rows:
        mark = "✓" if r["importable"] else "✗"
        note = "" if r["importable"] else f"  ({r['import_error']})"
        print(f"  {mark} {r['camera_type']:<{w}}  {r['display_name']}{note}")
    return 0


def _cmd_probe(args) -> int:
    from detectors import headless as h
    env = h.probe_environment()
    if args.json:
        _emit(env, True); return 0
    print(f"Python: {env['python']}")
    print("\nPackages:")
    for name, v in env["packages"].items():
        mark = "✓" if v else "✗"
        print(f"  {mark} {name:<12} {v or '(missing)'}")
    print("\nGenTL producers found:")
    if env["gentl_producers"]:
        for p in env["gentl_producers"]:
            print(f"  ✓ {p}")
    else:
        print("  (none — Euresys / Matrox / Pleora runtimes not installed)")
    print("\nBackends importable on this host:")
    for r in env["backends"]:
        mark = "✓" if r["importable"] else "✗"
        note = "" if r["importable"] else f"  ({r['import_error']})"
        print(f"  {mark} {r['camera_type']:<32} {r['display_name']}{note}")
    return 0


def _cmd_detect(args) -> int:
    from detectors import headless as h
    r = h.try_auto_detect()
    if args.json:
        _emit(r, True); return 0
    if r["found"]:
        print(f"  ✓ auto-detected: {r['found']}  ({r.get('display_name','')})")
        return 0
    print(f"  ✗ no camera found — {r['reason']}", file=sys.stderr)
    return 1


def _cmd_pvs(args) -> int:
    from detectors import headless as h
    try:
        pvs = h.list_pvs(prefix=args.prefix)
    except Exception as e:
        print(f"  ✗ list_pvs failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if args.json:
        _emit(pvs, True); return 0
    for pv in pvs:
        print(f"  {pv}")
    return 0


def _cmd_ioc_cmd(args) -> int:
    """Print (does NOT run) the run_ioc.py command that would start
    the IOC for the requested backend. Convenience for scripting —
    the user or agent then invokes it themselves."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run_ioc = os.path.join(here, "run_ioc.py")
    cmd = [sys.executable, run_ioc, "--camera", args.camera,
           "--prefix", args.prefix]
    if args.gui:
        cmd.append("--gui")
    if args.device_index is not None:
        cmd += ["--device-index", str(args.device_index)]
    if args.serial:
        cmd += ["--serial", args.serial]
    if args.json:
        _emit({"cmd": cmd, "run_ioc_path": run_ioc}, True); return 0
    print(" ".join(cmd))
    return 0


# ── argparse tree ────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="detectors-cli",
        description="Headless CLI for the `detectors` package "
                    "(camera backend introspection, SDK probe, "
                    "PV schema, auto-detect).")
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON on read commands.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list-backends",
                       help="Every registered camera backend + import status")
    p.set_defaults(func=_cmd_list_backends)

    p = sub.add_parser("probe",
                       help="Python packages + GenTL producers + backends")
    p.set_defaults(func=_cmd_probe)

    p = sub.add_parser("detect",
                       help="Try to auto-open a camera; report which backend "
                            "claimed it (or why none did)")
    p.set_defaults(func=_cmd_detect)

    p = sub.add_parser("pvs",
                       help="Print every PV the IOC would expose under a "
                            "given prefix (instantiates in-process; no IOC "
                            "actually launched)")
    p.add_argument("--prefix", default="DET:",
                   help="PV prefix (default: DET:)")
    p.set_defaults(func=_cmd_pvs)

    p = sub.add_parser("ioc-cmd",
                       help="Print the exact run_ioc.py command that would "
                            "start an IOC for the requested backend (does NOT "
                            "run it — the caller invokes)")
    p.add_argument("--camera", required=True,
                   help="Camera type (e.g. teledyne.kinetix, simulator)")
    p.add_argument("--prefix", default="DET:")
    p.add_argument("--gui", action="store_true",
                   help="Include --gui in the printed command")
    p.add_argument("--device-index", type=int, default=None)
    p.add_argument("--serial", default=None)
    p.set_defaults(func=_cmd_ioc_cmd)

    return ap


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
