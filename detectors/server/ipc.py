"""
Local IPC channel between the camera driver process and the GUI subprocess.

Serves the camera's full BaseCamera surface (list_params / get_param /
set_param / is_param_writable / get_info / software_trigger) over a
line-delimited JSON protocol on a UNIX socket. Lets the GUI enumerate
and edit every feature the backend supports without needing a matching
EPICS PV for each one — the CA gateway (cam_plugin.py) still exists for
tomoscan and other EPICS clients, but the GUI no longer depends on it
for feature access.

Socket path convention:
    /tmp/detector_ioc_<prefix>.sock   (colons in prefix stripped)

Protocol: one JSON object per line, request/response:

    request  {"op": "list_params"}
    response {"ok": true, "value": ["ExposureTime", "Width", ...]}

    request  {"op": "get_param", "name": "ExposureTime"}
    response {"ok": true, "value": 0.01}

    request  {"op": "set_param", "name": "Width", "value": 6480}
    response {"ok": true}

    request  {"op": "is_param_writable", "name": "Width"}
    response {"ok": true, "value": true}

    request  {"op": "get_info"}
    response {"ok": true, "value": {"vendor": "FLIR", ...}}

    request  {"op": "software_trigger"}
    response {"ok": true}

Errors:
    response {"ok": false, "error": "message"}
"""

import json
import logging
import os
import socket
import threading
from typing import Optional

from detectors.core.base import BaseCamera

logger = logging.getLogger(__name__)


def socket_path_for(prefix: str) -> str:
    """Derive the socket path from an EPICS prefix (colons stripped)."""
    clean = prefix.replace(":", "").replace("/", "_") or "default"
    return f"/tmp/detector_ioc_{clean}.sock"


class CameraIPCServer:
    """Serves BaseCamera methods over a UNIX socket to the GUI subprocess."""

    def __init__(self, camera: BaseCamera, socket_path: str):
        self._camera = camera
        self._path = socket_path
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Serialize camera SDK access from IPC clients — the acquisition
        # loop and CA putters also touch the camera, and vendor SDKs are
        # inconsistent about thread safety.
        self._cam_lock = threading.Lock()

    def start(self) -> None:
        # Handle stale socket files from crashed prior runs. Probe with a
        # short-timeout connect: if it refuses, no one's listening → unlink
        # and rebind. If it accepts, another IOC owns this prefix → abort
        # rather than silently take over.
        if os.path.exists(self._path):
            if self._path_is_live():
                raise RuntimeError(
                    f"IPC socket {self._path} is in use by another IOC "
                    f"process (same prefix). Stop the other IOC first.")
            try:
                os.unlink(self._path)
                logger.info("Removed stale IPC socket %s", self._path)
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot remove stale IPC socket {self._path}: {exc}. "
                    f"Delete it manually and retry.") from exc
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self._path)
        try:
            os.chmod(self._path, 0o666)  # Let any local user's GUI connect
        except OSError:
            pass
        self._sock.listen(4)
        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="CameraIPCServer")
        self._thread.start()
        logger.info("IPC server listening on %s", self._path)

    def _path_is_live(self) -> bool:
        """True if a process is actively listening on the socket path."""
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(self._path)
            return True
        except (ConnectionRefusedError, FileNotFoundError, socket.timeout):
            return False
        except OSError:
            return False
        finally:
            probe.close()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        try:
            if os.path.exists(self._path):
                os.unlink(self._path)
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            t = threading.Thread(
                target=self._handle_client, args=(conn,),
                daemon=True, name="CameraIPCClient")
            t.start()

    def _handle_client(self, conn: socket.socket) -> None:
        conn.settimeout(30.0)
        buf = b""
        try:
            while not self._stop.is_set():
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    resp = self._dispatch(line)
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except (ConnectionError, OSError, socket.timeout):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, raw_line: bytes) -> dict:
        try:
            req = json.loads(raw_line.decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": f"malformed request: {exc}"}
        op = req.get("op")
        try:
            with self._cam_lock:
                if op == "list_params":
                    return {"ok": True, "value": list(self._camera.list_params())}
                if op == "get_param":
                    return {"ok": True,
                             "value": self._coerce(self._camera.get_param(req["name"]))}
                if op == "set_param":
                    self._camera.set_param(req["name"], req["value"])
                    return {"ok": True}
                if op == "is_param_writable":
                    return {"ok": True,
                             "value": bool(self._camera.is_param_writable(req["name"]))}
                if op == "get_info":
                    info = self._camera.get_info()
                    return {"ok": True, "value": info.to_dict()}
                if op == "software_trigger":
                    self._camera.software_trigger()
                    return {"ok": True}
            return {"ok": False, "error": f"unknown op: {op!r}"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _coerce(v):
        """Turn SDK-native types into JSON-safe primitives."""
        if isinstance(v, (int, float, str, bool)) or v is None:
            return v
        if isinstance(v, (list, tuple)):
            return [CameraIPCServer._coerce(x) for x in v]
        if isinstance(v, dict):
            return {str(k): CameraIPCServer._coerce(x) for k, x in v.items()}
        # numpy scalar / anything else → best-effort primitive
        try:
            return v.item()
        except AttributeError:
            return str(v)
