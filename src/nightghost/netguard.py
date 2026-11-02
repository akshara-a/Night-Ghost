"""Best-effort network egress blocking for Python subprocesses (the test runner).

Proxy variables only stop well-behaved HTTP clients. This module additionally installs a
``sitecustomize`` shim, loaded through ``PYTHONPATH``, that makes ``socket.connect`` and DNS
lookups fail for any non-loopback destination. It covers every Python process started by the test
command (pytest and anything it imports). It does NOT cover non-Python tools or a process that
removes the shim; for hard isolation use an OS-level sandbox (network namespace, container).
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Optional

SHIM_SOURCE = '''\
import socket as _socket

_LOCAL = {"localhost", "127.0.0.1", "::1", "::ffff:127.0.0.1", "0.0.0.0", ""}


def _is_local(host):
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "ignore")
    return host in _LOCAL or str(host).startswith("127.")


def _block(host):
    raise OSError("Network access blocked by Night Ghost (safety.network=off): %r" % (host,))


def _is_ip_literal(host):
    try:
        _socket.inet_pton(_socket.AF_INET, host)
        return True
    except (OSError, ValueError, TypeError):
        pass
    try:
        _socket.inet_pton(_socket.AF_INET6, host)
        return True
    except (OSError, ValueError, TypeError):
        return False


_orig_connect = _socket.socket.connect
_orig_connect_ex = _socket.socket.connect_ex
_orig_getaddrinfo = _socket.getaddrinfo


def _address_host(sock, address):
    if sock.family not in (_socket.AF_INET, _socket.AF_INET6):
        return None
    return address[0] if isinstance(address, tuple) else address


def _connect(self, address):
    host = _address_host(self, address)
    if host is not None and not _is_local(host):
        _block(host)
    return _orig_connect(self, address)


def _connect_ex(self, address):
    host = _address_host(self, address)
    if host is not None and not _is_local(host):
        _block(host)
    return _orig_connect_ex(self, address)


def _getaddrinfo(host, *args, **kwargs):
    if isinstance(host, str) and not _is_local(host) and not _is_ip_literal(host):
        _block(host)
    return _orig_getaddrinfo(host, *args, **kwargs)


_socket.socket.connect = _connect
_socket.socket.connect_ex = _connect_ex
_socket.getaddrinfo = _getaddrinfo
'''

_shim_dir: Optional[Path] = None


def _cleanup() -> None:
    if _shim_dir is not None:
        shutil.rmtree(_shim_dir, ignore_errors=True)


def shim_directory() -> Path:
    """Create (once per process) a temp directory holding the sitecustomize shim."""
    global _shim_dir
    if _shim_dir is None or not _shim_dir.is_dir():
        _shim_dir = Path(tempfile.mkdtemp(prefix="nightghost-netguard-"))
        (_shim_dir / "sitecustomize.py").write_text(SHIM_SOURCE, encoding="utf-8")
        atexit.register(_cleanup)
    return _shim_dir


def apply_to_env(env: Dict[str, str]) -> None:
    """Add the proxy poison variables and the socket shim to a subprocess environment."""
    poison = "http://127.0.0.1:0"
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[key] = poison
    env["NO_PROXY"] = ""
    env["PIP_NO_INDEX"] = "1"
    env["npm_config_offline"] = "true"

    shim = str(shim_directory())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = shim + (os.pathsep + existing if existing else "")
