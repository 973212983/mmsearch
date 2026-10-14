import os
import time

_t0 = time.perf_counter()

if os.environ.get("MMSEARCH_TIMING"):
    try:
        from mmsearch import config as _cfg
        _cfg.START_T0 = _t0
        _cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_cfg.DATA_DIR / "startup-timing.log", "a", encoding="utf-8") as _f:
            _f.write(f"[{_t0 - _t0:8.2f}s] main.py entry\n")
    except OSError:
        pass

import sys

from mmsearch.app import main

if __name__ == "__main__":
    sys.exit(main())
