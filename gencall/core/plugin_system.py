"""
GenCall Plugin / Extension System.

Loads custom Python scenario plugins from a directory, providing a
hook-based interface for extending GenCall behaviour at runtime.

Plugins implement a standard interface via the ``PluginBase`` class
and are discovered, registered, and managed through ``PluginManager``.

Built-in plugin: ``CallLoggerPlugin`` -- logs all call events to a file.
"""

from __future__ import annotations

import datetime
import importlib
import importlib.util
import inspect
import logging
import os
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("gencall.plugin_system")

_DEFAULT_PLUGIN_DIR = "/opt/gencall/plugins"


# ---------------------------------------------------------------------------
# Plugin state
# ---------------------------------------------------------------------------

class PluginState(Enum):
    DISCOVERED = "discovered"
    LOADED = "loaded"
    INITIALIZED = "initialized"
    STARTED = "started"
    STOPPED = "stopped"
    ERROR = "error"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Plugin manifest
# ---------------------------------------------------------------------------

@dataclass
class PluginManifest:
    """Metadata for a plugin."""
    name: str = ""
    version: str = "0.0.0"
    author: str = ""
    description: str = ""
    homepage: str = ""
    min_gencall_version: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "homepage": self.homepage,
            "min_gencall_version": self.min_gencall_version,
            "tags": self.tags,
        }


# ---------------------------------------------------------------------------
# Hook event types
# ---------------------------------------------------------------------------

@dataclass
class CallEvent:
    """Event data passed to on_call_start / on_call_end hooks."""
    call_id: str = ""
    caller: str = ""
    callee: str = ""
    start_time: Optional[datetime.datetime] = None
    end_time: Optional[datetime.datetime] = None
    duration: float = 0.0
    status: str = ""
    sip_code: int = 0
    codec: str = ""
    scenario_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "caller": self.caller,
            "callee": self.callee,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration": round(self.duration, 3),
            "status": self.status,
            "sip_code": self.sip_code,
            "codec": self.codec,
            "scenario_name": self.scenario_name,
            "extra": self.extra,
        }


@dataclass
class RTPPacketEvent:
    """Event data passed to on_rtp_packet hook."""
    ssrc: int = 0
    sequence: int = 0
    timestamp: int = 0
    payload_type: int = 0
    payload_size: int = 0
    direction: str = "sent"
    source_ip: str = ""
    dest_ip: str = ""

    def to_dict(self) -> dict:
        return {
            "ssrc": self.ssrc,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "payload_type": self.payload_type,
            "payload_size": self.payload_size,
            "direction": self.direction,
            "source_ip": self.source_ip,
            "dest_ip": self.dest_ip,
        }


@dataclass
class SIPMessageEvent:
    """Event data passed to on_sip_message hook."""
    direction: str = "received"
    method: str = ""
    status_code: int = 0
    call_id: str = ""
    cseq: str = ""
    source_ip: str = ""
    source_port: int = 0
    dest_ip: str = ""
    dest_port: int = 0
    raw: str = ""

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "method": self.method,
            "status_code": self.status_code,
            "call_id": self.call_id,
            "cseq": self.cseq,
            "source": f"{self.source_ip}:{self.source_port}",
            "dest": f"{self.dest_ip}:{self.dest_port}",
        }


# ---------------------------------------------------------------------------
# Plugin base class
# ---------------------------------------------------------------------------

