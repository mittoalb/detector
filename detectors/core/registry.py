"""
Camera type registry and factory.

Allows new camera backends to register themselves so the IOC/GUI can list
and instantiate them by name.
"""

import logging
from typing import Dict, List, Optional, Type

from .base import BaseCamera

logger = logging.getLogger(__name__)

_REGISTRY: Dict[str, Type[BaseCamera]] = {}


def register(camera_class: Type[BaseCamera]) -> Type[BaseCamera]:
    """Decorator to register a camera class.

    Usage:
        @register
        class MyCamera(BaseCamera):
            camera_type = "vendor.model"
            display_name = "My Camera Model"
    """
    if not camera_class.camera_type:
        raise ValueError(f"{camera_class.__name__} must define camera_type")
    _REGISTRY[camera_class.camera_type] = camera_class
    logger.debug("Registered camera type: %s", camera_class.camera_type)
    return camera_class


def list_types() -> List[str]:
    """Return all registered camera type identifiers."""
    return sorted(_REGISTRY.keys())


def get_class(camera_type: str) -> Type[BaseCamera]:
    """Look up a camera class by type identifier."""
    if camera_type not in _REGISTRY:
        raise KeyError(
            f"Unknown camera type '{camera_type}'. "
            f"Available: {list_types()}")
    return _REGISTRY[camera_type]


def create(camera_type: str, **kwargs) -> BaseCamera:
    """Instantiate a camera by type identifier."""
    cls = get_class(camera_type)
    return cls(**kwargs)


def auto_detect(camera_types: Optional[List[str]] = None) -> Optional[BaseCamera]:
    """Try to open each registered camera type until one succeeds.

    Args:
        camera_types: If provided, only try these types. Otherwise try all.

    Returns:
        First successfully-opened camera, or None if all fail.
    """
    types = camera_types if camera_types is not None else list_types()
    # Never silently pick the simulator during auto-detect; if no real camera
    # comes up, the caller should report failure rather than fake data.
    real_types = [ct for ct in types if ct != "simulator"]
    for ct in real_types:
        try:
            cls = get_class(ct)
            cam = cls()
            cam.open()
            logger.info("Auto-detected camera: %s", ct)
            return cam
        except Exception as exc:
            # Promote to INFO so the user sees *why* each backend failed —
            # the most common cause after a reboot is the vendor SDK / driver
            # not being loaded, and silent DEBUG hides it.
            logger.info("  auto-detect %s failed: %s", ct, exc)
    return None


def load_all() -> None:
    """Import all camera modules so they self-register.

    Call this once at program startup before listing/creating cameras.
    Modules that fail to import (missing SDKs etc.) are silently skipped.
    """
    import importlib
    modules = [
        "detectors.cameras.hamamatsu.orca_fire_dcam",
        "detectors.cameras.hamamatsu.orca_fire_gentl",
        "detectors.cameras.teledyne.oryx",
        "detectors.cameras.teledyne.kinetix",
        "detectors.cameras.simulator",
    ]
    for mod in modules:
        try:
            importlib.import_module(mod)
        except ImportError as exc:
            logger.debug("Skipping %s: %s", mod, exc)
        except Exception as exc:
            logger.warning("Error loading %s: %s", mod, exc)
