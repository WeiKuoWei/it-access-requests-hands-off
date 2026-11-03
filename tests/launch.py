"""Test-only launcher: import everything first, then wait for a barrier file, then run the real CLI.

Python start-up and imports take far longer than the work in these tests, so without a barrier two
"simultaneous" operators would run one after the other. This makes them start within milliseconds.
Usage: python launch.py BARRIER_FILE [cli args...]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patches  # noqa: E402  (also puts the repository root on sys.path)

patches.apply()
from intake.cli import main  # noqa: E402  (imported before waiting so start-up cost is already paid)

patches.wait_for(sys.argv[1])
sys.exit(main(sys.argv[2:]))