class PluginBase(ABC):
    """
    Abstract base for all GenCall plugins.

    Subclass this and implement the hooks you need.
    The ``manifest`` class variable must be set.
    """

    manifest: PluginManifest = PluginManifest()

    def __init__(self) -> None:
        self._state = PluginState.LOADED

    @property
    def state(self) -> PluginState:
        return self._state

    # -- Lifecycle hooks ---------------------------------------------------

    def initialize(self, config: dict[str, Any] | None = None) -> None:
        """Called once after loading.  Receive config, set up resources."""
        self._state = PluginState.INITIALIZED

    def start(self) -> None:
        """Called when the plugin is activated."""
        self._state = PluginState.STARTED

    def stop(self) -> None:
        """Called when the plugin is deactivated."""
        self._state = PluginState.STOPPED

    # -- Event hooks -------------------------------------------------------

    def on_call_start(self, event: CallEvent) -> None:
        """Invoked when a new call is initiated."""

    def on_call_end(self, event: CallEvent) -> None:
        """Invoked when a call ends."""

    def on_rtp_packet(self, event: RTPPacketEvent) -> None:
        """Invoked for each RTP packet (high frequency!)."""

    def on_sip_message(self, event: SIPMessageEvent) -> None:
        """Invoked for each SIP message sent or received."""

    # -- Serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "manifest": self.manifest.to_dict(),
            "state": self._state.value,
        }


# ---------------------------------------------------------------------------
# Plugin wrapper (internal bookkeeping)
# ---------------------------------------------------------------------------

@dataclass
class _PluginEntry:
    """Internal wrapper around a loaded plugin instance."""
    plugin: PluginBase
    module_path: str = ""
    filename: str = ""
    state: PluginState = PluginState.DISCOVERED
    enabled: bool = True
    loaded_at: Optional[datetime.datetime] = None
    error_message: str = ""

    @property
    def name(self) -> str:
        return self.plugin.manifest.name or self.plugin.__class__.__name__

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "enabled": self.enabled,
            "filename": self.filename,
            "loaded_at": self.loaded_at.isoformat() if self.loaded_at else None,
            "error_message": self.error_message,
            "manifest": self.plugin.manifest.to_dict(),
        }


# ---------------------------------------------------------------------------
# Plugin Manager
# ---------------------------------------------------------------------------

