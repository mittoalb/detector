"""
Client side of the driver→GUI IPC channel.

Talks to detectors.server.ipc.CameraIPCServer over a UNIX socket. The
GUI subprocess uses this to reach the camera's full BaseCamera surface
without going through EPICS PVs.

Design:
    - Persistent connection with a lock for thread safety
    - Auto-reconnect on socket errors (driver process may restart)
    - Requests are line-delimited JSON, same format as the server
    - Blocking calls with a short timeout; caller decides on retry policy
"""

import json
import logging
import socket
import threading
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class IPCError(RuntimeError):
    """Raised for any IPC-level failure (connect, timeout, protocol)."""


class CameraIPCClient:
    """UNIX-socket JSON-RPC client for the camera driver process."""

    def __init__(self, socket_path: str, connect_timeout: float = 1.0,
                 io_timeout: float = 5.0):
        self._path = socket_path
        self._connect_timeout = connect_timeout
        self._io_timeout = io_timeout
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._lock = threading.Lock()
        # Fail fast if the driver socket isn't there — caller falls back to CA
        self._connect()

    def _connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self._connect_timeout)
        try:
            s.connect(self._path)
        except OSError as exc:
            s.close()
            raise IPCError(f"cannot reach IPC socket {self._path}: {exc}") from exc
        s.settimeout(self._io_timeout)
        self._sock = s
        self._buf = b""
        logger.info("IPC client connected to %s", self._path)

    def close(self) -> None:
        with self._lock:
            try:
                if self._sock is not None:
                    self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._buf = b""

    def _call(self, op: str, **kwargs) -> Any:
        """Send one request, read one response. Auto-reconnect on failure."""
        payload = json.dumps({"op": op, **kwargs}).encode("utf-8") + b"\n"
        with self._lock:
            for attempt in (0, 1):
                if self._sock is None:
                    try:
                        self._connect()
                    except IPCError:
                        if attempt == 1:
                            raise
                        continue
                try:
                    self._sock.sendall(payload)
                    resp = self._recv_line()
                    break
                except (OSError, socket.timeout) as exc:
                    logger.debug("IPC send/recv failed (%s); reconnecting", exc)
                    self.close_locked()
                    if attempt == 1:
                        raise IPCError(f"IPC transport failure: {exc}") from exc
        try:
            obj = json.loads(resp.decode("utf-8"))
        except Exception as exc:
            raise IPCError(f"malformed IPC response: {exc}") from exc
        if not obj.get("ok"):
            raise IPCError(str(obj.get("error", "unknown IPC error")))
        return obj.get("value")

    def close_locked(self) -> None:
        """Close without re-acquiring the lock (already held by caller)."""
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        self._sock = None
        self._buf = b""

    def _recv_line(self) -> bytes:
        """Read bytes until '\\n'. Reuses any residual buffer."""
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("IPC connection closed by server")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    # ---- BaseCamera-shaped facade the CameraProxy uses ----

    def list_params(self) -> List[str]:
        return list(self._call("list_params") or [])

    def get_param(self, name: str) -> Any:
        return self._call("get_param", name=name)

    def set_param(self, name: str, value: Any) -> None:
        self._call("set_param", name=name, value=value)

    def is_param_writable(self, name: str) -> bool:
        return bool(self._call("is_param_writable", name=name))

    def get_info(self) -> dict:
        return dict(self._call("get_info") or {})

    def software_trigger(self) -> None:
        self._call("software_trigger")
