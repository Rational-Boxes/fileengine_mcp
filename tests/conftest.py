"""Make the src-layout package importable for unit tests without an install
(live phase tests still rely on PYTHONPATH including ../python_interface)."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
