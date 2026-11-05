<p align="center">
  <img src="assets/scribd.svg" alt="Scribd" width="200">
</p>

<h1 align="center">Scribd Downloader</h1>

<p align="center">
  <b>Download Scribd documents as PDF for free - Fast, automated, and runs in background!</b>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/">
    <img src="https://img.shields.io/badge/Python-3.10+-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10+">
  </a>
  <a href="https://pypi.org/project/selenium/">
    <img src="https://img.shields.io/badge/Selenium-4.0+-green?style=for-the-badge&logo=selenium&logoColor=white" alt="Selenium 4.0+">
  </a>
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-MIT-orange?style=for-the-badge" alt="MIT License">
  </a>
</p>

<p align="center">
  <a href="https://buymeacoffee.com/mrsami">
    <img src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black" alt="Buy Me A Coffee">
  </a>
  <a href="https://github.com/sponsors/fullstackusama">
    <img src="https://img.shields.io/badge/Sponsor-ea4aaa?style=for-the-badge&logo=github-sponsors&logoColor=white" alt="GitHub Sponsors">
  </a>
  <a href="https://github.com/fullstackusama/scribd-downloader/stargazers">
    <img src="https://img.shields.io/github/stars/fullstackusama/scribd-downloader?style=for-the-badge&logo=github" alt="GitHub Stars">
  </a>
</p>

---

