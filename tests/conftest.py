# test_gencall.py is a standalone, self-running test script (it executes its
# whole suite at import time and calls sys.exit()), not a pytest module. Running
# it under pytest collection raises SystemExit and aborts the whole run, so we
# exclude it here. CI runs it directly via `python tests/test_gencall.py`.
collect_ignore = ["test_gencall.py"]
