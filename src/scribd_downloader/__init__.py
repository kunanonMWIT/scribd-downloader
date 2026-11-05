"""
Scribd Downloader - download Scribd documents as PDF from Python.

Quick start::

    from scribd_downloader import download

    pdf_path = download("https://www.scribd.com/document/123456789/Document-Title")
"""

from scribd_downloader.core import (
    DownloadOptions,
    InvalidScribdUrlError,
    ScribdDownloadError,
    convert_scribd_link,
    download,
    extract_document_id,
    get_filename_from_url,
)

__version__ = "1.0.0"

__all__ = [
    "DownloadOptions",
    "InvalidScribdUrlError",
    "ScribdDownloadError",
    "__version__",
    "convert_scribd_link",
    "download",
    "extract_document_id",
    "get_filename_from_url",
]
