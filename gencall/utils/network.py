"""
GenCall Network Utilities.
"""

import socket
import logging

logger = logging.getLogger("gencall.utils.network")


def get_default_ip() -> str:
    """Get the IP address of the default network interface."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def check_port_available(host: str, port: int) -> bool:
    """Check if a port is available for binding."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((host, port))
        s.close()
        return True
    except OSError:
        return False


def resolve_host(hostname: str) -> str:
    """Resolve hostname to IP address."""
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        return hostname
