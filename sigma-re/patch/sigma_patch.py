"""
sigma_patch - runtime CPU/leak hotfix for the legacy ("sigma") VanDorial build.

It does NOT modify the compiled .so files. It wraps the offending methods at import
time via a .pth auto-loader, so it installs/removes cleanly without touching the shipped
(vendor) binaries. Fully reversible: delete the two installed files and restart.

============================ WHAT IT FIXES ============================

(1) SCHEDULER idle-CPU busy-poll  [the real idle-CPU culprit]
    sigma.core.scheduler.Scheduler.run() runs the log-retention cleanup
    (Scheduler.cleanup / do_cleanup -> a burst of retention DELETEs) on EVERY loop
    iteration with no throttle, even when nothing is enabled (~hundreds of DELETE-0
    queries/sec, ~37-40% of a core while idle).
    FIX: wrap cleanup/do_cleanup with a minimum-interval gate so the retention pass runs
    at most once per SIGMA_PATCH_CLEANUP_INTERVAL seconds (default 300). run() keeps
    looping, but the expensive DB pass becomes a cheap no-op between intervals.
    Scheduler is a regular compiled class (no __pyx_vtab), so run()'s self.cleanup()
    call dispatches through Python and the wrapper takes effect.
    Optional: SIGMA_PATCH_LOOP_FLOOR>0 also enforces a minimum loop period (sleep) in
    case run()'s own interval is too small.

(2) RTP socket/fd hygiene (secondary)
    sigma.core.RTP.RTPGenericStreamer (created per dialog) self-creates a UDP socket when
    manageSocket is truthy. Its stop() close path is guarded and swallows failures, so an
    abandoned streamer can leak the owned socket/fd/port.
    FIX: wrap stop() to idempotently close the OWNED socket, and register a weakref
    finalizer to close it if the streamer is GC'd without stop(). Externally-managed
    sockets (manageSocket falsy) are never touched.

NOTE: the RTP *busy-spin* (queue.Empty: continue with no sleep, inside stream()'s loop)
is INSIDE a compiled method body and cannot be reached by a method-boundary wrapper. If
that specific spin recurs, the known lever is the pure-Python RTP.py swap, not this patch.

============================ CONTROLS (env) ============================
* SIGMA_PATCH_DISABLE=1            -> do nothing at all (no-op, without uninstalling).
* SIGMA_PATCH_DRY_RUN=1           -> log every decision but DO NOT change behavior
                                     (cleanup still runs; sockets still left as-is).
* SIGMA_PATCH_CLEANUP_INTERVAL=N  -> min seconds between retention cleanups (default 300).
* SIGMA_PATCH_LOOP_FLOOR=N        -> min seconds per scheduler loop iteration (default 0=off).
* SIGMA_PATCH_LOG=<path>          -> where events are appended
                                     (default /var/log/sigma_patch.log, falls back to tempdir).

Auto-loaded by the companion sigma_patch.pth dropped into site-packages.
"""

import os
import sys
import time
import logging
import tempfile

RTP_MODULE = "sigma.core.RTP"
SCHED_MODULE = "sigma.core.scheduler"

_PATCH_FLAG = "_sigma_patched"
_FINALIZER_FLAG = "_sigma_finalizer_registered"


def _read_config_file():
    """Read KEY=VALUE settings from a config file, so options work even when the
    service launcher doesn't propagate environment variables to the python process.
    Checked in order; first existing file wins. Env vars always take precedence."""
    paths = [os.environ.get("SIGMA_PATCH_CONF"),
             "/opt/sigma/etc/sigma_patch.conf",
             "/etc/sigma_patch.conf"]
    cfg = {}
    for p in paths:
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
            cfg["__source__"] = p
            break
        except Exception:
            pass
    return cfg


_CONF = _read_config_file()


def _get(name, default=None):
    """Resolve a setting: environment first, then config file, then default."""
    if name in os.environ:
        return os.environ[name]
    if name in _CONF:
        return _CONF[name]
    return default


