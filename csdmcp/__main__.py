"""`python -m csdmcp` 的進入點。"""
import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
