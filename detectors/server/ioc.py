"""
Camera-agnostic EPICS IOC for areaDetector compatibility.
"""

import logging
from typing import Optional

from caproto.server import PVGroup, SubGroup

from detectors.core.base import BaseCamera
from detectors.server.cam_plugin import CamPlugin
from detectors.server.hdf5_plugin import HDF5Plugin
from detectors.server.ntnda_server import NTNDArrayServer

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

    def __init__(self, *args,
                 camera: Optional[BaseCamera] = None,
                 pva_pv: Optional[str] = None,
                 **kwargs):
        super().__init__(*args, **kwargs)

        self.camera = camera
        self.cam1.set_camera(camera)
        self.cam1.register_frame_callback(self.HDF1.on_frame)
        # When acquisition ends, close HDF5 only if NumCapture target was
        # reached. Tomoscan keeps one capture session across multiple
        # acquisitions (flats / projections / darks), so we must not
        # auto-close between phases.
        self.cam1.register_end_callback(self.HDF1._maybe_stop_capture)

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
