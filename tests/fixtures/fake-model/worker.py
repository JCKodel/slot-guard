"""A real tool process for integration tests; never calls an API."""
from pathlib import Path
import sys
import time
home, name, delay = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
(home/(name+'.tool-started')).write_text(str(time.time_ns()))
time.sleep(delay)
(home/(name+'.tool-finished')).write_text(str(time.time_ns()))
