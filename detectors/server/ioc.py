"""
Camera-agnostic EPICS IOC for areaDetector compatibility.
"""

import logging
import os
from typing import Dict, List, Optional

from caproto.server import PVGroup, SubGroup

from detectors.core.base import BaseCamera
from detectors.server.cam_plugin import CamPlugin
from detectors.server.hdf5_plugin import HDF5Plugin
from detectors.server.ipc import CameraIPCServer, socket_path_for
from detectors.server.nd_attributes import NDAttributesManager
from detectors.server.ntnda_server import NTNDArrayServer
from detectors.server.tiff_plugin import TIFFPlugin

logger = logging.getLogger(__name__)


class DetectorIOC(PVGroup):
    """
    areaDetector-compatible IOC for any BaseCamera backend.

    Serves cam1: + HDF1: PVs and an optional PVAccess NTNDArray stream.

    Example:
        from detectors.core import registry
        registry.load_all()
        camera = registry.create("hamamatsu.orca_fire_dcam")
        camera.open()
        ioc = DetectorIOC(prefix="ORCA:", camera=camera)
        run(ioc.pvdb)
    """

    cam1 = SubGroup(CamPlugin, prefix="cam1:")
    HDF1 = SubGroup(HDF5Plugin, prefix="HDF1:")
    TIF1 = SubGroup(TIFFPlugin, prefix="TIF1:")

    def __init__(self, *args,
                 camera: Optional[BaseCamera] = None,
                 pva_pv: Optional[str] = None,
                 nd_attributes_macros: Optional[Dict[str, str]] = None,
                 xml_search_paths: Optional[List[str]] = None,
                 **kwargs):
        super().__init__(*args, **kwargs)

        self.camera = camera
        self.cam1.set_camera(camera)

        # Build the NDAttributesManager. It's alive for the IOC's lifetime
        # (not per-capture) so CA subscriptions stay warm across scans.
        # Tomoscan puts a basename into cam1:NDAttributesFile at scan start;
        # that putter triggers .load() on this shared manager.
        prefix = self.prefix if hasattr(self, "prefix") else ""
        macros = dict(nd_attributes_macros or {})
        macros.setdefault("DET", prefix)
        # Search paths: the shell's CWD (for tomoscan's basename puts) plus
        # any caller-provided dirs.
        search = list(xml_search_paths or []) + [os.getcwd()]
        self._nd_attrs = NDAttributesManager(
            macros=macros, search_paths=search)
        self.cam1.set_nd_attributes_manager(self._nd_attrs)
        # HDF5 plugin needs the same search paths to resolve XMLFileName
        self.HDF1.set_layout_search_paths(search)
        # Direct pyepics subscription to $(TS)HDF5Location — this is the
        # authoritative per-frame routing key from tomoscan. Snapshotted
        # into every frame's metadata so the HDF5 writer picks the right
        # /exchange/* dataset without racing an intermediate mirror PV.
        ts_prefix = macros.get("TS", "")
        if ts_prefix and "$(" not in ts_prefix:
            self.cam1.set_ts_prefix(ts_prefix)

        self.cam1.register_frame_callback(self.HDF1.on_frame)
        # When acquisition ends, close HDF5 only if NumCapture target was
        # reached. Tomoscan keeps one capture session across multiple
        # acquisitions (flats / projections / darks), so we must not
        # auto-close between phases.
        self.cam1.register_end_callback(self.HDF1._maybe_stop_capture)
        # TIFF series writer runs in parallel with HDF5. Its on_frame
        # early-returns when not capturing, so wiring it unconditionally
        # is free — only one writer ever has _capturing=True at a time
        # (GUI enforces this with its format selector).
        self.cam1.register_frame_callback(self.TIF1.on_frame)
        self.cam1.register_end_callback(self.TIF1._maybe_stop_capture)

        # PVA NTNDArray server
        prefix = self.prefix if hasattr(self, "prefix") else ""
        if pva_pv is None:
            pva_pv = prefix + "image1:ArrayData"
        self._pva_server = None
        try:
            self._pva_server = NTNDArrayServer(pva_pv)
            self.cam1.register_frame_callback(self._pva_server.publish_frame)
        except Exception as exc:
            logger.warning("PVA server unavailable: %s", exc)

        # Local IPC socket so the GUI subprocess can reach the camera's
        # full BaseCamera surface (all Oryx GenICam features, all DCAM
        # properties, all PVCAM params) rather than just the 15-key CA
        # gateway in cam_plugin. Camera-agnostic — every backend gets it.
        self._ipc_server = None
        if camera is not None:
            try:
                self._ipc_server = CameraIPCServer(
                    camera, socket_path_for(prefix))
                self._ipc_server.start()
            except Exception as exc:
                logger.warning("IPC server unavailable: %s", exc)
