#!/usr/bin/env python3
"""Compatibility entry point: `python run.py new|update|export` still works.

The implementation lives in the `spa_sitemap` package; prefer
`python -m spa_sitemap` or the installed `spa-sitemap` console script.
"""

import sys

from spa_sitemap.cli import main

if __name__ == "__main__":
    sys.exit(main())
