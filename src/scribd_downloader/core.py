"""
Scribd Document Downloader
==========================

A Selenium-based utility that loads a Scribd embed and saves it as a PDF.

Public entry points
-------------------

- ``download(url, ...)``: download a Scribd document as a PDF from Python.
- ``DownloadOptions``: dataclass with every tunable knob.
- ``convert_scribd_link`` / ``extract_document_id`` / ``get_filename_from_url``:
  URL helpers.
- ``ScribdDownloadError`` / ``InvalidScribdUrlError``: catchable exceptions.

Key behaviors:
1. Converts a Scribd document URL to the embed/content URL.
2. Opens the document in Chrome.
3. Loads document pages in bounded batches (with a scrolling fallback).
4. Removes UI overlays without stripping layout classes needed for rendering.
5. Waits for fonts, images, and page geometry to settle.
6. Saves the PDF through Chrome DevTools Protocol with a larger timeout.

The ``SCRIBD_*`` environment variables keep their original meaning and are
honored by ``DownloadOptions.from_env()``, which ``download()`` uses whenever
it is called without explicit options. ``parallel_shards`` (default 1, also
via the ``SCRIBD_PARALLEL_SHARDS`` environment variable) splits the page
export across several concurrent Chrome instances and merges the results.
"""

from __future__ import annotations

import base64
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options


SCRIBD_DOCUMENT_URL_PATTERN = re.compile(
    r"https://www\.scribd\.com/(?:document|doc)/(\d+)/"
)

DEFAULT_PAPER_WIDTH_INCHES = 7.25
DEFAULT_PAPER_HEIGHT_INCHES = 10.5

LogFunction = Callable[[str], None]


def _noop_log(message: str) -> None:
    """Discard progress messages (used when ``log=None``)."""


class ScribdDownloadError(RuntimeError):
    """Raised when a Scribd document cannot be loaded, printed, or saved."""


class InvalidScribdUrlError(ScribdDownloadError, ValueError):
    """Raised when a URL is not a valid Scribd document URL."""


@dataclass(frozen=True)
class DownloadOptions:
    """
    Every tunable knob of the downloader.

    Defaults mirror the original environment-variable driven configuration.
    Use :meth:`from_env` to build an instance from the ``SCRIBD_*``
    environment variables, or construct one directly (or via
    ``dataclasses.replace``) to override individual fields.
    """

    headless: bool = True
    cdp_timeout_seconds: int = 600
    render_settle_timeout_seconds: int = 30
    scroll_delay_seconds: float = 0.15
    page_load_concurrency: int = 8
    page_load_timeout_seconds: int = 120
    export_batch_size: int = 8
    parallel_shards: int = 1

    @classmethod
    def from_env(cls) -> "DownloadOptions":
        """Read the ``SCRIBD_*`` environment variables into options."""
        return cls(
            headless=(
                os.getenv("SCRIBD_HEADLESS", "1").strip().lower()
                not in {"0", "false", "no"}
            ),
            cdp_timeout_seconds=int(os.getenv("SCRIBD_CDP_TIMEOUT", "600")),
            render_settle_timeout_seconds=int(
                os.getenv("SCRIBD_RENDER_SETTLE_TIMEOUT", "30")
            ),
            scroll_delay_seconds=float(os.getenv("SCRIBD_SCROLL_DELAY", "0.15")),
            page_load_concurrency=max(
                1,
                int(os.getenv("SCRIBD_PAGE_LOAD_CONCURRENCY", "8")),
            ),
            page_load_timeout_seconds=max(
                10,
                int(os.getenv("SCRIBD_PAGE_LOAD_TIMEOUT", "120")),
            ),
            export_batch_size=max(
                1,
                int(os.getenv("SCRIBD_EXPORT_BATCH_SIZE", "8")),
            ),
            parallel_shards=max(
                1,
                min(64, int(os.getenv("SCRIBD_PARALLEL_SHARDS", "1"))),
            ),
        )


def extract_document_id(url):
    """
    Extract the numeric document id from a Scribd URL.

    Args:
        url: Scribd document URL such as
            https://www.scribd.com/document/123456789/Document-Title
            or https://www.scribd.com/doc/123456789/Document-Title

    Returns:
        The document id as a string, or None if no id can be extracted.
    """
    match = SCRIBD_DOCUMENT_URL_PATTERN.search(url or "")
    return match.group(1) if match else None


def convert_scribd_link(url):
    """
    Convert a Scribd document URL to the embed/content URL.

    Args:
        url: Standard Scribd URL such as
            https://www.scribd.com/document/123456789/Document-Title
            or https://www.scribd.com/doc/123456789/Document-Title

    Returns:
        The embeddable content URL.

    Raises:
        InvalidScribdUrlError: If no document id can be extracted.
    """
    document_id = extract_document_id(url)
    if document_id is None:
        raise InvalidScribdUrlError(
            "No Scribd document id found in URL. Expected something like "
            "https://www.scribd.com/document/123456789/Document-Title or "
            "https://www.scribd.com/doc/123456789/Document-Title."
        )

    return f"https://www.scribd.com/embeds/{document_id}/content"


