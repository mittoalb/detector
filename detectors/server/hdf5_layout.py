"""
HDF5 layout writer driven by an areaDetector-style Layout XML.

Parses the tomoscan-flavor XML (see 32idbSP1/iocBoot/…/TomoScanLayout.xml)
into a tree of LayoutNodes, then writes an HDF5 file that matches that
layout using per-frame snapshots from NDAttributesManager.

Supported layout features (matches what the 32-ID XML uses):

  <hdf5_layout>
    <global name="detector_data_destination" ndattribute="HDF5FrameLocation"/>
    <group name="...">
      <group name="..." ndattr_default="true">       # /defaults catch-all
      <dataset name="..." source="detector">          # per-frame image data
      <dataset name="..." source="constant" type="string" value="...">
      <dataset name="..." source="ndattribute" ndattribute="..." when="OnFileOpen|OnFileClose">
        <attribute name="units" source="constant" type="string" value="mA"/>

Not implemented (bail loudly if seen — extend when a real XML needs them):
  * <hardlink>, <softlink>
  * source="param", source="drv_info"
  * constant type= other than "string"
  * ndattr_default on any group other than the single catch-all
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import h5py
import numpy as np

logger = logging.getLogger(__name__)


# Dataset names that are written by the HDF5 plugin itself (not from XML) —
# excluded from the catch-all /defaults dump so we don't double-write them.
LEGACY_DEFAULT_DATASETS = {"NDArrayUniqueId", "HDF5FrameLocation"}


@dataclass
class LayoutAttr:
    """HDF5 attribute attached to a dataset/group (from <attribute>)."""
    name: str
    value: str
    type: str = "string"    # only "string" seen in real XMLs; extend later


@dataclass
class LayoutNode:
    """One node in the parsed layout tree."""
    kind: str                        # 'root' | 'group' | 'dataset'
    name: str
    path: str = ""                   # full HDF5 path, built at parse time
    children: List["LayoutNode"] = field(default_factory=list)
    hdf5_attrs: List[LayoutAttr] = field(default_factory=list)

    # dataset-only fields
    source: str = ""                 # 'detector' | 'constant' | 'ndattribute'
    when: str = ""                   # 'OnFileOpen' | 'OnFileClose' | ''
    type: str = ""                   # only used for source=constant
    value: str = ""                  # only used for source=constant
    ndattribute: str = ""            # attr name to read for source=ndattribute

    # group-only field
    ndattr_default: bool = False     # the single catch-all group


@dataclass
class ParsedLayout:
    """Result of parsing a Layout XML."""
    root: LayoutNode
    routing_ndattr: str              # value of <global … ndattribute="…"/>
    catch_all_path: str              # HDF5 path of the ndattr_default group, if any
    consumed_ndattrs: Set[str]       # set of ndattribute names referenced by named datasets
    detector_dataset_paths: List[str]  # every HDF5 path a frame could be routed to


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def parse_layout_xml(xml_path: str) -> ParsedLayout:
    """Parse an areaDetector Layout XML into a LayoutNode tree.

    Enforces: exactly one <global name="detector_data_destination"> and
    exactly one <group ndattr_default="true">. Bails loudly on
    schema surprises rather than write a subtly-wrong file.
    """
    tree = ET.parse(xml_path)
    root_elem = tree.getroot()
    if root_elem.tag != "hdf5_layout":
        raise ValueError(
            f"{xml_path}: root element is <{root_elem.tag}>, expected <hdf5_layout>")

    root = LayoutNode(kind="root", name="", path="")

    # First: the <global>. Must appear exactly once.
    globals_ = root_elem.findall("global")
    if len(globals_) != 1:
        raise ValueError(
            f"{xml_path}: expected exactly one <global>, found {len(globals_)}")
    g = globals_[0]
    if g.get("name") != "detector_data_destination":
        raise ValueError(
            f"{xml_path}: <global name={g.get('name')!r}>; only "
            f"'detector_data_destination' is supported")
    routing_ndattr = g.get("ndattribute", "")
    if not routing_ndattr:
        raise ValueError(
            f"{xml_path}: <global> is missing ndattribute= attribute")

    # Walk children (groups + datasets at the root level)
    catch_all_paths: List[str] = []
    detector_paths: List[str] = []
    consumed: Set[str] = set()

    for child in root_elem:
        if child.tag == "global":
            continue    # handled above
        node = _parse_node(child, parent_path="", xml_path=xml_path,
                           catch_all_paths=catch_all_paths,
                           detector_paths=detector_paths,
                           consumed=consumed)
        if node is not None:
            root.children.append(node)

    if len(catch_all_paths) > 1:
        raise ValueError(
            f"{xml_path}: multiple ndattr_default=true groups: {catch_all_paths}")
    catch_all_path = catch_all_paths[0] if catch_all_paths else ""

    if not detector_paths:
        raise ValueError(
            f"{xml_path}: no <dataset source='detector'> found — nothing to "
            f"route frames to")

    logger.info(
        "%s: parsed layout — %d detector datasets, %d consumed ndattrs, "
        "catch-all=%s, routing=%s",
        xml_path, len(detector_paths), len(consumed),
        catch_all_path or "(none)", routing_ndattr)

    return ParsedLayout(
        root=root,
        routing_ndattr=routing_ndattr,
        catch_all_path=catch_all_path,
        consumed_ndattrs=consumed,
        detector_dataset_paths=detector_paths,
    )


def _parse_node(
    elem: ET.Element,
    parent_path: str,
    xml_path: str,
    catch_all_paths: List[str],
    detector_paths: List[str],
    consumed: Set[str],
) -> Optional[LayoutNode]:
    """Recursive parser. Returns None for elements we ignore."""
    tag = elem.tag
    name = elem.get("name", "")
    full_path = f"{parent_path}/{name}" if name else parent_path

    if tag == "group":
        node = LayoutNode(kind="group", name=name, path=full_path)
        if elem.get("ndattr_default", "").lower() == "true":
            node.ndattr_default = True
            catch_all_paths.append(full_path)
        # Recurse into children
        for child in elem:
            sub = _parse_node(child, full_path, xml_path,
                              catch_all_paths, detector_paths, consumed)
            if sub is not None:
                if sub.kind in ("group", "dataset"):
                    node.children.append(sub)
                elif sub.kind == "hdf5_attr":
                    node.hdf5_attrs.append(
                        LayoutAttr(name=sub.name, value=sub.value,
                                   type=sub.type or "string"))
        return node

    if tag == "dataset":
        source = elem.get("source", "")
        if source not in ("detector", "constant", "ndattribute"):
            raise ValueError(
                f"{xml_path}: <dataset name={name!r} source={source!r}> — "
                f"only 'detector', 'constant', 'ndattribute' are supported")
        node = LayoutNode(
            kind="dataset", name=name, path=full_path,
            source=source,
            when=elem.get("when", ""),
            type=elem.get("type", ""),
            value=elem.get("value", ""),
            ndattribute=elem.get("ndattribute", ""),
        )
        if source == "detector":
            detector_paths.append(full_path)
        elif source == "ndattribute":
            if not node.ndattribute:
                raise ValueError(
                    f"{xml_path}: <dataset name={name!r} source='ndattribute'> "
                    f"is missing ndattribute= attribute")
            consumed.add(node.ndattribute)
        elif source == "constant":
            if node.type and node.type != "string":
                raise ValueError(
                    f"{xml_path}: <dataset name={name!r} source='constant' "
                    f"type={node.type!r}> — only 'string' constants are "
                    f"supported (extend when needed)")
        # Collect <attribute> children (HDF5 attributes)
        for child in elem:
            if child.tag == "attribute":
                node.hdf5_attrs.append(_parse_attribute(child, xml_path))
        return node

    if tag == "attribute":
        # An <attribute> at the top level of a group is an HDF5 attribute on
        # that group. Signal to the caller with a stub node.
        attr = _parse_attribute(elem, xml_path)
        return LayoutNode(kind="hdf5_attr", name=attr.name,
                          value=attr.value, type=attr.type)

    if tag in ("hardlink", "softlink"):
        raise ValueError(
            f"{xml_path}: <{tag}> is not implemented (see hdf5_layout.py)")

    # Unknown tag — warn but continue
    logger.warning("%s: ignoring unknown element <%s>", xml_path, tag)
    return None


def _parse_attribute(elem: ET.Element, xml_path: str) -> LayoutAttr:
    src = elem.get("source", "")
    if src != "constant":
        raise ValueError(
            f"{xml_path}: <attribute name={elem.get('name')!r} "
            f"source={src!r}> — only 'constant' HDF5 attributes are supported")
    return LayoutAttr(
        name=elem.get("name", ""),
        value=elem.get("value", ""),
        type=elem.get("type", "string"),
    )


# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------

def _string_dtype():
    """Variable-length UTF-8 string dtype for h5py. If a downstream consumer
    complains, switch this to a fixed-length ASCII dtype (e.g. |S40)."""
    return h5py.string_dtype(encoding="utf-8")


def _value_to_hdf5(value: object, dbrtype_hint: str = "DBR_NATIVE"):
    """Coerce a Python value from the NDAttributes cache into something
    h5py can store as a scalar dataset. Returns (data, dtype)."""
    if value is None:
        # Unknown/disconnected: encode as an empty string. Never invent a
        # numeric sentinel — silently-plausible zeros are worse than blanks.
        return "", _string_dtype()

    if dbrtype_hint == "DBR_STRING" or isinstance(value, (str, bytes)):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return str(value), _string_dtype()

    if dbrtype_hint == "DBR_DOUBLE":
        try:
            return float(value), np.float64
        except (TypeError, ValueError):
            return 0.0, np.float64

    if dbrtype_hint == "DBR_LONG":
        try:
            return int(value), np.int32
        except (TypeError, ValueError):
            return 0, np.int32

    if dbrtype_hint == "DBR_ENUM":
        try:
            return int(value), np.int32
        except (TypeError, ValueError):
            return 0, np.int32

    # DBR_NATIVE — look at the Python type
    if isinstance(value, (int, np.integer)):
        return int(value), np.int64
    if isinstance(value, (float, np.floating)):
        return float(value), np.float64
    if isinstance(value, np.ndarray):
        return value, value.dtype
    # Fallback
    return str(value), _string_dtype()


class LayoutWriter:
    """Writes an HDF5 file following a ParsedLayout.

    Detector datasets are created lazily on the first frame that maps to
    them (we don't know frame shape until then).
    Constant / OnFileOpen ndattribute datasets are written in open().
    OnFileClose ndattribute datasets are written in close().
    The catch-all /defaults group receives every attribute not consumed
    by a named dataset (and not in LEGACY_DEFAULT_DATASETS).
    """

    def __init__(self, layout: ParsedLayout):
        self.layout = layout
        # (path -> h5py Dataset) for detector datasets, populated lazily
        self._detector_datasets: Dict[str, h5py.Dataset] = {}
        # (path -> count of frames written)
        self._detector_counts: Dict[str, int] = {}
        # Last routing key seen empty — used to log a WARNING only once
        self._logged_empty_route = False
        # Track datasets we routed to but that aren't in the layout — log once
        self._logged_unknown_routes: Set[str] = set()

    # ---------------- open ----------------------------------------------

    def open(self, h5file: h5py.File, snapshot: Dict[str, object]) -> None:
        """Create the group tree, write constants, write OnFileOpen ndattributes."""
        self._create_tree(h5file, self.layout.root)
        self._write_static(h5file, self.layout.root, snapshot,
                            when_filter="OnFileOpen")

    def _create_tree(self, h5file: h5py.File, node: LayoutNode) -> None:
        for child in node.children:
            if child.kind == "group":
                if child.path and child.path not in h5file:
                    h5file.create_group(child.path)
                # Group-level HDF5 attributes
                if child.path:
                    grp = h5file[child.path]
                    for a in child.hdf5_attrs:
                        try:
                            grp.attrs[a.name] = a.value
                        except Exception:
                            logger.exception(
                                "Failed to write group attr %s on %s",
                                a.name, child.path)
                self._create_tree(h5file, child)
            elif child.kind == "dataset":
                # Detector datasets are created lazily on first frame.
                # Static datasets are created in _write_static.
                pass

    def _write_static(self, h5file: h5py.File, node: LayoutNode,
                      snapshot: Dict[str, object], when_filter: str) -> None:
        """Walk tree, write constant datasets always, and ndattribute
        datasets whose `when` matches when_filter."""
        for child in node.children:
            if child.kind == "group":
                self._write_static(h5file, child, snapshot, when_filter)
            elif child.kind == "dataset":
                if child.source == "constant":
                    # Constants written once, at open time (idempotent — skip
                    # if we've already been called)
                    if when_filter == "OnFileOpen" and child.path not in h5file:
                        self._create_string_dataset(
                            h5file, child.path, child.value or "",
                            child.hdf5_attrs)
                elif child.source == "ndattribute":
                    if child.when == when_filter:
                        value = snapshot.get(child.ndattribute)
                        self._create_ndattr_dataset(
                            h5file, child, value)

    def _create_string_dataset(
        self, h5file: h5py.File, path: str, value: str,
        hdf5_attrs: List[LayoutAttr],
    ) -> None:
        try:
            ds = h5file.create_dataset(path, data=value, dtype=_string_dtype())
            for a in hdf5_attrs:
                try:
                    ds.attrs[a.name] = a.value
                except Exception:
                    logger.exception(
                        "Failed to write ds attr %s on %s", a.name, path)
        except Exception:
            logger.exception("Failed to write constant dataset %s", path)

    def _create_ndattr_dataset(
        self, h5file: h5py.File, ds_node: LayoutNode,
        value: object,
    ) -> None:
        if ds_node.path in h5file:
            return
        # Look up the dbrtype from the snapshot's origin — we don't have it
        # here, so fall back to Python-type inference.
        data, dtype = _value_to_hdf5(value)
        try:
            ds = h5file.create_dataset(ds_node.path, data=data, dtype=dtype)
            for a in ds_node.hdf5_attrs:
                try:
                    ds.attrs[a.name] = a.value
                except Exception:
                    logger.exception(
                        "Failed to write ds attr %s on %s",
                        a.name, ds_node.path)
        except Exception:
            logger.exception(
                "Failed to write ndattribute dataset %s (value=%r)",
                ds_node.path, value)

    # ---------------- per-frame -----------------------------------------

    def write_frame(
        self, h5file: h5py.File, frame: np.ndarray,
        snapshot: Dict[str, object],
        fallback_path: str = "/exchange/data",
    ) -> str:
        """Append `frame` to the dataset named by
        snapshot[routing_ndattr]. Returns the path actually used.
        """
        raw = snapshot.get(self.layout.routing_ndattr)
        if raw is None or raw == "":
            if not self._logged_empty_route:
                logger.warning(
                    "Routing key %r empty on first frame; falling back to %s",
                    self.layout.routing_ndattr, fallback_path)
                self._logged_empty_route = True
            path = fallback_path
        else:
            path = str(raw)

        if path not in self.layout.detector_dataset_paths:
            if path not in self._logged_unknown_routes:
                logger.warning(
                    "Routing key value %r is not a declared detector dataset "
                    "in the layout; frame will still be written there but "
                    "won't have the layout's HDF5 attributes",
                    path)
                self._logged_unknown_routes.add(path)

        ds = self._detector_datasets.get(path)
        if ds is None:
            ds = self._create_detector_dataset(h5file, path, frame)
            self._detector_datasets[path] = ds
            self._detector_counts[path] = 0

        idx = self._detector_counts[path]
        new_size = idx + 1
        if new_size > ds.shape[0]:
            ds.resize((new_size,) + ds.shape[1:])
        ds[idx] = frame
        self._detector_counts[path] = new_size
        return path

    def _create_detector_dataset(
        self, h5file: h5py.File, path: str, first_frame: np.ndarray,
    ) -> h5py.Dataset:
        h, w = first_frame.shape
        # Ensure parent groups exist (safety-belt — usually done in open())
        parent = path.rsplit("/", 1)[0]
        if parent and parent not in h5file:
            h5file.create_group(parent)
        ds = h5file.create_dataset(
            path,
            shape=(1, h, w),
            maxshape=(None, h, w),
            chunks=(1, h, w),
            dtype=first_frame.dtype,
        )
        # Copy over the layout's HDF5 attributes if we can find the node
        node = self._find_node(self.layout.root, path)
        if node is not None:
            for a in node.hdf5_attrs:
                try:
                    ds.attrs[a.name] = a.value
                except Exception:
                    logger.exception(
                        "Failed to write ds attr %s on %s", a.name, path)
        return ds

    def _find_node(self, node: LayoutNode, target_path: str) -> Optional[LayoutNode]:
        for child in node.children:
            if child.kind == "dataset" and child.path == target_path:
                return child
            if child.kind == "group":
                found = self._find_node(child, target_path)
                if found is not None:
                    return found
        return None

    # ---------------- close ---------------------------------------------

    def close(
        self, h5file: h5py.File,
        last_snapshot: Optional[Dict[str, object]] = None,
    ) -> None:
        """Trim detector datasets to actual counts, write OnFileClose
        ndattribute datasets, dump remaining attributes into catch-all."""
        # Trim each detector dataset to the number of frames actually written
        for path, ds in self._detector_datasets.items():
            n = self._detector_counts.get(path, 0)
            if n < ds.shape[0]:
                ds.resize((n,) + ds.shape[1:])

        # Use the most recent snapshot seen for close-time writes
        last = last_snapshot or {}

        self._write_static(h5file, self.layout.root, last,
                            when_filter="OnFileClose")

        # Catch-all: every attribute not consumed by a named dataset and
        # not in the legacy-defaults set goes into /defaults/<name>
        if self.layout.catch_all_path:
            excluded = (self.layout.consumed_ndattrs
                        | LEGACY_DEFAULT_DATASETS
                        | {self.layout.routing_ndattr})
            base = self.layout.catch_all_path
            if base not in h5file:
                h5file.create_group(base)
            for name, value in last.items():
                if name in excluded:
                    continue
                path = f"{base}/{name}"
                if path in h5file:
                    continue
                data, dtype = _value_to_hdf5(value)
                try:
                    h5file.create_dataset(path, data=data, dtype=dtype)
                except Exception:
                    logger.exception(
                        "Failed to write catch-all default dataset %s "
                        "(value=%r)", path, value)
