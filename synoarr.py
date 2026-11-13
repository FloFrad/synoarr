#!/usr/bin/env python3
"""Lanceur : python3 synoarr.py <commande>  (voir python3 synoarr.py --help)."""
import os
import sys

if sys.version_info < (3, 8):
    sys.stderr.write("synoarr nécessite Python 3.8 ou plus récent (version actuelle : %s)\n" % sys.version.split()[0])
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synoarr.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