def get_filename_from_url(url):
    """
    Build an output filename from the last URL path segment.

    Args:
        url: Scribd document URL.

    Returns:
        Filename ending in ".pdf".
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    last_segment = path.split("/")[-1] if path else "scribd_document"
    return f"{unquote(last_segment)}.pdf"


def build_chrome_options(runtime_profile_dir, options):
    """Create Chrome options for reliable headless PDF generation."""
    chrome_options = Options()

    if options.headless:
        chrome_options.add_argument("--headless=new")

    chrome_options.add_argument("--window-size=1600,2200")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--remote-debugging-port=0")
    chrome_options.add_argument(f"--user-data-dir={runtime_profile_dir}")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--force-color-profile=srgb")
    chrome_options.add_argument("--hide-scrollbars")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    return chrome_options


def configure_command_timeout(driver, timeout_seconds):
    """
    Increase the Selenium HTTP timeout used for ChromeDriver commands.

    Large image-heavy documents can spend minutes inside Page.printToPDF before
    ChromeDriver responds, so the default 120 second timeout is too small.
    """
    executor = getattr(driver, "command_executor", None)
    if executor is None:
        return

    client_config = getattr(executor, "client_config", None)
    if client_config is None:
        client_config = getattr(executor, "_client_config", None)

    if client_config is not None:
        client_config.timeout = timeout_seconds


def hide_cookie_dialogs(driver):
    """Dismiss and remove common cookie, consent, and privacy banners."""
    driver.execute_script(
        """
        const closeButtonSelectors = [
            '[class*="cookie"] [class*="close"]',
            '[class*="cookie"] [class*="dismiss"]',
            '[class*="cookie"] button[aria-label*="close"]',
            '[class*="cookie"] button[aria-label*="Close"]',
            '[class*="consent"] [class*="close"]',
            '[class*="consent"] [class*="dismiss"]',
            '[class*="banner"] [class*="close"]',
            '[class*="banner"] [class*="dismiss"]',
            '[class*="notice"] [class*="close"]',
            '[class*="notice"] [class*="dismiss"]',
            'button[class*="close"]',
            'button[aria-label="Close"]',
            'button[aria-label="close"]',
            'button[aria-label="Dismiss"]',
            '[data-dismiss]',
            '[role="button"][class*="close"]'
        ];

        closeButtonSelectors.forEach((selector) => {
            try {
                document.querySelectorAll(selector).forEach((button) => button.click());
            } catch (error) {}
        });

        const cookieSelectors = [
            '[class*="cookie"]',
            '[class*="Cookie"]',
            '[class*="consent"]',
            '[class*="Consent"]',
            '[class*="gdpr"]',
            '[class*="GDPR"]',
            '[id*="cookie"]',
            '[id*="Cookie"]',
            '[id*="consent"]',
            '[id*="gdpr"]',
            '[class*="privacy-notice"]',
            '[class*="Privacy"]',
            '[class*="cookie-banner"]',
            '[class*="cookie-notice"]',
            '[class*="cookie-popup"]',
            '[class*="cookie-modal"]',
            '[class*="CookieConsent"]',
            '[class*="notice-banner"]',
            '.cc-window',
            '.cc-banner',
            '#onetrust-consent-sdk',
            '#onetrust-banner-sdk',
            '.evidon-banner',
            '.truste_box_overlay',
            '[class*="osano-cm"]',
            '[id*="osano"]'
        ];

        cookieSelectors.forEach((selector) => {
            try {
                document.querySelectorAll(selector).forEach((element) => element.remove());
            } catch (error) {}
        });

        document.querySelectorAll('*').forEach((element) => {
            try {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                const text = (element.innerText || '').toLowerCase();
                const fixedAtTop =
                    (style.position === 'fixed' || style.position === 'sticky') &&
                    rect.top < 100;

                if (
                    fixedAtTop &&
                    (
                        text.includes('cookie') ||
                        text.includes('privacy') ||
                        text.includes('consent') ||
                        text.includes('analytics') ||
                        text.includes('advertising') ||
                        text.includes('personalization')
                    )
                ) {
                    element.remove();
                }
            } catch (error) {}
        });
        """
    )


def scroll_through_pages(driver, scroll_delay_seconds, log=_noop_log):
    """
    Scroll through all detected pages until the page count stabilizes.

    Scribd lazily renders more page nodes while scrolling, so a single snapshot
    of "[class*='page']" is not always enough for long documents.
    """
    scrolled_count = 0
    stable_rounds = 0
    last_total_pages = -1

    while stable_rounds < 2:
        page_elements = driver.find_elements("css selector", "[class*='page']")
        total_pages = len(page_elements)

        if total_pages == 0:
            log("No page elements were detected.")
            return 0

        if total_pages == last_total_pages:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_total_pages = total_pages

        if scrolled_count == 0:
            log(f"Found {total_pages} pages, scrolling...")
        elif total_pages > scrolled_count:
            log(f"Detected {total_pages} pages after lazy loading, continuing...")

        for index in range(scrolled_count, total_pages):
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});",
                page_elements[index],
            )
            time.sleep(scroll_delay_seconds)

            if (index + 1) % 10 == 0:
                log(f"  Scrolled {index + 1}/{total_pages} pages...")

        scrolled_count = total_pages
        time.sleep(0.5)

    log(f"All {scrolled_count} pages loaded.")
    return scrolled_count


def load_all_pages(driver, options, log=_noop_log):
    """Load Scribd pages directly, without simulating user scrolling."""
    page_count = driver.execute_script(
        """
        return window.docManager && window.docManager.pages
            ? Object.values(window.docManager.pages).filter(Boolean).length
            : 0;
        """
    )
    batch_count = max(
        1,
        (page_count + options.page_load_concurrency - 1)
        // options.page_load_concurrency,
    )
    document_timeout_seconds = (
        options.page_load_timeout_seconds * batch_count
    )
    configure_command_timeout(driver, document_timeout_seconds + 10)
    driver.set_script_timeout(document_timeout_seconds + 10)

    result = driver.execute_async_script(
        """
        const concurrency = arguments[0];
        const timeoutMs = arguments[1];
        const documentTimeoutMs = arguments[2];
        const done = arguments[arguments.length - 1];
        const manager = window.docManager;

        if (!manager || !manager.pages) {
            done({supported: false});
            return;
        }

        const pages = Object.values(manager.pages).filter(Boolean);
        const pending = pages.filter((page) => !page.innerPageElem);
        const active = new Map();
        const failed = [];
        const startedAt = Date.now();
        let nextIndex = 0;
        let completed = pages.length - pending.length;
        let finished = false;

        function finish() {
            if (finished) {
                return;
            }

            finished = true;
            clearInterval(timer);

            pages.forEach((page) => {
                if (!page.innerPageElem) {
                    return;
                }

                try {
                    page.display();
                } catch (error) {}

                try {
                    page.turnOnImages();
                } catch (error) {}
            });

            done({
                supported: true,
                total: pages.length,
                loaded: pages.filter((page) => page.innerPageElem).length,
                failed,
                elapsedMs: Date.now() - startedAt
            });
        }

        function launchMore() {
            while (active.size < concurrency && nextIndex < pending.length) {
                const page = pending[nextIndex++];

                try {
                    if (!page.loadHasStarted) {
                        page.load();
                    }

                    active.set(page.pageNum, {
                        page,
                        startedAt: Date.now()
                    });
                } catch (error) {
                    failed.push({
                        pageNum: page.pageNum,
                        reason: String(error)
                    });
                }
            }

            if (completed + failed.length >= pages.length) {
                finish();
            }
        }

        const timer = setInterval(() => {
            for (const [pageNum, state] of active) {
                if (state.page.innerPageElem) {
                    active.delete(pageNum);
                    completed += 1;
                    continue;
                }

                if (Date.now() - state.startedAt >= timeoutMs) {
                    active.delete(pageNum);
                    failed.push({
                        pageNum,
                        reason: 'page load timed out'
                    });
                }
            }

            if (Date.now() - startedAt >= documentTimeoutMs) {
                for (const [pageNum] of active) {
                    failed.push({
                        pageNum,
                        reason: 'document load timed out'
                    });
                }
                active.clear();
                finish();
                return;
            }

            launchMore();
        }, 50);

        launchMore();
        """,
        options.page_load_concurrency,
        options.page_load_timeout_seconds * 1000,
        document_timeout_seconds * 1000,
    )

    if not result.get("supported"):
        log("Direct page loader unavailable; using scrolling fallback.")
        return scroll_through_pages(driver, options.scroll_delay_seconds, log)

    total_pages = result["total"]
    loaded_pages = result["loaded"]
    elapsed_seconds = result["elapsedMs"] / 1000

    log(
        f"Loaded {loaded_pages}/{total_pages} pages directly "
        f"in {elapsed_seconds:.2f}s "
        f"(concurrency: {options.page_load_concurrency})."
    )

    if result["failed"]:
        failed_pages = ", ".join(
            str(item["pageNum"])
            for item in result["failed"]
        )
        raise ScribdDownloadError(
            "Failed to load Scribd page(s): "
            f"{failed_pages}"
        )

    return loaded_pages


def prepare_document_for_print(driver, log=_noop_log):
    """
    Remove UI chrome and make the scroll containers printable.

    The old version removed the .document_scroller class entirely, which can
    break descendant CSS needed by math- and font-heavy documents. We keep the
    class and only override the few layout properties that interfere with print.
    """
    result = driver.execute_script(
        """
        const removed = { toolbarTop: false, toolbarBottom: false, containers: 0 };

        const toolbarTop = document.querySelector('.toolbar_top');
        if (toolbarTop) {
            toolbarTop.remove();
            removed.toolbarTop = true;
        }

        const toolbarBottom = document.querySelector('.toolbar_bottom');
        if (toolbarBottom) {
            toolbarBottom.remove();
            removed.toolbarBottom = true;
        }

        document.querySelectorAll('.document_scroller').forEach((element) => {
            element.setAttribute('data-scribd-print-root', 'true');
            element.style.position = 'static';
            element.style.top = 'auto';
            element.style.bottom = 'auto';
            element.style.left = 'auto';
            element.style.right = 'auto';
            element.style.overflow = 'visible';
            element.style.maxHeight = 'none';
            element.style.height = 'auto';
            element.style.margin = '0';
            element.style.padding = '0';
            removed.containers += 1;
        });

        return removed;
        """
    )

    if result["toolbarTop"]:
        log("Top toolbar removed.")
    if result["toolbarBottom"]:
        log("Bottom toolbar removed.")

    log(f"Adjusted {result['containers']} scroll containers for print.")


def inject_print_styles(driver, log=_noop_log):
    """Install conservative print CSS without hiding Scribd document content."""
    driver.execute_script(
        """
        const existing = document.getElementById('scribd-print-styles');
        if (existing) {
            existing.remove();
        }

        const style = document.createElement('style');
        style.id = 'scribd-print-styles';
        style.textContent = `
            [class*="cookie"],
            [class*="Cookie"],
            [class*="consent"],
            [class*="Consent"],
            [class*="gdpr"],
            [class*="privacy-notice"],
            [class*="notice-banner"],
            [id*="cookie"],
            [id*="consent"],
            [class*="osano-cm"],
            [id*="osano"] {
                display: none !important;
                visibility: hidden !important;
                opacity: 0 !important;
                height: 0 !important;
                overflow: hidden !important;
            }

            [data-scribd-print-root="true"],
            .document_scroller {
                position: static !important;
                top: auto !important;
                right: auto !important;
                bottom: auto !important;
                left: auto !important;
                overflow: visible !important;
                height: auto !important;
                max-height: none !important;
                margin: 0 !important;
                padding: 0 !important;
            }

            @media print {
                html,
                body {
                    margin: 0 !important;
                    padding: 0 !important;
                    -webkit-print-color-adjust: exact !important;
                    print-color-adjust: exact !important;
                }

                .toolbar_top,
                .toolbar_bottom {
                    display: none !important;
                }

                [data-scribd-print-root="true"],
                .document_scroller {
                    position: static !important;
                    top: auto !important;
                    right: auto !important;
                    bottom: auto !important;
                    left: auto !important;
                    overflow: visible !important;
                    height: auto !important;
                    max-height: none !important;
                    margin: 0 !important;
                    padding: 0 !important;
                }

                mjx-container,
                .MathJax,
                .katex,
                math,
                svg {
                    visibility: visible !important;
                    overflow: visible !important;
                }
            }
        `;

        document.head.appendChild(style);
        """
    )

    log("Print CSS injected.")


def wait_for_render_stability(driver, timeout_seconds, log=_noop_log):
    """
    Wait for fonts, images, and page dimensions to settle before printing.

    This lowers the risk of exporting before math glyphs, SVG content, or web
    fonts finish rendering.
    """
    driver.set_script_timeout(timeout_seconds + 5)

    try:
        result = driver.execute_async_script(
            """
            const settleBudgetMs = arguments[0];
            const done = arguments[arguments.length - 1];
            const start = performance.now();
            let stableTicks = 0;
            let lastSample = '';

            function sample() {
                const pages = Array.from(document.querySelectorAll("[class*='page']"));
                const heights = pages.slice(0, 12).map((element) =>
                    Math.round(element.getBoundingClientRect().height)
                );
                const pendingImages = Array.from(document.images || []).filter(
                    (image) => !image.complete
                ).length;
                return JSON.stringify({
                    pageCount: pages.length,
                    heights,
                    pendingImages
                });
            }

            function finish(timedOut) {
                done({
                    timedOut,
                    sample: lastSample || sample()
                });
            }

            function tick() {
                lastSample = sample();
                const parsed = JSON.parse(lastSample);
                const isBusy = parsed.pendingImages > 0;

                if (!isBusy && lastSample === window.__scribdLastRenderSample) {
                    stableTicks += 1;
                } else {
                    stableTicks = 0;
                }

                window.__scribdLastRenderSample = lastSample;

                if (stableTicks >= 2) {
                    finish(false);
                    return;
                }

                if (performance.now() - start >= settleBudgetMs) {
                    finish(true);
                    return;
                }

                requestAnimationFrame(() => setTimeout(tick, 200));
            }

            const fontsReady = document.fonts && document.fonts.ready
                ? document.fonts.ready.catch(() => undefined)
                : Promise.resolve();

            fontsReady.finally(() => {
                requestAnimationFrame(() => setTimeout(tick, 200));
            });
            """,
            int(timeout_seconds * 1000),
        )
    except WebDriverException as error:
        log(f"Render settle check failed; continuing with best effort: {error}")
        return

    if result.get("timedOut"):
        log("Render settle reached its time budget; continuing with best effort.")
    else:
        log("Document render settled before export.")


def detect_document_paper_size(driver):
    """
    Infer a paper size from the first rendered Scribd page.

    Scribd pages often render as absolutely positioned HTML at a fixed CSS size.
    Using that page box as the print sheet size avoids splitting one Scribd page
    across multiple PDF pages.
    """
    paper_size = driver.execute_script(
        """
        const candidates = [
            '.outer_page',
            '.newpage',
            '.outer_page_container',
            "[class*='page']"
        ];

        for (const selector of candidates) {
            const element = document.querySelector(selector);
            if (!element) {
                continue;
            }

            const rect = element.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                return {
                    widthInches: rect.width / 96,
                    heightInches: rect.height / 96,
                    selector
                };
            }
        }

        return null;
        """
    )

    if not paper_size:
        return {
            "widthInches": DEFAULT_PAPER_WIDTH_INCHES,
            "heightInches": DEFAULT_PAPER_HEIGHT_INCHES,
            "selector": "default",
        }

    return {
        "widthInches": max(1.0, round(paper_size["widthInches"], 3)),
        "heightInches": max(1.0, round(paper_size["heightInches"], 3)),
        "selector": paper_size["selector"],
    }


def load_page_batch(driver, page_numbers, options):
    """Load one bounded batch of page DOM and image assets."""
    configure_command_timeout(
        driver,
        options.page_load_timeout_seconds + 10,
    )
    driver.set_script_timeout(options.page_load_timeout_seconds + 10)

    result = driver.execute_async_script(
        """
        const pageNumbers = arguments[0];
        const timeoutMs = arguments[1];
        const done = arguments[arguments.length - 1];
        const manager = window.docManager;

        if (!manager || !manager.pages) {
            done({supported: false});
            return;
        }

        const states = pageNumbers.map((pageNum) => ({
            pageNum,
            page: manager.pages[pageNum],
            error: null
        }));
        const startedAt = Date.now();

        for (const state of states) {
            if (!state.page) {
                state.error = 'page object missing';
                continue;
            }

            try {
                if (!state.page.innerPageElem && !state.page.loadHasStarted) {
                    state.page.load();
                }
            } catch (error) {
                state.error = String(error);
            }
        }

        const timer = setInterval(() => {
            let ready = 0;

            for (const state of states) {
                if (state.error) {
                    ready += 1;
                    continue;
                }

                const page = state.page;
                if (!page.innerPageElem) {
                    continue;
                }

                try {
                    page.display();
                    if (!page._imagesTurnedOn) {
                        page.turnOnImages();
                    }
                } catch (error) {
                    state.error = String(error);
                    ready += 1;
                    continue;
                }

                const images = Array.from(
                    page.innerPageElem.querySelectorAll('img')
                );
                const pending = images.filter((image) => !image.complete);

                if (pending.length === 0) {
                    ready += 1;
                }
            }

            if (ready === states.length) {
                clearInterval(timer);
                done({
                    supported: true,
                    failed: states
                        .filter((state) => state.error)
                        .map((state) => ({
                            pageNum: state.pageNum,
                            reason: state.error
                        }))
                });
                return;
            }

            if (Date.now() - startedAt >= timeoutMs) {
                clearInterval(timer);
                done({
                    supported: true,
                    failed: states
                        .filter((state) => (
                            state.error ||
                            !state.page ||
                            !state.page.innerPageElem ||
                            Array.from(
                                state.page.innerPageElem.querySelectorAll('img')
                            ).some((image) => !image.complete)
                        ))
                        .map((state) => ({
                            pageNum: state.pageNum,
                            reason: state.error || 'page or image load timed out'
                        }))
                });
            }
        }, 50);
        """,
        list(page_numbers),
        options.page_load_timeout_seconds * 1000,
    )

    if not result.get("supported"):
        raise ScribdDownloadError("Scribd direct page loader is unavailable.")

    if result["failed"]:
        details = ", ".join(
            f"{item['pageNum']} ({item['reason']})"
            for item in result["failed"]
        )
        raise ScribdDownloadError(f"Failed to load Scribd page(s): {details}")


def release_page_batch(driver, page_numbers):
    """Release printed page DOM and image resources from Chrome."""
    driver.execute_script(
        """
        const manager = window.docManager;
        if (!manager || !manager.pages) {
            return;
        }

        for (const pageNum of arguments[0]) {
            const page = manager.pages[pageNum];
            if (!page) {
                continue;
            }

            try {
                page.remove();
            } catch (error) {
                const container = document.getElementById(`outer_page_${pageNum}`);
                if (container) {
                    const inner = container.querySelector('.newpage');
                    if (inner) {
                        inner.remove();
                    }
                }
            }
        }
        """,
        list(page_numbers),
    )

    try:
        driver.execute_cdp_cmd("HeapProfiler.collectGarbage", {})
    except WebDriverException:
        pass


def wait_for_document_ready(driver, timeout_seconds, log=_noop_log):
    """
    Wait until the Scribd embed exposes its page manager.

    Replaces the fixed post-navigation sleep with a condition-based wait that
    returns as soon as the document pages are addressable.
    """
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        ready = driver.execute_script(
            "return !!(window.docManager && window.docManager.pages && "
            "Object.keys(window.docManager.pages).length > 0);"
        )

        if ready:
            return True

        time.sleep(0.1)

    log("Document readiness wait timed out; continuing.")
    return False


_ISOLATED_PAGE_SCRIPT = """
                const targetIndex = arguments[0];

                const pages = Array.from(
                    document.querySelectorAll(
                        '.outer_page'
                    )
                );

                const target = pages[targetIndex];

                if (!target) {
                    return null;
                }

                /*
                 * Remove previous isolated-print style.
                 */
                const oldStyle = document.getElementById(
                    'isolated-page-print-style'
                );

                if (oldStyle) {
                    oldStyle.remove();
                }

                /*
                 * Restore all pages before measuring.
                 */
                pages.forEach((page) => {
                    page.style.removeProperty('display');
                    page.style.removeProperty('visibility');
                    page.style.removeProperty('position');
                    page.style.removeProperty('top');
                    page.style.removeProperty('left');
                    page.style.removeProperty('right');
                    page.style.removeProperty('bottom');
                    page.style.removeProperty('margin');
                    page.style.removeProperty('break-after');
                    page.style.removeProperty('page-break-after');
                    page.style.removeProperty('break-before');
                    page.style.removeProperty('page-break-before');
                });

                const rect = target.getBoundingClientRect();

                const width = Math.ceil(rect.width);
                const height = Math.ceil(rect.height);

                /*
                 * Mark the target instead of relying on nth-child.
                 */
                pages.forEach((page) => {
                    page.removeAttribute(
                        'data-export-target'
                    );
                });

                target.setAttribute(
                    'data-export-target',
                    'true'
                );

                const style = document.createElement(
                    'style'
                );

                style.id = 'isolated-page-print-style';

                style.textContent = `
                    @page {
                        size: ${width}px ${height}px;
                        margin: 0;
                    }

                    @media print {
                        html,
                        body {
                            width: ${width}px !important;
                            height: ${height}px !important;
                            min-width: ${width}px !important;
                            min-height: ${height}px !important;
                            max-width: ${width}px !important;
                            max-height: ${height}px !important;

                            margin: 0 !important;
                            padding: 0 !important;

                            overflow: hidden !important;

                            -webkit-print-color-adjust:
                                exact !important;

                            print-color-adjust:
                                exact !important;
                        }

                        .outer_page {
                            display: none !important;
                        }

                        .outer_page[
                            data-export-target="true"
                        ] {
                            display: block !important;
                            visibility: visible !important;

                            position: absolute !important;

                            top: 0 !important;
                            left: 0 !important;
                            right: auto !important;
                            bottom: auto !important;

                            width: ${width}px !important;
                            height: ${height}px !important;

                            min-width: 0 !important;
                            min-height: 0 !important;

                            max-width: none !important;
                            max-height: none !important;

                            margin: 0 !important;
                            padding: 0 !important;

                            transform: none !important;

                            break-before: auto !important;
                            break-after: auto !important;
                            break-inside: auto !important;

                            page-break-before:
                                auto !important;

                            page-break-after:
                                auto !important;

                            page-break-inside:
                                auto !important;

                            overflow: hidden !important;
                        }
                    }
                `;

                document.head.appendChild(style);

                return {
                    width,
                    height
                };
