"""Pytest bootstrap for the pipeline test suite.

The pipeline modules use top-level imports (`import config`, `from common.x import y`),
so the pipeline directory must be on sys.path when the tests run. Adding it here keeps
`pytest` runnable from anywhere (repo root or pipeline/).
"""

import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))
