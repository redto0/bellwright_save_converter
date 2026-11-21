#!/usr/bin/env python3
"""Bellwright Save Converter.

Double-click or run with no arguments for the window; see --help for the
command line. Keep the ``bellwright`` folder next to this file.
"""

import sys

from bellwright.cli import main

if __name__ == "__main__":
    sys.exit(main())