"""


_PAGE_COUNT_SCRIPT = """
return document.querySelectorAll(
    '.outer_page'
).length;
"""


def export_pages(
    driver,
    spool_dir,
    options,
    log,
    start_index,
    end_index,
    page_count,
):
    """Export pages [start_index, end_index) as one PDF sheet each.

    Returns the list of spooled one-page PDF file paths, in page order.
    """
    from pypdf import PdfReader

    configure_command_timeout(
        driver,
        options.cdp_timeout_seconds,
    )

    page_files = []

    for index in range(start_index, end_index):
        if (index - start_index) % options.export_batch_size == 0:
            batch_end = min(
                end_index,
                index + options.export_batch_size,
            )
            batch_page_numbers = list(
                range(index + 1, batch_end + 1)
            )
            log(
                f"  Loading page batch "
                f"{index + 1}-{batch_end}/{page_count}..."
            )
            load_page_batch(
                driver,
                batch_page_numbers,
                options,
            )

        page_info = driver.execute_script(
            _ISOLATED_PAGE_SCRIPT,
            index,
        )

        if not page_info:
            log(
                f"  Skipping page "
                f"{index + 1}: element missing"
            )
            continue

        width_px = int(page_info["width"])
        height_px = int(page_info["height"])

        if width_px <= 0 or height_px <= 0:
            log(
                f"  Skipping page {index + 1}: "
                f"invalid geometry "
                f"{width_px}x{height_px}"
            )
            continue

        width_inches = width_px / 96.0
        height_inches = height_px / 96.0

        log(
            f"  Page {index + 1}/{page_count} "
            f"{width_px}x{height_px}px "
            f"-> "
            f'{width_inches:.3f}"'
            f'x{height_inches:.3f}"'
        )

        driver.execute_cdp_cmd(
            "Emulation.setEmulatedMedia",
            {
                "media": "print",
            },
        )

        print_params = {
            "landscape": False,
            "displayHeaderFooter": False,
            "printBackground": True,

            "scale": 1,

            "paperWidth": width_inches,
            "paperHeight": height_inches,

            "marginTop": 0,
            "marginBottom": 0,
            "marginLeft": 0,
            "marginRight": 0,

            "preferCSSPageSize": True,

            "pageRanges": "1",
        }

        page_path = os.path.join(
            spool_dir,
            f"page-{index + 1:08d}.pdf",
        )

        print_params["transferMode"] = "ReturnAsBase64"
        result = driver.execute_cdp_cmd(
            "Page.printToPDF",
            print_params,
        )

        pdf_bytes = base64.b64decode(
            result["data"]
        )

        page_reader = PdfReader(BytesIO(pdf_bytes))

        with open(page_path, "wb") as page_handle:
            page_handle.write(pdf_bytes)

        if len(page_reader.pages) != 1:
            raise ScribdDownloadError(
                f"Document page "
                f"{index + 1} produced "
                f"{len(page_reader.pages)} "
                "PDF sheets; expected exactly 1."
            )

        page_files.append(page_path)

        log(
            f"    OK: exactly 1 PDF sheet"
        )

        is_batch_end = (
            (index + 1 - start_index) % options.export_batch_size == 0
            or index + 1 == end_index
        )
        if is_batch_end:
            release_page_batch(
                driver,
                batch_page_numbers,
            )

    return page_files


def _merge_page_pdfs(page_files, filename, log=_noop_log):
    """Combine spooled one-page PDFs into the final document."""
    from pypdf import PdfWriter

    log(
        f"Merging {len(page_files)} "
        "disk-spooled PDF pages..."
    )
    writer = PdfWriter()
    try:
        for page_path in page_files:
            writer.append(page_path)
        with open(filename, "wb") as output_handle:
            writer.write(output_handle)
    finally:
        writer.close()


def save_pdf_pages_individually(
    driver,
    filename,
    options,
    log=_noop_log,
):
    """Export each document page as one PDF sheet and merge them with pypdf."""
    page_count = driver.execute_script(_PAGE_COUNT_SCRIPT)

    if page_count <= 0:
        raise ScribdDownloadError(
            "No .outer_page elements found."
        )

    log(
        f"Exporting {page_count} "
        "document pages in bounded batches "
        f"of {options.export_batch_size}..."
    )

    spool = tempfile.TemporaryDirectory(
        prefix="scribd-pdf-pages-"
    )

    try:
        page_files = export_pages(
            driver,
            spool.name,
            options,
            log,
            0,
            page_count,
            page_count,
        )

        if not page_files:
            raise ScribdDownloadError(
                "No valid document pages "
                "were exported."
            )

        _merge_page_pdfs(page_files, filename, log)
    finally:
        spool.cleanup()

    return os.path.abspath(filename)


def _make_shard_log(shard_number, shard_count, base_log, lock):
    """Wrap a log callable so shard output is prefixed and line-atomic."""
    prefix = f"[shard {shard_number}/{shard_count}] "

    def shard_log(message):
        with lock:
            base_log(prefix + str(message))

    return shard_log


def _split_page_ranges(page_count, shard_count):
    """Split [0, page_count) into shard_count contiguous half-open ranges."""
    base, remainder = divmod(page_count, shard_count)
    ranges = []
    start = 0

    for shard_index in range(shard_count):
        size = base + (1 if shard_index < remainder else 0)
        ranges.append((start, start + size))
        start += size

    return ranges


def _run_export_shard(
    shard_index,
    page_range,
    shard_count,
    embed_url,
    page_count,
    main_driver,
    spool_dir,
    options,
    base_log,
    log_lock,
):
    """Export one shard's pages with its own Chrome (shard 0 reuses the main one)."""
    start_index, end_index = page_range
    shard_log = _make_shard_log(
        shard_index + 1,
        shard_count,
        base_log,
        log_lock,
    )

    own_driver = shard_index != 0
    profile_tempdir = None
    driver = main_driver

    try:
        if own_driver:
            profile_tempdir = tempfile.TemporaryDirectory(
                prefix=f"scribd-chrome-profile-shard{shard_index + 1}-"
            )

            shard_log("Starting Chrome browser...")
            driver = webdriver.Chrome(
                options=build_chrome_options(profile_tempdir.name, options)
            )

            driver.get(embed_url)

            wait_for_document_ready(driver, 15, shard_log)

            hide_cookie_dialogs(driver)
            shard_log("Cookie dialogs hidden.")

        observed_page_count = driver.execute_script(_PAGE_COUNT_SCRIPT)

        if observed_page_count != page_count:
            raise ScribdDownloadError(
                f"Shard {shard_index + 1} detected "
                f"{observed_page_count} pages; "
                f"expected {page_count}."
            )

        prepare_document_for_print(driver, shard_log)
        inject_print_styles(driver, shard_log)
        driver.execute_script("window.scrollTo(0, 0)")
        shard_log(
            f"Exporting pages {start_index + 1}-{end_index}..."
        )

        return export_pages(
            driver,
            spool_dir,
            options,
            shard_log,
            start_index,
            end_index,
            page_count,
        )
    finally:
        if own_driver and driver is not None:
            driver.quit()
            shard_log("Browser closed.")

        if profile_tempdir is not None:
            profile_tempdir.cleanup()


