"""Isolated host network helper entry point."""

import sys
from pathlib import Path

from .host_network import serve

if __name__ == "__main__":
    serve(Path(sys.argv[1]))
