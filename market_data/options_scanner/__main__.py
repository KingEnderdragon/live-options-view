"""Run the option scanner: python -m market_data.options_scanner."""
import sys
from .cli import main

if __name__ == "__main__":
    sys.exit(main())
