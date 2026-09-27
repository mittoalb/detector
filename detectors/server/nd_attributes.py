"""
NDAttributes: parse areaDetector-style Attributes.xml and maintain a
live cache of the referenced EPICS PV values, snapshottable per frame.

Modeled on synApps areaDetector NDPluginBase's attribute list, but only
supports the `type="EPICS_PV"` subset — the only kind of attribute the
32-ID TomoScanDetectorAttributes.xml uses in practice. PARAM / CONST /
DRV_INFO types are not implemented until a real XML needs them.

Lifetime: one manager per IOC, connect once at IOC start, keep monitors
alive across scans. Reloading the XML file (via cam1:NDAttributesFile
putter) discards the old subscription set and rebuilds.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import epics  # pyepics — proven-reliable CA client, also used by tomoscan

logger = logging.getLogger(__name__)


def default_xml_search_paths() -> List[str]:
    """Directories to search when a basename is put into the XML PVs and
    the caller hasn't configured any explicit paths.

    Covers the common synApps-on-a-beamline layout:
      $HOME/epics/synApps/support/<detector-ioc>/iocBoot/ioc<detector-ioc>/
    and its usertxm equivalent at APS beamlines.
    Non-existent dirs are silently dropped.
    """
    import glob
    globs = [
        # Current-user synApps installs
        os.path.expanduser("~/epics/synApps/support/*/iocBoot/ioc*"),
        # 32-ID usertxm install (referenced from every user's tomoscan)
        "/home/beams/USERTXM/epics/synApps/support/*/iocBoot/ioc*",
        "/home/beams19/USERTXM/epics/synApps/support/*/iocBoot/ioc*",
    ]
    found: List[str] = []
    seen = set()
    for pat in globs:
        for path in sorted(glob.glob(pat)):
            if path in seen:
                continue
            if os.path.isdir(path):
                found.append(path)
                seen.add(path)
    return found


# DBR type hints from the XML — map to a Python-side normalized dtype we
# then hand to h5py at write time.
DBR_TYPE_HINTS = {
    "DBR_STRING": "string",
    "DBR_DOUBLE": "float64",
    "DBR_LONG": "int32",
    "DBR_ENUM": "int32",       # numeric enum index
    "DBR_INT16": "int16",
    "DBR_FLOAT": "float32",
    "DBR_CHAR": "uint8",
    "DBR_NATIVE": "native",    # resolved from PV.channel.native_data_type
}


@dataclass
class AttributeSpec:
    """One parsed <Attribute> row."""
    name: str
    pvname: str                  # after macro substitution
    dbrtype_hint: str            # DBR_*
    description: str = ""
    raw_source: str = ""         # original source string, for logging


def _substitute_macros(source: str, macros: Dict[str, str]) -> str:
    """Replace every $(KEY) in source with macros[KEY]. Unknown macros
    left in place (caller detects them by re-scanning for '$(')."""
    out = source
    for key, value in macros.items():
        out = out.replace(f"$({key})", value)
    return out


def parse_attributes_xml(
    xml_path: str,
    macros: Optional[Dict[str, str]] = None,
) -> List[AttributeSpec]:
    """Parse an areaDetector NDAttributes XML file.

    Skips any Attribute whose `type` is not `EPICS_PV`, and any whose
    `source` still contains an unresolved `$(...)` macro after substitution
    (with a WARNING per skipped attribute).
    """
    macros = macros or {}
    tree = ET.parse(xml_path)
    root = tree.getroot()
    if root.tag != "Attributes":
        raise ValueError(
            f"{xml_path}: root element is <{root.tag}>, expected <Attributes>")

    specs: List[AttributeSpec] = []
    n_skipped_type = 0
    n_skipped_macro = 0

    for elem in root.findall("Attribute"):
        atype = elem.get("type", "")
        if atype != "EPICS_PV":
            n_skipped_type += 1
            continue

        name = elem.get("name")
        source = elem.get("source", "")
        if not name or not source:
            logger.warning(
                "%s: skipping Attribute with missing name/source: %s",
                xml_path, ET.tostring(elem, encoding="unicode").strip())
            continue

        pvname = _substitute_macros(source, macros)
        if "$(" in pvname:
            logger.warning(
                "%s: attribute %s has unresolved macro in source=%r; skipping",
                xml_path, name, source)
            n_skipped_macro += 1
            continue

        specs.append(AttributeSpec(
            name=name,
            pvname=pvname,
            dbrtype_hint=elem.get("dbrtype", "DBR_NATIVE"),
            description=elem.get("description", ""),
            raw_source=source,
        ))

    logger.info(
        "%s: parsed %d attributes (skipped %d non-EPICS_PV, %d unresolved-macro)",
        xml_path, len(specs), n_skipped_type, n_skipped_macro)
    return specs


class NDAttributesManager:
    """Owns pyepics subscriptions for a set of NDAttributes, keeps a live
    cache of their latest values, and exposes a non-blocking snapshot().

    Thread-safe. Value updates arrive on pyepics's CA callback thread;
    snapshot() can be called from any thread (typically the camera
    acquisition loop).

    Why pyepics and not caproto's threading client? caproto's client
    inside a caproto server process failed to connect to remote PVs in
    the 32-ID environment (0/174 connected). pyepics is what tomoscan
    uses successfully from the same shell, so we know it works.
    """

    def __init__(
        self,
        macros: Optional[Dict[str, str]] = None,
        search_paths: Optional[List[str]] = None,
    ):
        self._macros = dict(macros or {})
        self._search_paths = list(search_paths or [])
        # Value cache: attr_name -> (value, timestamp, ever_connected)
        self._values: Dict[str, Tuple[object, float, bool]] = {}
        self._lock = threading.Lock()
        # pyepics PV objects, one per attribute
        self._pvs: Dict[str, epics.PV] = {}
        self._specs: List[AttributeSpec] = []
        # Frame counter — synApps treats NDArrayUniqueId as an implicit
        # driver-provided attribute; caller feeds it via update_uid().
        self._uid: int = 0
        # Status
        self._loaded_path: Optional[str] = None
        self._load_error: Optional[str] = None

    # ---------------- lifecycle ---------------------------------------

    def load(self, xml_file: str) -> None:
        """Load (or reload) the given XML file. Basename is resolved via
        AREA_DETECTOR_ATTRIBUTES_PATH env, then the manager's search_paths,
        then CWD, then absolute if it exists.

        Raises RuntimeError if the file cannot be found or parsed.
        """
        try:
            resolved = self._resolve_path(xml_file)
        except FileNotFoundError as exc:
            self._load_error = str(exc)
            raise RuntimeError(str(exc)) from exc

        try:
            specs = parse_attributes_xml(resolved, self._macros)
        except ET.ParseError as exc:
            self._load_error = f"{resolved}: XML parse error: {exc}"
            raise RuntimeError(self._load_error) from exc

        self._teardown_subscriptions()
        self._specs = specs
        self._loaded_path = resolved
        self._load_error = None
        with self._lock:
            self._values = {s.name: (None, 0.0, False) for s in specs}

        self._create_subscriptions()

    def close(self) -> None:
        """Disconnect all PVs. Idempotent."""
        self._teardown_subscriptions()

    # ---------------- data access -------------------------------------

    def snapshot(self, uid: Optional[int] = None) -> Dict[str, object]:
        """Return a shallow copy of the current cached values.

        If `uid` is provided, `NDArrayUniqueId` is included as an implicit
        attribute (matches areaDetector's driver-provided attribute).
        Never blocks on network I/O; disconnected PVs surface as None.
        """
        with self._lock:
            snap = {name: val for name, (val, _, _) in self._values.items()}
        if uid is not None:
            snap["NDArrayUniqueId"] = int(uid)
        return snap

    def status(self) -> Dict[str, object]:
        """Diagnostic snapshot of connection state."""
        with self._lock:
            total = len(self._values)
            connected = sum(1 for _, (_, _, ever) in self._values.items() if ever)
        return {
            "loaded_path": self._loaded_path,
            "load_error": self._load_error,
            "total": total,
            "connected": connected,
            "specs": len(self._specs),
        }

    def specs(self) -> List[AttributeSpec]:
        """Read-only accessor for the currently-loaded attribute specs."""
        return list(self._specs)

    def dbrtype_for(self, name: str) -> str:
        """The dbrtype hint for a given attribute (or 'DBR_NATIVE')."""
        for s in self._specs:
            if s.name == name:
                return s.dbrtype_hint
        return "DBR_NATIVE"

    def update_uid(self, uid: int) -> None:
        """Update the implicit frame counter used by snapshot(uid=)."""
        self._uid = int(uid)

    # ---------------- internals ---------------------------------------

    def _resolve_path(self, xml_file: str) -> str:
        """Resolve xml_file to an existing absolute path.

        Order:
          1. Absolute path (if it exists).
          2. Env var AREA_DETECTOR_ATTRIBUTES_PATH (colon-separated).
          3. Manager's search_paths (in order).
          4. CWD.
          5. Auto-discovered synApps iocBoot dirs (see
             default_xml_search_paths()) — so tomoscan basenames just
             work out of the box on standard beamline installs.
        Raises FileNotFoundError with the list of paths tried.
        """
        tried: List[str] = []
        p = Path(xml_file)
        if p.is_absolute():
            tried.append(str(p))
            if p.exists():
                return str(p)

        env = os.environ.get("AREA_DETECTOR_ATTRIBUTES_PATH", "")
        env_dirs = [d for d in env.split(":") if d]

        candidates_ordered = (env_dirs + self._search_paths
                              + [os.getcwd()]
                              + default_xml_search_paths())
        seen = set()
        for base in candidates_ordered:
            if base in seen:
                continue
            seen.add(base)
            candidate = Path(base) / xml_file
            tried.append(str(candidate))
            if candidate.exists():
                return str(candidate)

        raise FileNotFoundError(
            f"NDAttributes XML {xml_file!r} not found. Tried:\n  "
            + "\n  ".join(tried))

    def _create_subscriptions(self) -> None:
        if not self._specs:
            return

        # Create pyepics PVs — non-blocking. Each PV starts its own async
        # search. add_callback() gets fired both on the initial value AND
        # on every monitor update.
        for spec in self._specs:
            try:
                pv = epics.PV(
                    spec.pvname,
                    callback=self._make_callback(spec.name),
                    auto_monitor=True,
                    connection_callback=self._make_conn_callback(spec.name),
                )
                self._pvs[spec.name] = pv
            except Exception:
                logger.exception(
                    "Failed to create PV %s (attr %s)",
                    spec.pvname, spec.name)

        logger.info(
            "NDAttributesManager: %d subscriptions queued for %s",
            len(self._pvs), self._loaded_path)

        # Background thread: wait for connections and log per-PV status
        # so operators can immediately see which IOCs are down.
        t = threading.Thread(
            target=self._connect_and_report,
            daemon=True, name="ndattrs-connect")
        t.start()

    def _connect_and_report(
        self, wait_seconds: float = 10.0,
    ) -> None:
        """Wait up to wait_seconds for PVs to connect, then log stats."""
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            with self._lock:
                unconnected = [
                    name for name, (_, _, ever) in self._values.items()
                    if not ever
                ]
            if not unconnected:
                break
            time.sleep(0.5)

        with self._lock:
            n_conn = sum(1 for _, (_, _, ever) in self._values.items() if ever)
            n_total = len(self._values)
            missing = [
                name for name, (_, _, ever) in self._values.items() if not ever
            ]
        logger.info(
            "NDAttributesManager: %d/%d PVs connected within %.0fs",
            n_conn, n_total, wait_seconds)
        if missing:
            sample = missing[:20]
            pv_by_name = {s.name: s.pvname for s in self._specs}
            logger.warning(
                "NDAttributesManager: %d PVs never connected. Examples:\n  %s",
                len(missing),
                "\n  ".join(f"{n} -> {pv_by_name.get(n, '?')}"
                            for n in sample))

    def _make_callback(self, attr_name: str) -> Callable:
        """pyepics value callback. Fires on initial value + every update.

        Normalizes so downstream layout writer never has to guess:
          - CHAR waveforms (numpy uint8 arrays) → utf-8 str, null-stripped
          - DBR_STRING (bytes) → utf-8 str, null-stripped
          - Existing str → null-stripped
          - Everything else (int/float/array) → passed through as-is
        """
        # dbrtype hint helps decide whether a numpy uint8 array is meant
        # as a string or as raw bytes.
        dbrtype = self.dbrtype_for(attr_name)

        def _cb(pvname=None, value=None, **kw):
            try:
                v = value
                if isinstance(v, bytes):
                    v = v.decode("utf-8", errors="replace").rstrip("\x00")
                elif isinstance(v, str):
                    v = v.rstrip("\x00")
                else:
                    # numpy uint8 array from CHAR waveform (== a string) —
                    # only decode if this attr was declared DBR_STRING or
                    # its dtype clearly signals ASCII (uint8).
                    try:
                        import numpy as _np
                        if isinstance(v, _np.ndarray) and v.dtype == _np.uint8:
                            if dbrtype == "DBR_STRING" or True:
                                # Almost always a CHAR-waveform string in
                                # areaDetector; strip nulls, decode ASCII.
                                v = bytes(v).decode(
                                    "ascii", errors="replace").rstrip("\x00")
                    except Exception:
                        pass
                with self._lock:
                    self._values[attr_name] = (v, time.time(), True)
            except Exception:
                logger.exception(
                    "Error handling monitor update for attr %s", attr_name)
        return _cb

    def _make_conn_callback(self, attr_name: str) -> Callable:
        """pyepics connection callback. Sets ever_connected=True as soon
        as the PV connects, even before the first value arrives."""
        def _cb(pvname=None, conn=False, **kw):
            if conn:
                with self._lock:
                    val, ts, _ = self._values.get(
                        attr_name, (None, 0.0, False))
                    self._values[attr_name] = (val, ts, True)
        return _cb

    def _teardown_subscriptions(self) -> None:
        for name, pv in list(self._pvs.items()):
            try:
                pv.disconnect()
            except Exception:
                logger.debug("disconnect() failed for %s", name, exc_info=True)
        self._pvs.clear()