_DISABLED = str(_get("SIGMA_PATCH_DISABLE", "")) in ("1", "true", "TRUE", "yes")
_DRY_RUN = str(_get("SIGMA_PATCH_DRY_RUN", "")) in ("1", "true", "TRUE", "yes")


def _env_float(name, default):
    try:
        return float(_get(name, default))
    except (TypeError, ValueError):
        return float(default)


_CLEANUP_INTERVAL = _env_float("SIGMA_PATCH_CLEANUP_INTERVAL", 300.0)
_LOOP_FLOOR = _env_float("SIGMA_PATCH_LOOP_FLOOR", 0.0)

_logger = logging.getLogger("sigma_patch")


def _log_file_path():
    p = _get("SIGMA_PATCH_LOG")
    if p:
        return p
    default = "/var/log/sigma_patch.log"
    try:
        d = os.path.dirname(default)
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return default
    except Exception:
        pass
    return os.path.join(tempfile.gettempdir(), "sigma_patch.log")


_LOG_PATH = _log_file_path()


def _log(msg, level=logging.INFO):
    line = "[sigma_patch] " + msg
    try:
        _logger.log(level, line)
    except Exception:
        pass
    try:
        with open(_LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _is_patchable(cls):
    try:
        setattr(cls, "_sigma_probe", True)
        delattr(cls, "_sigma_probe")
        return True
    except (TypeError, AttributeError):
        return False


# ============================ (1) SCHEDULER ============================

def _make_gated(cls, method_name, interval):
    """Wrap cls.method_name so it actually runs at most once per `interval` seconds
    (per instance). Between intervals it returns None without doing the work."""
    import functools

    orig = cls.__dict__.get(method_name, None)
    if orig is None:
        orig = getattr(cls, method_name, None)
    if orig is None:
        return False
    state_attr = "_sigma_last_" + method_name

    @functools.wraps(orig)
    def gated(self, *args, **kwargs):
        now = time.monotonic()
        last = getattr(self, state_attr, 0.0)
        due = (now - last) >= interval
        if not due:
            if _DRY_RUN:
                _log("DRY-RUN would throttle %s (%.0fs since last < %.0fs); running anyway"
                     % (method_name, now - last, interval))
                return orig(self, *args, **kwargs)
            return None  # skip the expensive retention pass this iteration
        try:
            setattr(self, state_attr, now)
        except Exception:
            pass
        return orig(self, *args, **kwargs)

    try:
        setattr(cls, method_name, gated)
        return True
    except (TypeError, AttributeError):
        return False


# Methods run() calls every loop iteration; throttling any of these caps the loop rate.
_LOOP_METHODS = ("monitorScenarios", "checkPlannings",
                 "scanDatabaseScenarios", "scanDatabaseTestCampaigns")


def _make_loop_throttle(cls, method_name, floor):
    """Wrap cls.method_name so consecutive calls of THIS method are >= `floor` seconds
    apart (sleeping to fill the gap). Per-method timestamps self-coordinate: once the
    first per-iteration method sleeps, the others called right after already have ~floor
    since their own previous call, so the net effect is ~one floor-sleep per loop pass
    (no compounding). This caps run()'s poll frequency without touching its compiled body."""
    import functools

    orig = cls.__dict__.get(method_name, None)
    if orig is None:
        orig = getattr(cls, method_name, None)
    if orig is None or not callable(orig):
        return False
    state_attr = "_sigma_period_" + method_name

    @functools.wraps(orig)
    def throttled(self, *args, **kwargs):
        now = time.monotonic()
        last = getattr(self, state_attr, 0.0)
        gap = now - last
        if 0.0 <= gap < floor and not _DRY_RUN:
            time.sleep(floor - gap)
        try:
            setattr(self, state_attr, time.monotonic())
        except Exception:
            pass
        return orig(self, *args, **kwargs)

    try:
        setattr(cls, method_name, throttled)
        return True
    except (TypeError, AttributeError):
        return False


def _apply_scheduler_patch(module):
    if _DISABLED:
        _log("disabled via SIGMA_PATCH_DISABLE - scheduler untouched")
        return
    cls = getattr(module, "Scheduler", None)
    if cls is None:
        _log("Scheduler class not found in %s" % SCHED_MODULE, level=logging.WARNING)
        return
    if getattr(cls, _PATCH_FLAG, False):
        return
    if not _is_patchable(cls):
        _log("CANNOT patch Scheduler - it is a compiled cdef extension type. "
             "On-box throttle impossible; a recompile is required.", level=logging.ERROR)
        return

    gated = []
    for name in ("cleanup", "do_cleanup"):
        if _make_gated(cls, name, _CLEANUP_INTERVAL):
            gated.append(name)

    # Cap the poll-loop frequency (kills the SELECT-poll busy-spin under load).
    floored = []
    if _LOOP_FLOOR > 0:
        for name in _LOOP_METHODS:
            if _make_loop_throttle(cls, name, _LOOP_FLOOR):
                floored.append(name)

    try:
        setattr(cls, _PATCH_FLAG, True)
    except Exception:
        pass

    if gated:
        _log("scheduler CPU hotfix ACTIVE%s: throttled %s to >=%.0fs%s. log=%s"
             % (" [DRY-RUN]" if _DRY_RUN else "", "+".join(gated), _CLEANUP_INTERVAL,
                (" + loop floor %.3fs on %s" % (_LOOP_FLOOR, "+".join(floored)))
                if floored else " (loop floor OFF)", _LOG_PATH))
    else:
        _log("scheduler: no cleanup/do_cleanup method found to throttle "
             "(retention DELETEs may be inline in run(); see SIGMA_PATCH_DB_THROTTLE note)",
             level=logging.WARNING)


# ============================ (2) RTP ============================

def _close_owned_socket(self, where):
    try:
        import socket as _socket
    except Exception:
        return
    try:
        if not getattr(self, "manageSocket", False):
            return
        sock = getattr(self, "socket", None)
        if sock is None or not isinstance(sock, _socket.socket):
            return
        try:
            fd = sock.fileno()
        except Exception:
            fd = -1
        if fd == -1:
            return
        port = getattr(self, "localPort", None)
        if _DRY_RUN:
            _log("DRY-RUN would close owned RTP socket (%s) fd=%s port=%s" % (where, fd, port))
            return
        sock.close()
        _log("closed owned RTP socket (%s) fd=%s port=%s" % (where, fd, port))
    except Exception as e:
        _log("error closing owned RTP socket (%s): %r" % (where, e), level=logging.WARNING)


def _register_finalizer(self):
    try:
        import weakref
        import socket as _socket
    except Exception:
        return
    try:
        if getattr(self, _FINALIZER_FLAG, False):
            return
        if not getattr(self, "manageSocket", False):
            return
        sock = getattr(self, "socket", None)
        if sock is None or not isinstance(sock, _socket.socket):
            return
        port = getattr(self, "localPort", None)

        def _finalize(s=sock, p=port):
            try:
                if s.fileno() == -1:
                    return
            except Exception:
                return
            if _DRY_RUN:
                _log("DRY-RUN would close abandoned RTP socket (finalizer) port=%s" % p)
                return
            try:
                s.close()
                _log("closed abandoned RTP socket (finalizer) port=%s" % p)
            except Exception as e:
                _log("finalizer close error port=%s: %r" % (p, e), level=logging.WARNING)

        weakref.finalize(self, _finalize)
        setattr(self, _FINALIZER_FLAG, True)
    except Exception as e:
        _log("could not register finalizer: %r" % e, level=logging.WARNING)


def _wrap_streamer_class(cls):
    import functools

    if getattr(cls, _PATCH_FLAG, False):
        return True

    orig_init = cls.__dict__.get("__init__", None) or cls.__init__
    orig_start = cls.__dict__.get("start", None)
    orig_stop = cls.__dict__.get("stop", None)

    @functools.wraps(orig_init)
    def patched_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        _register_finalizer(self)

    new_attrs = {"__init__": patched_init}

    if orig_start is not None:
        @functools.wraps(orig_start)
        def patched_start(self, *args, **kwargs):
            result = orig_start(self, *args, **kwargs)
            _register_finalizer(self)
            return result
        new_attrs["start"] = patched_start

    if orig_stop is not None:
        @functools.wraps(orig_stop)
        def patched_stop(self, *args, **kwargs):
            try:
                return orig_stop(self, *args, **kwargs)
            finally:
                _close_owned_socket(self, where="stop")
        new_attrs["stop"] = patched_stop

    try:
        for name, fn in new_attrs.items():
            setattr(cls, name, fn)
        setattr(cls, _PATCH_FLAG, True)
        _log("patched %s (%s) for owned-socket leak%s"
             % (cls.__name__, "+".join(sorted(new_attrs)),
                " [DRY-RUN]" if _DRY_RUN else ""))
        return True
    except (TypeError, AttributeError) as e:
        _log("CANNOT patch %s - compiled cdef extension type (%r); recompile required."
             % (cls.__name__, e), level=logging.ERROR)
        return False


def _apply_rtp_patch(module):
    if _DISABLED:
        _log("disabled via SIGMA_PATCH_DISABLE - RTP untouched")
        return
    patched_any = False
    primary = getattr(module, "RTPGenericStreamer", None)
    candidates = []
    if primary is not None:
        candidates.append(primary)
    for name in dir(module):
        if not name.endswith("Streamer"):
            continue
        obj = getattr(module, name, None)
        if obj is primary or not isinstance(obj, type):
            continue
        if hasattr(obj, "stop"):
            candidates.append(obj)
    for cls in candidates:
        patched_any = _wrap_streamer_class(cls) or patched_any
    if patched_any:
        _log("RTP socket hotfix ACTIVE%s. log=%s"
             % (" [DRY-RUN]" if _DRY_RUN else "", _LOG_PATH))


# ============================ import plumbing ============================

_TARGETS = {
    SCHED_MODULE: _apply_scheduler_patch,
    RTP_MODULE: _apply_rtp_patch,
}


def _dispatch(module):
    fn = _TARGETS.get(getattr(module, "__name__", None))
    if fn is None:
        return
    try:
        fn(module)
    except Exception as e:
        _log("error applying patch to %s: %r" % (module.__name__, e), level=logging.ERROR)


class _PostImportFinder:
    """Meta-path finder that wraps the real loader of each target module to run our
    patch right after the module loads. It delegates; it never claims to be the loader."""

    def find_spec(self, fullname, path, target=None):
        if fullname not in _TARGETS:
            return None
        try:
            idx = sys.meta_path.index(self)
        except ValueError:
            idx = -1
        for finder in sys.meta_path[idx + 1:]:
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            try:
                spec = find(fullname, path, target)
            except Exception:
                spec = None
            if spec is not None and spec.loader is not None:
                self._wrap_loader(spec.loader)
                return spec
        return None

    @staticmethod
    def _wrap_loader(loader):
        if getattr(loader, "_sigma_patch_wrapped", False):
            return
        _orig_loader = loader.exec_module

        def _load_and_patch(module):
            _orig_loader(module)
            _dispatch(module)

        try:
            loader.exec_module = _load_and_patch
            loader._sigma_patch_wrapped = True
        except Exception:
            pass


def install():
    if _CONF.get("__source__"):
        _log("loaded config from %s (loop_floor=%.3fs cleanup_interval=%.0fs)"
             % (_CONF["__source__"], _LOOP_FLOOR, _CLEANUP_INTERVAL))
    if _DISABLED:
        _log("SIGMA_PATCH_DISABLE set - hook not installed")
        return
    # Patch anything already imported (import order beat us).
    for name in _TARGETS:
        mod = sys.modules.get(name)
        if mod is not None:
            _dispatch(mod)
    # And hook future imports of the targets.
    if not any(isinstance(f, _PostImportFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _PostImportFinder())
        _log("post-import hook installed for %s%s"
             % (", ".join(_TARGETS), " [DRY-RUN]" if _DRY_RUN else ""))


try:
    install()
except Exception as _e:
    try:
        _log("install() failed: %r" % _e, level=logging.ERROR)
    except Exception:
        pass