> **Credits:** This project is an enhanced fork of [themrsami/scribd-downloader](https://github.com/themrsami/scribd-downloader), originally created by [Usama Nazir](https://github.com/fullstackusama). This fork adds uv-based packaging and an importable Python API on top of the original download engine.

## Features

- **One-click download** - Just paste the Scribd URL and get your PDF
- **uv-ready** - Install, run, and manage with [uv](https://docs.astral.sh/uv/): `uv sync`, `uv run`, or `uvx`
- **Importable API** - Use it programmatically: `from scribd_downloader import download`
- **Supports both Scribd URL styles** - Works with `/document/...` and legacy `/doc/...` links
- **Runs in background** - Headless Chrome, no browser window pops up
- **No scrolling required** - Loads Scribd page data directly instead of simulating page-by-page scrolling
- **Clean PDFs** - No cookie banners, toolbars, or watermarks
- **Bounded-memory export** - Keeps only a configurable batch of fully loaded pages in Chrome at once
- **Disk-spooled merge** - Writes each rendered page to temporary storage before combining the final PDF with `pypdf`
- **Large document support** - Verified with image-heavy documents containing up to 2,552 pages
- **Better math rendering** - Preserves Scribd layout classes needed by equations and SVG content
- **Exact pagination** - Validates that every Scribd page produces exactly one PDF sheet
- **Dynamic page size** - Detects each rendered page's dimensions instead of forcing one fixed sheet size
- **Auto filename** - PDF named after the document URL automatically
- **No login required** - Works without Scribd account

---

## Requirements

- [uv](https://docs.astral.sh/uv/) - manages the Python version (3.10+), the virtual environment, and all dependencies automatically
- Google Chrome browser installed
- Chrome WebDriver (auto-managed by Selenium)

---

## Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/kunanonMWIT/scribd-downloader.git
   cd scribd-downloader
   ```

2. **Sync the environment** - `uv` provisions Python, creates an isolated `.venv`, and installs all dependencies (also generates `uv.lock` for reproducible installs):
   ```bash
   uv sync
   ```

No clone needed? Run it straight from GitHub:

```bash
uvx --from git+https://github.com/kunanonMWIT/scribd-downloader scribd-downloader
```

---

## Usage

### Command line

```bash
# Interactive - prompts for the URL
uv run scribd-downloader

# Or pass the URL directly
uv run scribd-downloader https://www.scribd.com/document/123456789/Document-Title

# Custom output path and a visible browser window
uv run scribd-downloader <url> -o my-document.pdf --visible

# Tune the export
uv run scribd-downloader <url> --batch-size 4 --cdp-timeout 900

# Equivalent module entry point
uv run python -m scribd_downloader
```

Useful CLI flags: `-o/--output` (PDF path), `--visible`, `--batch-size`, `--cdp-timeout`, `--page-load-timeout`, `--concurrency`, `-q/--quiet`, `--version`.

Both Scribd URL styles work: `/document/...` and legacy `/doc/...`.

While running, the downloader opens the document in headless Chrome, loads document pages directly in bounded batches, releases each batch from Chrome after printing to control memory use, removes unwanted elements (toolbars, cookie banners), spools individual pages to temporary storage, and merges the final PDF. Your PDF is saved with the document name from the URL.

### As a Python library

The downloader is importable as a plain function:

```python
from scribd_downloader import download

# Saves "<Document-Title>.pdf" in the current directory and returns a pathlib.Path
pdf_path = download("https://www.scribd.com/document/123456789/Document-Title")

# Choose your own output path
pdf_path = download(url, output="reports/lecture-notes.pdf")

# Tune the browser and export behavior
pdf_path = download(
    url,
    output="manual.pdf",
    headless=False,           # watch Chrome work
    export_batch_size=4,      # fewer pages in memory at once
    cdp_timeout_seconds=900,  # more patient PDF printing
    log=None,                 # silence progress messages
)
```

Failures raise catchable exceptions:

```python
from scribd_downloader import download, ScribdDownloadError

try:
    download("https://www.scribd.com/document/123456789/Document-Title")
except ScribdDownloadError as error:
    print("Download failed:", error)
```

`InvalidScribdUrlError` (a subclass) is raised for malformed URLs. Progress messages go to any callable via `log=logger.info`, or are silenced with `log=None`. The `SCRIBD_*` environment variables documented below still apply whenever no explicit options are passed.

Any script in this project can simply `import scribd_downloader` once the environment is synced (`uv sync`), then run with `uv run python your_script.py`. From anywhere else, point uv at the project: `uv run --with git+https://github.com/kunanonMWIT/scribd-downloader python your_script.py`.

---

## Example Output

```text
$ uv run scribd-downloader
Input link Scribd: https://www.scribd.com/document/903361807/WorkdaySimpleIntegrations-EIB-31v2

Link embed: https://www.scribd.com/embeds/903361807/content
Output filename: WorkdaySimpleIntegrations-EIB-31v2.pdf

Starting Chrome browser...
Cookie dialogs hidden.
Top toolbar removed.
Bottom toolbar removed.
Adjusted 1 scroll containers for print.
Print CSS injected.

Saving PDF as: WorkdaySimpleIntegrations-EIB-31v2.pdf
  Export mode: Individual document pages
  Margins: None
  Headers/Footers: Disabled
  ChromeDriver command timeout: 600s
Exporting 316 document pages in bounded batches of 8...
  Loading page batch 1-8/316...
  Page 1/316 1002x1296px -> 10.438"x13.500"
    OK: exactly 1 PDF sheet
  ...
Merging 316 disk-spooled PDF pages...
PDF saved successfully to: C:\Users\...\WorkdaySimpleIntegrations-EIB-31v2.pdf
Browser closed.
```

---

## PDF Settings

| Setting | Value |
|---------|-------|
| Page Size | Detected dynamically from Scribd's rendered page |
| Margins | None (0) |
| Headers/Footers | Disabled |
| Background Graphics | Enabled |

---

## How It Works

1. **URL Conversion** - Converts Scribd document URL to embeddable format
2. **Headless Browser** - Opens Chrome in background (invisible)
3. **Batched Page Loading** - Loads a small group of Scribd pages and their images directly without scrolling
4. **Cleanup** - Removes toolbars, cookie banners, and overlays while preserving Scribd layout classes
5. **Per-page Export** - Detects each page's rendered size and prints exactly one PDF sheet through Chrome DevTools Protocol
6. **Memory Release** - Removes the completed batch from Chrome and requests garbage collection
7. **Disk-spooled Merge** - Combines the temporary one-page PDFs into the final document with `pypdf`
8. **Auto Close** - Browser closes automatically after saving

---

## Benchmarks

Reference results from one Windows machine are shown below. Performance varies with network speed, document complexity, CPU, RAM, Chrome version, and storage speed.

| Document | Pages | Total Time | Output Size | Blank Pages | Peak Combined RAM |
|----------|------:|-----------:|------------:|------------:|------------------:|
| CSS Solved Past Papers | 359 | 58.36 seconds | 228.49 MB | 0 | 1.47 GB |
| Manual de Servicio MX-305 | 2,552 | 14 minutes 12 seconds | 237.28 MB | 0 | 2.46 GB |

The batch size was `8` in both tests. Long documents still take time because Chrome must print every page individually, but fully loaded browser content is kept to one batch at a time.

---

## Troubleshooting

### "ChromeDriver not found" error
The script uses Selenium Manager to auto-download ChromeDriver. If you face issues:
```bash
uv lock --upgrade-package selenium
uv sync
```

### PDF not saving
- Ensure you have write permissions in the current directory
- Check if the Scribd URL is valid and accessible
- For very large documents, increase `SCRIBD_CDP_TIMEOUT` (default: `600`)

### Blank pages in PDF
- Some documents may have DRM protection
- Try increasing `SCRIBD_PAGE_LOAD_TIMEOUT` if page images load slowly
- If a document still renders incorrectly, try visible mode with `SCRIBD_HEADLESS=0`

### Very large documents
- Ensure the drive containing your temporary directory has enough free space for individual page PDFs
- Reduce `SCRIBD_EXPORT_BATCH_SIZE` if Chrome uses too much memory
- Increase `SCRIBD_PAGE_LOAD_TIMEOUT` when slow image assets time out
- Long documents still take time because each page is printed and validated separately

### Large, image-heavy, or math-heavy documents
You can tune the export with environment variables:

```powershell
$env:SCRIBD_CDP_TIMEOUT="900"
$env:SCRIBD_PAGE_LOAD_TIMEOUT="180"
$env:SCRIBD_EXPORT_BATCH_SIZE="4"
uv run scribd-downloader
```

Useful variables:

- `SCRIBD_CDP_TIMEOUT` - ChromeDriver command timeout in seconds for `Page.printToPDF`
- `SCRIBD_PAGE_LOAD_TIMEOUT` - Maximum direct page-loading time in seconds (default: `120`)
- `SCRIBD_EXPORT_BATCH_SIZE` - Maximum fully loaded pages kept in Chrome at once (default: `8`)
- `SCRIBD_HEADLESS=0` - Run with a visible browser when debugging rendering issues locally

---

## Contributing

Contributions are welcome! Feel free to:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## Support the Project

If you find this tool useful, consider supporting its development:

<p align="center">
  <a href="https://buymeacoffee.com/mrsami">
    <img src="assets/buymeacoffee.svg" alt="Buy Me A Coffee" width="40" height="40">
  </a>
</p>

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## Disclaimer

This tool is for educational purposes only. Please respect copyright laws and Scribd's Terms of Service. Only download documents you have the right to access.

---

<p align="center">
  Made with ❤️ by <a href="https://github.com/fullstackusama">Usama Nazir</a>
</p>

<p align="center">
  If you find this useful, please consider giving it a ⭐
</p>
