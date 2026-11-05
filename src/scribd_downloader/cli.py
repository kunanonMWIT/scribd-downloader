"""Command line interface for scribd_downloader."""

from __future__ import annotations

import argparse
from typing import Optional, Sequence

from . import __version__
from .core import InvalidScribdUrlError, ScribdDownloadError, download


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``scribd-downloader`` command."""
    parser = argparse.ArgumentParser(
        prog="scribd-downloader",
        description="Download a Scribd document as a PDF using headless Chrome.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        help=(
            "Scribd document URL. When omitted, the downloader prompts for "
            "it interactively."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output PDF path. Defaults to a filename derived from the URL.",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Run Chrome with a visible window instead of headless.",
    )
    parser.add_argument(
        "--cdp-timeout",
        type=int,
        help="ChromeDriver command timeout in seconds for Page.printToPDF (default: 600).",
    )
    parser.add_argument(
        "--page-load-timeout",
        type=int,
        help="Per-batch page load timeout in seconds (default: 120).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        help="Pages kept fully loaded in Chrome per export batch (default: 8).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        help="Parallel page loads when prefetching the document (default: 8).",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    Run the exporter from the command line.

    Returns a process exit code: 0 on success, 1 on failure.
    """
    args = build_parser().parse_args(argv)

    url = args.url
    if not url:
        url = input("Input link Scribd: ").strip()

    overrides = {}
    if args.visible:
        overrides["headless"] = False
    if args.cdp_timeout is not None:
        overrides["cdp_timeout_seconds"] = args.cdp_timeout
    if args.page_load_timeout is not None:
        overrides["page_load_timeout_seconds"] = args.page_load_timeout
    if args.batch_size is not None:
        overrides["export_batch_size"] = args.batch_size
    if args.concurrency is not None:
        overrides["page_load_concurrency"] = args.concurrency

    try:
        download(
            url,
            output=args.output,
            log=None if args.quiet else print,
            **overrides,
        )
    except InvalidScribdUrlError:
        print("Error: Please provide a valid Scribd document URL")
        print(
            "Example: "
            "https://www.scribd.com/document/"
            "123456789/Document-Title"
        )
        print(
            "Example: "
            "https://www.scribd.com/doc/"
            "123456789/Document-Title"
        )
        return 1
    except ScribdDownloadError:
        # download() already reported the failure through its log callable.
        return 1

    return 0
