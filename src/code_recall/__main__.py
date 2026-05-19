"""Allow running as `python -m code_recall`."""

import sys

from code_recall.cli import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