def save_pdf_pages_sharded(
    driver,
    filename,
    options,
    log=_noop_log,
):
    """Export pages across several parallel Chrome instances, then merge them."""
    page_count = driver.execute_script(_PAGE_COUNT_SCRIPT)

    if page_count <= 0:
        raise ScribdDownloadError(
            "No .outer_page elements found."
        )

    shard_count = max(1, min(options.parallel_shards, page_count))
    shard_ranges = _split_page_ranges(page_count, shard_count)

    log(
        f"Exporting {page_count} "
        "document pages across "
        f"{shard_count} parallel browser shards..."
    )

    spool = tempfile.TemporaryDirectory(
        prefix="scribd-pdf-pages-shared-"
    )
    log_lock = threading.Lock()

    try:
        with ThreadPoolExecutor(max_workers=shard_count) as pool:
            futures = [
                pool.submit(
                    _run_export_shard,
                    shard_index,
                    shard_ranges[shard_index],
                    shard_count,
                    driver.current_url,
                    page_count,
                    driver,
                    spool.name,
                    options,
                    log,
                    log_lock,
                )
                for shard_index in range(shard_count)
            ]

            page_files = []
            for future in futures:
                page_files.extend(future.result())

        page_files.sort()

        if not page_files:
            raise ScribdDownloadError(
                "No valid document pages "
                "were exported."
            )

        _merge_page_pdfs(page_files, filename, log)
    finally:
        spool.cleanup()

    return os.path.abspath(filename)


