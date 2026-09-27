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

from caproto.threading.client import Context, SharedBroadcaster

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
    """Owns caproto subscriptions for a set of NDAttributes, keeps a live
    cache of their latest values, and exposes a non-blocking snapshot().

    Thread-safe. Subscription callbacks run on caproto's threads; snapshot()
    can be called from any thread (typically the camera acquisition loop).
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
        # caproto plumbing
        self._broadcaster: Optional[SharedBroadcaster] = None
        self._context: Optional[Context] = None
        self._pvs: Dict[str, object] = {}          # attr_name -> caproto PV
        self._subs: Dict[str, object] = {}         # attr_name -> Subscription
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

        self._start_context()
        self._create_subscriptions()

    def close(self) -> None:
        """Disconnect everything. Idempotent."""
        self._teardown_subscriptions()
        if self._context is not None:
            try:
                self._context.disconnect()
            except Exception:
                logger.exception("Error disconnecting caproto context")
            self._context = None
        if self._broadcaster is not None:
            try:
                self._broadcaster.disconnect()
            except Exception:
                logger.exception("Error disconnecting caproto broadcaster")
            self._broadcaster = None

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

    def _start_context(self) -> None:
        if self._broadcaster is None:
            self._broadcaster = SharedBroadcaster()
        if self._context is None:
            self._context = Context(broadcaster=self._broadcaster)

    def _create_subscriptions(self) -> None:
        if not self._specs:
            return
        pvnames = [s.pvname for s in self._specs]
        # get_pvs returns one PV per name, in order, and starts async
        # search immediately. This does NOT block on connection.
        pv_objs = self._context.get_pvs(*pvnames)

        for spec, pv in zip(self._specs, pv_objs):
            self._pvs[spec.name] = pv
            try:
                sub = pv.subscribe(data_type="time")
                sub.add_callback(self._make_callback(spec.name))
                self._subs[spec.name] = sub
            except Exception:
                logger.exception(
                    "Failed to subscribe to %s (attr %s)",
                    spec.pvname, spec.name)

        logger.info(
            "NDAttributesManager: %d subscriptions queued for %s",
            len(self._subs), self._loaded_path)

    def _make_callback(self, attr_name: str) -> Callable:
        # Bound closure so caproto's callback interface (sub, response)
        # gets routed to this manager's cache under a name key.
        def _cb(sub, response):
            try:
                data = response.data
                # caproto returns a tuple/array for the data field; scalar
                # PVs have length 1. Take the singleton, else keep as-is.
                if hasattr(data, "__len__") and len(data) == 1:
                    value = data[0]
                    if isinstance(value, bytes):
                        try:
                            value = value.decode("utf-8", errors="replace")
                        except Exception:
                            pass
                else:
                    value = data
                with self._lock:
                    self._values[attr_name] = (value, time.time(), True)
            except Exception:
                logger.exception(
                    "Error handling monitor update for attr %s", attr_name)
        return _cb

    def _teardown_subscriptions(self) -> None:
        for name, sub in list(self._subs.items()):
            try:
                sub.clear()
            except Exception:
                logger.debug("clear() failed for %s", name, exc_info=True)
        self._subs.clear()
        for name, pv in list(self._pvs.items()):
            try:
                pv.unsubscribe_all()
            except Exception:
                logger.debug("unsubscribe_all() failed for %s", name,
                             exc_info=True)
            try:
                pv.go_idle()
            except Exception:
                logger.debug("go_idle() failed for %s", name, exc_info=True)
        self._pvs.clear()
