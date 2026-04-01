"""Allow running the converter as ``python -m converter``."""

import sys

from .cli import main

sys.exit(main())