def download(
    url,
    output=None,
    *,
    options=None,
    log=print,
    **overrides,
):
    """
    Download a Scribd document as a PDF and return the saved path.

    Args:
        url: Scribd document URL, e.g.
            https://www.scribd.com/document/123456789/Document-Title
            (legacy https://www.scribd.com/doc/... links also work).
        output: Destination path for the PDF. Defaults to a filename derived
            from the URL, saved in the current working directory.
        options: Fully specified :class:`DownloadOptions`. When omitted,
            options are read from the ``SCRIBD_*`` environment variables.
        log: Callable receiving progress messages (defaults to ``print``).
            Pass a logger such as ``logger.info``, or ``None`` for silence.
        **overrides: Shorthand to override individual :class:`DownloadOptions`
            fields, e.g. ``headless=False`` or ``export_batch_size=4``.

    Returns:
        Path to the saved PDF.

    Raises:
        InvalidScribdUrlError: If ``url`` is not a valid Scribd document URL.
        ScribdDownloadError: If the document cannot be loaded or exported.
    """
    if options is None:
        options = DownloadOptions.from_env()
    if overrides:
        options = replace(options, **overrides)

    if log is None:
        log = _noop_log

    embed_url = convert_scribd_link(url)
    pdf_filename = (
        str(output) if output is not None else get_filename_from_url(url)
    )

    log(f"Link embed: {embed_url}")
    log(f"Output filename: {pdf_filename}")

    with tempfile.TemporaryDirectory(
        prefix="scribd-chrome-profile-"
    ) as runtime_profile_dir:
        driver = None

        try:
            log("\nStarting Chrome browser...")

            driver = webdriver.Chrome(
                options=build_chrome_options(
                    runtime_profile_dir,
                    options,
                )
            )

            driver.get(embed_url)

            wait_for_document_ready(driver, 15, log)

            hide_cookie_dialogs(driver)
            log("Cookie dialogs hidden.")

            total_pages = driver.execute_script(
                """
                return document.querySelectorAll('.outer_page').length;
                """
            )

            if total_pages == 0:
                raise ScribdDownloadError(
                    "No printable document pages "
                    "were detected."
                )

            prepare_document_for_print(driver, log)

            inject_print_styles(driver, log)

            log(
                f"\nSaving PDF as: {pdf_filename}"
            )

            log(
                "  Export mode: "
                "Individual document pages"
            )

            log("  Margins: None")

            log(
                "  Headers/Footers: Disabled"
            )

            log(
                "  ChromeDriver command timeout: "
                f"{options.cdp_timeout_seconds}s"
            )

            driver.execute_script(
                "window.scrollTo(0, 0)"
            )

            if options.parallel_shards > 1:
                saved_path = save_pdf_pages_sharded(
                    driver,
                    pdf_filename,
                    options,
                    log,
                )
            else:
                saved_path = save_pdf_pages_individually(
                    driver,
                    pdf_filename,
                    options,
                    log,
                )

            if not saved_path:
                raise ScribdDownloadError(
                    "PDF export failed."
                )

            log(
                "PDF saved successfully to: "
                f"{saved_path}"
            )

            return Path(saved_path)

        except (
            RuntimeError,
            WebDriverException,
        ) as error:
            log(
                f"Export failed: {error}"
            )

            raise ScribdDownloadError(str(error)) from error

        finally:
            if driver is not None:
                driver.quit()
                log("Browser closed.")
