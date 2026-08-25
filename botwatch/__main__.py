"""`python -m botwatch` -- run the CLI."""

import sys

from . import _cli

if __name__ == '__main__':
    sys.exit(_cli.main())
