# TraceVoyage Helpers

This directory contains helper scripts for importing content into TraceVoyage from various sources.

## FindPenguins Scraper

The `findpenguins_scraper.py` script scrapes a FindPenguins trip and converts it to TraceVoyage format.

### Installation

Install the required dependencies:

```bash
pip install requests beautifulsoup4
```

Or install with the scraper optional dependencies (includes Playwright for dynamic loading):

```bash
pip install -e ".[scraper]"
playwright install chromium  # Only needed for Playwright browser support
```

### Usage

```bash
python helpers/findpenguins_scraper.py https://findpenguins.com/karoc/trip/winter-is-coming
```

This will create a folder structure at:

```
imported/trips/winter-is-coming/
├── description.md          # Trip metadata, source info, and description
├── title.*                 # Optional trip title image from source (if present)
├── import_summary.json     # Summary of the import process
└── entries/
  ├── 2025-11-10-entry-title/
  │   ├── text.md         # Entry content with TOML front matter and Markdown body
  │   ├── media.json      # Per-file media metadata (e.g. image descriptions)
    │   └── media/
  │       ├── original-photo-name.jpg
  │       └── another-photo-name.jpg
  └── 2025-11-13-another-entry/
        └── text.md
```

### Options

- `--output DIR`: Specify the base output directory (default: `imported`)
- `--verbose`, `-v`: Enable verbose logging for debugging
- `--no-browser`: Skip Playwright browser automation and use requests only (may miss dynamically loaded content)

#### Authentication Options (for private trips)

- `--cookies FILE`: Path to a cookie file (JSON or Netscape format) exported from your browser
- `--interactive-login`: Opens a Playwright browser window where you can log in manually
- `--login-email EMAIL`: Email address for programmatic login (requires `--login-password`)
- `--login-password PASSWORD`: Password for programmatic login
- `--login-env`: Read credentials from `FINDPENGUINS_EMAIL` and `FINDPENGUINS_PASSWORD` environment variables
- `--save-cookies FILE`: Save cookies to a file after login for later reuse with `--cookies`

### Examples

```bash
# Basic usage
python helpers/findpenguins_scraper.py https://findpenguins.com/username/trip/my-trip

# Output to a different base directory
python helpers/findpenguins_scraper.py https://findpenguins.com/username/trip/my-trip --output data

# Verbose output for debugging
python helpers/findpenguins_scraper.py https://findpenguins.com/username/trip/my-trip -v

### Authentication Examples

```bash
# 1. Cookie file (exported from browser via "EditThisCookie" / "Get cookies.txt LOCALLY")
python helpers/findpenguins_scraper.py https://findpenguins.com/username/trip/private-trip --cookies cookies.json

# 2. Interactive login (opens browser window, log in manually)
python helpers/findpenguins_scraper.py https://findpenguins.com/username/trip/private-trip --interactive-login

# 3. Login with email and password (programmatic)
python helpers/findpenguins_scraper.py https://findpenguins.com/username/trip/private-trip --login-email user@example.com --login-password "yourpassword"

# 4. Login via environment variables (safer - no password in history)
export FINDPENGUINS_EMAIL="user@example.com"
export FINDPENGUINS_PASSWORD="yourpassword"
python helpers/findpenguins_scraper.py https://findpenguins.com/username/trip/private-trip --login-env

# 5. Interactive login + save cookies for future reuse (login once, reuse many times)
python helpers/findpenguins_scraper.py https://findpenguins.com/username/trip/private-trip --interactive-login --save-cookies cookies.json

# Later runs (no login needed):
python helpers/findpenguins_scraper.py https://findpenguins.com/username/trip/other-private-trip --cookies cookies.json
```

### What Gets Scraped

The scraper attempts to extract:

- **Trip level:**
  - Title
  - Description
  - Title image (`title.*`) when available on the source page
  - Source metadata (`source`, `source_url`)

- **Entry level:**
  - Date
  - Title
  - Content converted to Markdown body
  - Location/country information
  - GPS coordinates (if available)
  - Weather and temperature (if available)
  - Images and photos
  - Image descriptions/captions (when available) written to `media.json`

**Note:** The scraper uses Playwright (headless browser) to handle FindPenguins' dynamic "Load more" pagination. If Playwright is not installed, it falls back to a single-pass requests-based fetch, which may miss some articles on trips with pagination.

### Integration with TraceVoyage

The generated structure is already compatible with TraceVoyage's expected `data/trips/...` layout.

You can build directly from the imported directory:

```bash
python -m tracevoyage_gen build --data imported --assets assets
```

If you want to merge the imported trip into an existing `data` directory, move or copy the trip folder under `data/trips/`:

```bash
# PowerShell
Move-Item imported/trips/winter-is-coming data/trips/

# bash
mv imported/trips/winter-is-coming data/trips/

# Build from your main data directory
python -m tracevoyage_gen build --data data --assets assets
```

### Limitations

- The scraper works best with publicly accessible FindPenguins trips
- Private trips require authentication (use `--cookies`, `--interactive-login`, or `--login-email` options)
- The FindPenguins login page structure may change - if login fails, the selectors in `_playwright_login()` may need updating
- Image quality depends on what's available in the source
- Some metadata may not be available depending on the trip's privacy settings

### Troubleshooting

If the scraper fails or produces incomplete results:

1. Check that the URL is correct and the trip is publicly accessible
2. Run with `--verbose` flag to see detailed logging
3. Check the `import_summary.json` file for information about what was scraped
4. The website structure may have changed - you may need to adjust the scraper

### Notes

- The scraper is respectful and includes appropriate delays between requests
- Images are downloaded to preserve the complete trip
- Entry files include a Markdown heading, which TraceVoyage uses as the entry title
- The front matter preserves extracted metadata such as `title`, `country`, `weather`, and `temperature_c` when available
- TraceVoyage reads `media.json` and renders image descriptions in the gallery (caption + lightbox text)
- The source URL is recorded in the trip description for reference
