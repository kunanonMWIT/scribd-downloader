"""Allow running the package with ``python -m scribd_downloader``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
