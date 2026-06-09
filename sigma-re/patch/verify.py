#!/usr/bin/env python3
"""
verify.py - confirm the sigma CPU/leak hotfix is installed and can attach.

Run with the SAME interpreter that runs sigma:
    /opt/sigma/bin/python verify.py   (or the venv python that imports sigma)

Exit 0 = the scheduler CPU throttle attached (the important one).
Exit 1 = a problem (not importable, or a class is an unpatchable cdef extension type).
"""
import sys


def _is_patchable(cls):
    try:
        setattr(cls, "_sigma_probe", True)
        delattr(cls, "_sigma_probe")
        return True
    except (TypeError, AttributeError):
        return False


def main():
    print("python:", sys.executable)
    try:
        import sigma_patch
        print("sigma_patch: imported OK; log =", getattr(sigma_patch, "_LOG_PATH", "?"))
        print("cleanup throttle interval:", getattr(sigma_patch, "_CLEANUP_INTERVAL", "?"), "s")
    except Exception as e:
        print("FAIL: could not import sigma_patch:", repr(e))
        return 1

    ok_sched = _check_scheduler()
    _check_rtp()  # secondary; do not gate exit code on it

    if ok_sched:
        print("\nRESULT: OK - scheduler CPU throttle is active.")
        return 0
    print("\nRESULT: scheduler throttle NOT active - see messages above.")
    return 1


def _check_scheduler():
    try:
        import sigma.core.scheduler as sched
    except Exception as e:
        print("FAIL: could not import sigma.core.scheduler:", repr(e))
        return False
    cls = getattr(sched, "Scheduler", None)
    if cls is None:
        print("FAIL: Scheduler class not found")
        return False
    patched = getattr(cls, "_sigma_patched", False)
    print("Scheduler patched:", patched,
          "| patchable:", "yes" if _is_patchable(cls) else "NO (cdef extension type)")
    has_cleanup = hasattr(cls, "cleanup") or hasattr(cls, "do_cleanup")
    print("Scheduler has cleanup/do_cleanup:", has_cleanup)
    if not has_cleanup:
        print("  NOTE: retention DELETEs may be inline in run(); method throttle cannot catch")
        print("        them. In that case the fix needs a recompile.")
    return bool(patched and has_cleanup)


def _check_rtp():
    try:
        import sigma.core.RTP as rtp
    except Exception as e:
        print("RTP: could not import (%r) - skipping" % e)
        return
    cls = getattr(rtp, "RTPGenericStreamer", None)
    if cls is None:
        print("RTP: RTPGenericStreamer not found")
        return
    print("RTPGenericStreamer patched:", getattr(cls, "_sigma_patched", False),
          "| patchable:", "yes" if _is_patchable(cls) else "NO (cdef extension type)")


if __name__ == "__main__":
    sys.exit(main())