class PluginManager:
    """
    Discovers, loads, and manages GenCall plugins.

    Usage::

        pm = PluginManager("/opt/gencall/plugins")
        pm.discover()
        pm.start_all()

        # Dispatch hooks
        pm.dispatch_call_start(event)
    """

    def __init__(self, plugin_dir: str = _DEFAULT_PLUGIN_DIR) -> None:
        self._plugin_dir = plugin_dir
        self._plugins: dict[str, _PluginEntry] = {}
        self._lock = threading.Lock()
        logger.info("PluginManager initialised (dir=%s)", plugin_dir)

    @property
    def plugin_dir(self) -> str:
        return self._plugin_dir

    # -- Discovery ---------------------------------------------------------

    def discover(self) -> list[str]:
        """
        Scan the plugin directory for Python files containing
        ``PluginBase`` subclasses.  Returns list of discovered names.
        """
        discovered: list[str] = []

        if not os.path.isdir(self._plugin_dir):
            logger.warning("Plugin directory does not exist: %s", self._plugin_dir)
            return discovered

        for fname in sorted(os.listdir(self._plugin_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            path = os.path.join(self._plugin_dir, fname)
            try:
                names = self._load_file(path)
                discovered.extend(names)
            except Exception as exc:
                logger.warning("Failed to load plugin file %s: %s", fname, exc)

        logger.info("Discovered %d plugins in %s", len(discovered), self._plugin_dir)
        return discovered

    def _load_file(self, path: str) -> list[str]:
        """Load all PluginBase subclasses from a Python file."""
        module_name = f"gencall_plugin_{Path(path).stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return []

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        names: list[str] = []
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if (
                inspect.isclass(obj)
                and issubclass(obj, PluginBase)
                and obj is not PluginBase
            ):
                instance = obj()
                entry = _PluginEntry(
                    plugin=instance,
                    module_path=path,
                    filename=os.path.basename(path),
                    state=PluginState.LOADED,
                    loaded_at=datetime.datetime.utcnow(),
                )
                with self._lock:
                    self._plugins[entry.name] = entry
                names.append(entry.name)
                logger.info(
                    "Plugin loaded: %s v%s (%s)",
                    entry.name, instance.manifest.version, entry.filename,
                )

        return names

    # -- Registration (manual) ---------------------------------------------

    def register(self, plugin: PluginBase) -> str:
        """Manually register a plugin instance."""
        entry = _PluginEntry(
            plugin=plugin,
            state=PluginState.LOADED,
            loaded_at=datetime.datetime.utcnow(),
        )
        with self._lock:
            self._plugins[entry.name] = entry
        logger.info("Plugin registered: %s", entry.name)
        return entry.name

    def unregister(self, name: str) -> bool:
        """Remove a plugin.  Stops it first if running."""
        with self._lock:
            entry = self._plugins.get(name)
            if not entry:
                return False
            if entry.state == PluginState.STARTED:
                self._stop_plugin(entry)
            del self._plugins[name]
        logger.info("Plugin unregistered: %s", name)
        return True

    # -- Lifecycle ---------------------------------------------------------

    def initialize_all(self, config: dict[str, Any] | None = None) -> None:
        """Initialise all loaded plugins."""
        with self._lock:
            entries = list(self._plugins.values())
        for entry in entries:
            self._init_plugin(entry, config)

    def start_all(self) -> None:
        """Start all enabled plugins."""
        with self._lock:
            entries = list(self._plugins.values())
        for entry in entries:
            if entry.enabled:
                self._start_plugin(entry)

    def stop_all(self) -> None:
        """Stop all running plugins."""
        with self._lock:
            entries = list(self._plugins.values())
        for entry in entries:
            if entry.state == PluginState.STARTED:
                self._stop_plugin(entry)

    def start_plugin(self, name: str) -> bool:
        with self._lock:
            entry = self._plugins.get(name)
        if not entry:
            return False
        return self._start_plugin(entry)

    def stop_plugin(self, name: str) -> bool:
        with self._lock:
            entry = self._plugins.get(name)
        if not entry:
            return False
        return self._stop_plugin(entry)

    def enable_plugin(self, name: str) -> bool:
        with self._lock:
            entry = self._plugins.get(name)
            if not entry:
                return False
            entry.enabled = True
        logger.info("Plugin enabled: %s", name)
        return True

    def disable_plugin(self, name: str) -> bool:
        with self._lock:
            entry = self._plugins.get(name)
            if not entry:
                return False
            entry.enabled = False
            if entry.state == PluginState.STARTED:
                self._stop_plugin(entry)
        logger.info("Plugin disabled: %s", name)
        return True

    def _init_plugin(self, entry: _PluginEntry, config: dict[str, Any] | None = None) -> bool:
        try:
            entry.plugin.initialize(config)
            entry.state = PluginState.INITIALIZED
            return True
        except Exception as exc:
            entry.state = PluginState.ERROR
            entry.error_message = str(exc)
            logger.exception("Plugin init error: %s", entry.name)
            return False

    def _start_plugin(self, entry: _PluginEntry) -> bool:
        try:
            if entry.state not in (PluginState.INITIALIZED, PluginState.STOPPED, PluginState.LOADED):
                if entry.state != PluginState.STARTED:
                    self._init_plugin(entry)
            entry.plugin.start()
            entry.state = PluginState.STARTED
            logger.info("Plugin started: %s", entry.name)
            return True
        except Exception as exc:
            entry.state = PluginState.ERROR
            entry.error_message = str(exc)
            logger.exception("Plugin start error: %s", entry.name)
            return False

    def _stop_plugin(self, entry: _PluginEntry) -> bool:
        try:
            entry.plugin.stop()
            entry.state = PluginState.STOPPED
            logger.info("Plugin stopped: %s", entry.name)
            return True
        except Exception as exc:
            entry.state = PluginState.ERROR
            entry.error_message = str(exc)
            logger.exception("Plugin stop error: %s", entry.name)
            return False

    # -- Hook dispatch -----------------------------------------------------

    def dispatch_call_start(self, event: CallEvent) -> None:
        self._dispatch("on_call_start", event)

    def dispatch_call_end(self, event: CallEvent) -> None:
        self._dispatch("on_call_end", event)

    def dispatch_rtp_packet(self, event: RTPPacketEvent) -> None:
        self._dispatch("on_rtp_packet", event)

    def dispatch_sip_message(self, event: SIPMessageEvent) -> None:
        self._dispatch("on_sip_message", event)

    def _dispatch(self, hook_name: str, event: Any) -> None:
        with self._lock:
            entries = list(self._plugins.values())
        for entry in entries:
            if entry.state != PluginState.STARTED or not entry.enabled:
                continue
            method = getattr(entry.plugin, hook_name, None)
            if method is None:
                continue
            try:
                method(event)
            except Exception:
                logger.debug(
                    "Plugin %s hook %s error", entry.name, hook_name, exc_info=True,
                )

    # -- Query -------------------------------------------------------------

    def get_plugin(self, name: str) -> Optional[dict]:
        with self._lock:
            entry = self._plugins.get(name)
        return entry.to_dict() if entry else None

    def list_plugins(self) -> list[dict]:
        with self._lock:
            return [e.to_dict() for e in self._plugins.values()]

    def to_dict(self) -> dict:
        with self._lock:
            entries = list(self._plugins.values())
        return {
            "plugin_dir": self._plugin_dir,
            "total_plugins": len(entries),
            "started": sum(1 for e in entries if e.state == PluginState.STARTED),
            "plugins": [e.to_dict() for e in entries],
        }


# ---------------------------------------------------------------------------
# Built-in plugin: CallLoggerPlugin
# ---------------------------------------------------------------------------

class CallLoggerPlugin(PluginBase):
    """
    Built-in plugin that logs all call events to a file.

    Demonstrates the plugin interface and provides useful call logging.
    """

    manifest = PluginManifest(
        name="call_logger",
        version="1.0.0",
        author="GenCall",
        description="Logs all call start/end events and SIP messages to a file",
        tags=["builtin", "logging"],
    )

    def __init__(self, log_path: str = "/opt/gencall/logs/call_events.log") -> None:
        super().__init__()
        self._log_path = log_path
        self._file: Any = None
        self._lock = threading.Lock()
        self._event_count = 0

    def initialize(self, config: dict[str, Any] | None = None) -> None:
        super().initialize(config)
        if config and "log_path" in config:
            self._log_path = config["log_path"]

    def start(self) -> None:
        super().start()
        try:
            os.makedirs(os.path.dirname(self._log_path) or ".", exist_ok=True)
            self._file = open(self._log_path, "a", buffering=1)
            self._write(f"--- Call logger started at {datetime.datetime.utcnow().isoformat()} ---")
            logger.info("CallLoggerPlugin writing to: %s", self._log_path)
        except OSError as exc:
            self._state = PluginState.ERROR
            logger.warning("CallLoggerPlugin cannot open log file: %s", exc)

    def stop(self) -> None:
        if self._file:
            self._write(f"--- Call logger stopped at {datetime.datetime.utcnow().isoformat()} ({self._event_count} events) ---")
            self._file.close()
            self._file = None
        super().stop()

    def on_call_start(self, event: CallEvent) -> None:
        self._write(
            f"CALL_START call_id={event.call_id} "
            f"caller={event.caller} callee={event.callee} "
            f"scenario={event.scenario_name}"
        )

    def on_call_end(self, event: CallEvent) -> None:
        self._write(
            f"CALL_END call_id={event.call_id} "
            f"status={event.status} duration={event.duration:.3f}s "
            f"sip_code={event.sip_code}"
        )

    def on_sip_message(self, event: SIPMessageEvent) -> None:
        label = event.method or f"{event.status_code}"
        self._write(
            f"SIP_{event.direction.upper()} {label} "
            f"call_id={event.call_id} "
            f"{event.source_ip}:{event.source_port} -> "
            f"{event.dest_ip}:{event.dest_port}"
        )

    def on_rtp_packet(self, event: RTPPacketEvent) -> None:
        # RTP packets are too frequent to log individually
        pass

    def _write(self, line: str) -> None:
        with self._lock:
            self._event_count += 1
            if self._file:
                ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                self._file.write(f"{ts} {line}\n")

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["log_path"] = self._log_path
        d["event_count"] = self._event_count
        return d
