"""Generated -inf cleanup helper: deletes only our own gencall_loop_*.csv."""
import os, tempfile
from gencall.core.loop_engine import LoopEngine


def _mk(name):
    p = os.path.join(tempfile.gettempdir(), name)
    with open(p, "w") as f:
        f.write("x")
    return p


def test_unlink_inf_removes_generated_inf():
    p = _mk("gencall_loop_unittest_abc.csv")
    LoopEngine._unlink_inf(p)
    assert not os.path.exists(p)


def test_unlink_inf_refuses_non_inf_paths():
    # a node pool / arbitrary file must NEVER be deleted by this helper
    pool = _mk("numbers_unittest_keep.csv")
    other = _mk("important_unittest.txt")
    try:
        LoopEngine._unlink_inf(pool)
        LoopEngine._unlink_inf(other)
        LoopEngine._unlink_inf("")          # empty
        LoopEngine._unlink_inf(None)        # None
        LoopEngine._unlink_inf("/tmp/gencall_loop_does_not_exist.csv")  # missing
        assert os.path.exists(pool) and os.path.exists(other)
    finally:
        os.unlink(pool); os.unlink(other)
