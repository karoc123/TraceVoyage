#!/usr/bin/env python3
"""FindPenguins trip scraper for TraceVoyage.

Scrapes a FindPenguins trip URL and creates an TraceVoyage-compatible
folder structure with entries for each post from the trip.

Usage:
    python helpers/findpenguins_scraper.py https://findpenguins.com/karoc/trip/winter-is-coming

This will create:
    imported/trips/winter-is-coming/
        description.md
        entries/
            <entry-id>/
                text.md
                media/
                    <images>.jpg
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup, Tag
except ImportError:
    print("ERROR: This script requires 'requests' and 'beautifulsoup4'.", file=sys.stderr)
    print("Install with: pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from http.cookiejar import MozillaCookieJar
    HAS_COOKIEJAR = True
except ImportError:
    HAS_COOKIEJAR = False

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Image URL suffix patterns on FindPenguins CDN
# _t_s = tiny square, _m_s = medium small, _l = large
_IMG_SIZE_RE = re.compile(r'_(?:t_s|m_s|l)\.')


def sanitize_filename(text: str) -> str:
    """Convert text to a safe filesystem name."""
    text = re.sub(r'[^\w\s-]', '', text.lower())
    text = re.sub(r'[-\s]+', '-', text)
    return text.strip('-')


def download_image(url: str, dest_path: Path, session: requests.Session) -> bool:
    """Download an image from URL to dest_path."""
    try:
        log.debug(f"Downloading image: {url}")
        response = session.get(url, timeout=30, stream=True)
        response.raise_for_status()

        with open(dest_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return True
    except Exception as e:
        log.warning(f"Failed to download {url}: {e}")
        return False


def _img_url_to_large(url: str) -> str:
    """Convert any FindPenguins image URL to the _l (large) variant."""
    return _IMG_SIZE_RE.sub('_l.', url)


def _suffix_from_url(url: str) -> str:
    """Best-effort suffix extraction from URL path."""
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix:
        return suffix
    return ".jpg"


# ──────────────────────────────────────────────
# Login & Cookie handling
# ──────────────────────────────────────────────


def _load_cookies_from_json(path: Path, session: requests.Session) -> None:
    """Load cookies from a JSON file (e.g. EditThisCookie / Get cookies.txt LOCALLY export).

    Supports two formats:
      1. List of dicts with keys: name, value, domain, path, httpOnly, secure, sameSite
      2. Single dict with 'cookies' key containing such a list
         (Netscape.txt export from "Get cookies.txt LOCALLY")
    """
    raw = json.loads(path.read_text(encoding='utf-8'))
    cookies_list = raw.get('cookies', raw) if isinstance(raw, dict) else raw

    if isinstance(cookies_list, dict):
        # Simple name → value dict format
        for name, value in cookies_list.items():
            session.cookies.set(name, value)
        return

    for c in cookies_list:
        if not isinstance(c, dict):
            continue
        name = c.get('name', '')
        value = c.get('value', '')
        if not name:
            continue
        kwargs = {
            'domain': c.get('domain', ''),
            'path': c.get('path', '/'),
        }
        # httpOnly is sometimes stored as string
        if c.get('httpOnly') in (True, 'true', 'True'):
            kwargs['rest'] = {'HttpOnly': None}
        session.cookies.set(name, value, **kwargs)
    log.info(f"Loaded {len(cookies_list)} cookies from {path}")


def _load_cookies_netscape(path: Path, session: requests.Session) -> None:
    """Load cookies in Netscape cookie file format (cookies.txt)."""
    if not HAS_COOKIEJAR:
        log.warning("http.cookiejar not available, can't load Netscape format")
        return
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    for c in jar:
        session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
    log.info(f"Loaded {len(jar)} cookies from {path}")


def load_cookies(path: Path, session: requests.Session) -> None:
    """Detect cookie file format and load into the requests session."""
    if not path.exists():
        log.warning(f"Cookie file not found: {path}")
        return
    content = path.read_text(encoding='utf-8', errors='replace').strip()
    if not content:
        return
    # Detect format by looking at first non-empty line
    first_line = content.split('\n')[0].strip()
    if first_line.startswith('#') or first_line.startswith('.'):
        # Netscape format (cookies.txt)
        _load_cookies_netscape(path, session)
    else:
        # Assume JSON
        _load_cookies_from_json(path, session)


def _save_cookies(session: requests.Session, path: Path) -> None:
    """Save cookies from the requests session to a JSON file for reuse."""
    cookies_list = []
    for c in session.cookies:
        cookies_list.append({
            'name': c.name,
            'value': c.value,
            'domain': c.domain,
            'path': c.path,
        })
    path.write_text(
        json.dumps({'cookies': cookies_list}, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )
    log.info(f"Saved {len(cookies_list)} cookies to {path}")


def _playwright_login(
    login_url: str,
    email: str | None = None,
    password: str | None = None,
    interactive: bool = False,
    cookie_path: Path | None = None,
) -> list[dict] | None:
    """Log in to FindPenguins via Playwright.

    Args:
        login_url: The login page URL.
        email: Email for programmatic login.
        password: Password for programmatic login.
        interactive: If True, open browser and let user log in manually.
        cookie_path: If provided, save cookies after login.

    Returns:
        List of cookie dicts compatible with requests session, or None on failure.
    """
    if not HAS_PLAYWRIGHT:
        log.error("Playwright is required for login. Install with: pip install playwright && playwright install chromium")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not interactive,
                slow_mo=50 if interactive else None,
            )
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            )
            page = context.new_page()
            page.goto(login_url, wait_until='networkidle', timeout=60000)

            # Close cookie popup if present
            try:
                cookie_accept = page.locator('a[onclick*="acceptCookies"], button:has-text("Accept"), button:has-text("Alle")')
                if cookie_accept.count() > 0:
                    log.debug("  Accepting cookies popup")
                    cookie_accept.first.click()
                    page.wait_for_timeout(500)
            except Exception:
                pass

            if interactive:
                log.info("=" * 60)
                log.info("INTERACTIVE LOGIN MODE")
                log.info("A browser window has opened. Please log in manually.")
                log.info("After logging in, press Enter in this terminal to continue...")
                log.info("=" * 60)
                input("Press Enter after you have logged in...")
            elif email and password:
                log.info("Attempting programmatic login...")
                # Try common FindPenguins login form selectors
                email_input = page.locator('input[type="email"], input[name="email"], input[name="login"], input[placeholder*="email" i], input[placeholder*="Email" i]')
                password_input = page.locator('input[type="password"], input[name="password"], input[placeholder*="password" i], input[placeholder*="Passwort" i]')

                if email_input.count() == 0 or password_input.count() == 0:
                    log.warning("Could not find login form fields on the page.")
                    log.warning("The login page structure may have changed.")
                    browser.close()
                    return None

                email_input.first.fill(email)
                password_input.first.fill(password)

                # Try various submit button selectors
                submit_btn = page.locator(
                    'button[type="submit"], '
                    'input[type="submit"], '
                    'button:has-text("Log in"), '
                    'button:has-text("Sign in"), '
                    'button:has-text("Anmelden"), '
                    'a:has-text("Log in"), '
                    'button:has-text("Login")'
                )
                if submit_btn.count() > 0:
                    submit_btn.first.click()
                else:
                    # Fallback: press Enter on password field
                    password_input.first.press('Enter')

                # Wait for navigation/redirect
                page.wait_for_timeout(5000)
                try:
                    page.wait_for_url(
                        lambda url: '/login' not in url and '/auth' not in url,
                        timeout=15000,
                    )
                except Exception:
                    log.warning("Login may have failed - still on login page after submission.")
                    # Check for error messages
                    error = page.locator('.error, .alert, .notification-error, [class*="error"]')
                    if error.count() > 0:
                        log.warning(f"Login error: {error.first.text_content()}")

            # Save cookies from Playwright context
            playwright_cookies = context.cookies()
            if not playwright_cookies:
                log.warning("No cookies found after login attempt.")
                browser.close()
                return None

            log.info(f"Got {len(playwright_cookies)} cookies from browser session")

            # Save to file if path provided
            if cookie_path:
                cookie_path.parent.mkdir(parents=True, exist_ok=True)
                cookie_path.write_text(
                    json.dumps({'cookies': playwright_cookies}, indent=2, ensure_ascii=False),
                    encoding='utf-8',
                )
                log.info(f"Cookies saved to {cookie_path}")

            # Convert Playwright cookies to requests-session compatible format
            result = []
            for c in playwright_cookies:
                cookie = {
                    'name': c['name'],
                    'value': c['value'],
                    'domain': c.get('domain', ''),
                    'path': c.get('path', '/'),
                }
                if c.get('httpOnly', False):
                    cookie['rest'] = {'HttpOnly': None}
                result.append(cookie)

            browser.close()
            return result

    except Exception as e:
        log.error(f"Playwright login failed: {e}", exc_info=True)
        return None


def _apply_cookies_to_playwright_context(
    context: 'Any',
    cookies_path: Path,
    target_domain: str,
) -> None:
    """Load cookies from a JSON file and apply them to a Playwright browser context."""
    raw = json.loads(cookies_path.read_text(encoding='utf-8'))
    cookies_list = raw.get('cookies', raw) if isinstance(raw, dict) else raw
    if isinstance(cookies_list, dict):
        # Simple name → value dict: skip (can't set domain)
        log.warning("Simple dict cookie format not supported for Playwright; use list format.")
        return

    pw_cookies = []
    for c in cookies_list:
        if not isinstance(c, dict) or not c.get('name'):
            continue
        cookie = {
            'name': c['name'],
            'value': c.get('value', ''),
            'domain': c.get('domain', target_domain),
            'path': c.get('path', '/'),
        }
        if c.get('httpOnly') in (True, 'true', 'True'):
            cookie['httpOnly'] = True
        if c.get('secure') in (True, 'true', 'True'):
            cookie['secure'] = True
        pw_cookies.append(cookie)

    if pw_cookies:
        context.add_cookies(pw_cookies)
        log.info(f"Applied {len(pw_cookies)} cookies to Playwright browser context")


def _apply_playwright_cookies_to_session(
    cookies: list[dict],
    session: requests.Session,
) -> None:
    """Apply a list of cookie dicts (from Playwright) to a requests Session."""
    for c in cookies:
        name = c.get('name', '')
        value = c.get('value', '')
        if not name:
            continue
        kwargs = {
            'domain': c.get('domain', ''),
            'path': c.get('path', '/'),
        }
        session.cookies.set(name, value, **kwargs)
    log.info(f"Applied {len(cookies)} cookies to requests session")


# ──────────────────────────────────────────────

def _parse_desc_span(desc_span: Tag) -> dict[str, Any]:
    """Parse the .desc span that contains date, country, and weather.

    Typical text: "Nov 10–11, 2025 in Germany ⋅ ⛅ 6 °C"
    The element also has a `content` attribute with ISO date like "2025-11-10".
    """
    info: dict[str, Any] = {}

    # Date from the content attribute (ISO format)
    iso_date = desc_span.get('content', '')
    if iso_date:
        try:
            info['date'] = datetime.strptime(iso_date[:10], '%Y-%m-%d').date()
        except ValueError:
            pass

    text = desc_span.get_text(' ', strip=True)

    # Country: "... in <Country> ⋅ ..."
    country_match = re.search(r'\bin\s+([A-Za-zÀ-ÿ\s-]+?)(?:\s*⋅|$)', text)
    if country_match:
        info['country'] = country_match.group(1).strip()

    # Weather emoji + temperature: "⋅ ⛅ 6 °C" or "⋅ ☁️ 9 °C"
    weather_match = re.search(r'⋅\s*(.+)', text)
    if weather_match:
        weather_raw = weather_match.group(1).strip()
        info['weather'] = weather_raw

        temp_match = re.search(r'(-?\d+)\s*°C', weather_raw)
        if temp_match:
            info['temperature_c'] = temp_match.group(1)

    return info


def _extract_entry_text(article: Tag) -> str:
    """Extract the body text from an article, combining truncated and hidden parts."""
    text_div = article.find('div', class_='text')
    if not text_div:
        return ""

    # Remove "Read more" links
    for a in text_div.find_all('a', class_='readMore'):
        a.decompose()

    # The truncated text ends right before <span class="dots"> and the
    # continuation lives in <span class="rest hide">.  Unwrap the rest
    # span so its text stays inline in the <p>.
    for rest_span in text_div.find_all('span', class_='rest'):
        rest_span.unwrap()

    for dots in text_div.find_all('span', class_='dots'):
        dots.decompose()

    paragraphs = []
    for p in text_div.find_all('p'):
        # Use separator to preserve word boundaries, then normalise whitespace
        t = p.get_text(' ')
        t = re.sub(r'\s+', ' ', t).strip()
        if t:
            paragraphs.append(t)

    return '\n\n'.join(paragraphs)


def _extract_photos(article: Tag, base_url: str) -> list[dict[str, str]]:
    """Extract photo URLs from the .images-container in an article.

    Returns list of dicts with 'url' (large version), 'filename', and
    optional 'description'.
    """
    photos: list[dict[str, str]] = []
    container = article.find('div', class_='images-container')
    if not container:
        return photos

    for a_tag in container.find_all('a', class_='image'):
        data_url = a_tag.get('data-url', '')
        data_filename = a_tag.get('data-filename', '')
        if not data_url:
            continue

        # Make absolute and ensure large variant
        abs_url = urljoin(base_url, data_url)
        abs_url = _img_url_to_large(abs_url)

        filename = data_filename or abs_url.split('/')[-1]

        # Try multiple possible caption sources used in FindPenguins markup.
        desc_candidates = [
            a_tag.get('data-description', ''),
            a_tag.get('data-caption', ''),
            a_tag.get('title', ''),
            a_tag.get('aria-label', ''),
        ]

        img_tag = a_tag.find('img')
        if img_tag:
            desc_candidates.extend([
                img_tag.get('alt', ''),
                img_tag.get('title', ''),
            ])

        caption = ""
        for candidate in desc_candidates:
            cleaned = re.sub(r'\s+', ' ', candidate).strip()
            if cleaned:
                caption = cleaned
                break

        photos.append({'url': abs_url, 'filename': filename, 'description': caption})

    return photos


def _fetch_page_with_playwright(
    url: str,
    cookie_file: Path | None = None,
) -> str | None:
    """Fetch a trip page using Playwright to handle dynamic 'Load more' buttons.

    Args:
        url: The trip URL to fetch.
        cookie_file: Optional path to a saved cookies JSON file to apply before
                     navigation (for authenticated pages).

    Returns the full page HTML (including <head>) after all dynamically loaded articles
    are fetched, or None if Playwright is not available. Note: Our Playwright approach
    fetches articles dynamically but meta tags are static, so we still rely on requests
    for the initial page load to get og:image etc.
    """
    if not HAS_PLAYWRIGHT:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            )

            # Apply saved cookies if provided (for authenticated pages)
            if cookie_file and cookie_file.exists():
                from urllib.parse import urlparse
                domain = urlparse(url).netloc
                _apply_cookies_to_playwright_context(context, cookie_file, domain)

            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=60000)

            # Close cookie popup if present (FindPenguins uses one)
            try:
                cookie_accept = page.locator('a[onclick*="acceptCookies"], button:has-text("Accept")')
                if cookie_accept.count() > 0:
                    log.debug("  Accepting cookies popup")
                    cookie_accept.first.click()
                    page.wait_for_timeout(500)
            except Exception as e:
                log.debug(f"  No cookie popup found: {e}")

            # Poll for "Load more" button and click it until articles stop loading
            max_iterations = 100  # Safety limit
            iteration = 0
            while iteration < max_iterations:
                try:
                    # Look for the "Load more" button (German: "Mehr" or English text)
                    load_more = page.locator(
                        'button:has-text("Load more"), button:has-text("Mehr"), a:has-text("Load more"), a.loadmoreBtn'
                    )

                    if load_more.count() > 0:
                        log.debug(f"  Clicking 'Load more' button (iteration {iteration + 1})")
                        # Scroll into view and click
                        load_more.first.scroll_into_view_if_needed()
                        load_more.first.click()
                        # Wait for new articles to render
                        page.wait_for_timeout(2000)
                        iteration += 1
                    else:
                        log.debug("  No more 'Load more' button found")
                        break
                except Exception as e:
                    log.debug(f"  No more buttons to click or error: {e}")
                    break

            # Get full HTML including head and body
            html = page.content()
            browser.close()
            return html

    except Exception as e:
        log.warning(f"Playwright loading failed, falling back to requests: {e}")
        return None


def _fetch_coordinates(
    footprint_url: str, session: requests.Session
) -> tuple[float | None, float | None]:
    """Fetch an individual footprint page and extract coordinates.

    Coordinates live in a script like:
        MapSingleFootprintController.initMap(51.815978,12.338218)
    """
    try:
        log.debug(f"Fetching coordinates from: {footprint_url}")
        resp = session.get(footprint_url, timeout=30)
        resp.raise_for_status()

        match = re.search(
            r'initMap\(\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*\)', resp.text
        )
        if match:
            return float(match.group(1)), float(match.group(2))
    except Exception as e:
        log.warning(f"Failed to fetch coordinates from {footprint_url}: {e}")

    return None, None


def scrape_findpenguins_trip(
    url: str,
    output_base: Path = Path("imported"),
    use_browser: bool = True,
    cookie_file: Path | None = None,
    login_email: str | None = None,
    login_password: str | None = None,
    interactive_login: bool = False,
    save_cookies: Path | None = None,
) -> None:
    """Scrape a FindPenguins trip and create TraceVoyage folder structure.

    Args:
        url: FindPenguins trip URL
        output_base: Base directory for output (default: 'imported')
        use_browser: Use Playwright browser for dynamic loading (default: True)
        cookie_file: Path to a cookie file (JSON or Netscape format) to load
        login_email: Email for programmatic login (requires Playwright)
        login_password: Password for programmatic login (requires Playwright)
        interactive_login: If True, open Playwright browser for manual login
        save_cookies: Path to save cookies after successful login for reuse
    """
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split('/') if p]

    if len(path_parts) < 3 or path_parts[-2] != 'trip':
        log.error("Invalid FindPenguins trip URL format. Expected: .../trip/trip-name")
        return

    trip_slug = path_parts[-1]

    trip_dir = output_base / "trips" / trip_slug
    entries_dir = trip_dir / "entries"
    entries_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Scraping trip: {url}")
    log.info(f"Output directory: {trip_dir}")

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })

    # ── Login / Cookie handling ──────────────────────────────
    pw_cookies: list[dict] | None = None

    if login_email or interactive_login:
        # Use Playwright for login
        login_url = f"{parsed.scheme}://{parsed.netloc}/login"
        log.info(f"Initiating login via {login_url}...")
        pw_cookies = _playwright_login(
            login_url=login_url,
            email=login_email,
            password=login_password,
            interactive=interactive_login,
            cookie_path=save_cookies or cookie_file,
        )
        if pw_cookies:
            _apply_playwright_cookies_to_session(pw_cookies, session)
        else:
            log.warning("Login failed or no cookies obtained. Continuing without authentication.")

    elif cookie_file and cookie_file.exists():
        # Load cookies from file into the requests session
        log.info(f"Loading cookies from {cookie_file}...")
        load_cookies(cookie_file, session)

    # ── Fetch the trip page ───────────────────────────────

    try:
        # Try Playwright first for dynamic loading, fall back to requests if unavailable
        html_content = None
        if use_browser:
            log.info("Fetching trip page with browser (for dynamic loading)...")
            html_content = _fetch_page_with_playwright(url, cookie_file=cookie_file)

        if html_content is None:
            log.info("Fetching trip page with requests...")
            response = session.get(url, timeout=30)
            response.raise_for_status()
            html_content = response.text

        soup = BeautifulSoup(html_content, 'html.parser')

        # --- Trip metadata ---
        trip_title = None
        h1 = soup.find('h1')
        if h1:
            trip_title = h1.get_text(strip=True)
        if not trip_title:
            trip_title = trip_slug.replace('-', ' ').title()

        trip_description = ""
        og_desc = soup.find('meta', attrs={'property': 'og:description'})
        if og_desc:
            trip_description = og_desc.get('content', '')

        title_image_name: str | None = None
        og_image = soup.find('meta', attrs={'property': 'og:image'})
        if og_image and og_image.get('content'):
            image_url = urljoin(url, og_image.get('content', ''))
            suffix = _suffix_from_url(image_url)
            title_image_name = f"title{suffix}"
            title_image_dest = trip_dir / title_image_name
            log.debug(f"Attempting to download title image from og:image: {image_url}")
            if not download_image(image_url, title_image_dest, session):
                log.warning(f"Failed to download title image from og:image")
                title_image_name = None
            else:
                log.info(f"Successfully downloaded title image: {title_image_name}")

        fm_lines = [
            "+++",
            f"title = '{trip_title}'",
        ]
        if title_image_name:
            fm_lines.append(f"title_image = '{title_image_name}'")
        fm_lines.extend(
            [
                "source = 'FindPenguins'",
                f"source_url = '{url}'",
                "+++",
                "",
                f"# {trip_title}",
                "",
                trip_description or "Imported from FindPenguins.",
                "",
            ]
        )
        description_content = "\n".join(fm_lines)
        (trip_dir / "description.md").write_text(description_content, encoding='utf-8')
        log.info(f"Created trip description: {trip_dir / 'description.md'}")

        # --- Find articles ---
        articles = soup.find_all('article')
        if not articles:
            log.error("Could not find any <article> elements. The page structure may have changed.")
            return

        log.info(f"Found {len(articles)} entries. Processing...")

        entries: list[dict[str, Any]] = []

        for idx, article in enumerate(articles, 1):
            try:
                # --- Title ---
                title_elem = article.find('h2', class_='headline')
                title = title_elem.get_text(strip=True) if title_elem else f"Entry {idx}"

                # --- Footprint URL (for coordinates) ---
                footprint_link = None
                if title_elem:
                    a_tag = title_elem.find('a', href=True)
                    if a_tag:
                        footprint_link = a_tag['href']
                        if not footprint_link.startswith('http'):
                            footprint_link = urljoin(url, footprint_link)

                # --- Date, country, weather from .desc span ---
                desc_span = article.find('span', class_='desc')
                meta = _parse_desc_span(desc_span) if desc_span else {}

                entry_date = meta.get('date', datetime.now().date())
                country = meta.get('country', '')
                weather = meta.get('weather', '')
                temperature_c = meta.get('temperature_c', '')

                # --- Entry ID and directory ---
                entry_id = f"{entry_date}-{sanitize_filename(title)}"
                entry_dir = entries_dir / entry_id
                entry_dir.mkdir(parents=True, exist_ok=True)

                log.info(f"[{idx}/{len(articles)}] {entry_id}")

                # --- Coordinates (requires fetching individual page) ---
                lat, lon = None, None
                if footprint_link:
                    lat, lon = _fetch_coordinates(footprint_link, session)
                    if lat is not None:
                        log.debug(f"  Coordinates: {lat}, {lon}")
                    time.sleep(0.3)  # polite delay

                # --- Text content ---
                content_text = _extract_entry_text(article)

                # --- Photos ---
                photos = _extract_photos(article, url)
                image_count = 0
                media_items: list[dict[str, str]] = []
                if photos:
                    media_dir = entry_dir / "media"
                    media_dir.mkdir(exist_ok=True)
                    for photo in photos:
                        dest = media_dir / photo['filename']
                        if download_image(photo['url'], dest, session):
                            image_count += 1
                            media_items.append({
                                'name': photo['filename'],
                                'description': photo.get('description', ''),
                            })

                if media_items:
                    media_json_path = entry_dir / "media.json"
                    media_json_path.write_text(
                        json.dumps({'media': media_items}, indent=2, ensure_ascii=False),
                        encoding='utf-8',
                    )

                # --- Write text.md ---
                lines = ["+++"]
                lines.append(f"date = {entry_date}")
                if country:
                    lines.append(f"country = '{country}'")
                if weather:
                    lines.append(f"weather = '{weather}'")
                if temperature_c:
                    lines.append(f"temperature_c = {temperature_c}")
                if lat is not None and lon is not None:
                    lines.append(f"lat = {lat}")
                    lines.append(f"lon = {lon}")
                lines.append(f"title = '{title}'")
                lines.append("+++")
                lines.append("")
                lines.append(f"# {title}")
                lines.append("")
                lines.append(content_text.strip())

                (entry_dir / "text.md").write_text(
                    '\n'.join(lines) + '\n', encoding='utf-8'
                )

                log.info(f"  {image_count} photos, country={country or '?'}, coords={'yes' if lat else 'no'}")

                entries.append({
                    'id': entry_id,
                    'title': title,
                    'date': str(entry_date),
                    'country': country,
                    'lat': lat,
                    'lon': lon,
                    'images': image_count,
                })

            except Exception as e:
                log.error(f"Failed to process entry {idx}: {e}", exc_info=True)
                continue

        # --- Summary ---
        summary = {
            'trip_title': trip_title,
            'trip_slug': trip_slug,
            'source_url': url,
            'scraped_at': datetime.now().isoformat(),
            'entries_count': len(entries),
            'entries': entries,
        }

        summary_path = trip_dir / "import_summary.json"
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')

        log.info(f"\n{'='*60}")
        log.info(f"Scraping completed!")
        log.info(f"Trip: {trip_title}")
        log.info(f"Entries: {len(entries)}")
        log.info(f"Output: {trip_dir}")
        log.info(f"{'='*60}\n")

    except requests.RequestException as e:
        log.error(f"Failed to fetch trip page: {e}")
        sys.exit(1)
    except Exception as e:
        log.error(f"An error occurred: {e}", exc_info=True)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Scrape a FindPenguins trip and convert to TraceVoyage format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Public trip (basic usage)
  python helpers/findpenguins_scraper.py https://findpenguins.com/karoc/trip/winter-is-coming

  # With cookie file (exported from browser)
  python helpers/findpenguins_scraper.py https://findpenguins.com/user/trip/private-trip --cookies cookies.json

  # Interactive login (opens browser for manual login)
  python helpers/findpenguins_scraper.py https://findpenguins.com/user/trip/private-trip --interactive-login

  # Programmatic login (email + password)
  python helpers/findpenguins_scraper.py https://findpenguins.com/user/trip/private-trip --login-email user@example.com --login-password mypass

  # Programmatic login with credentials from environment variables
  python helpers/findpenguins_scraper.py https://findpenguins.com/user/trip/private-trip --login-env

  # Save cookies after login for reuse
  python helpers/findpenguins_scraper.py https://findpenguins.com/user/trip/private-trip --interactive-login --save-cookies cookies.json
        """
    )
    parser.add_argument(
        'url',
        help='FindPenguins trip URL (e.g., https://findpenguins.com/karoc/trip/winter-is-coming)'
    )
    parser.add_argument(
        '--output',
        default='imported',
        help='Base output directory (default: imported)'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging'
    )
    parser.add_argument(
        '--no-browser',
        action='store_true',
        help='Skip Playwright browser and use requests only (misses dynamically loaded content)'
    )

    # ── Login / Cookie arguments ──
    login_group = parser.add_argument_group('Authentication (for private trips)')
    login_group.add_argument(
        '--cookies',
        metavar='FILE',
        help='Path to cookie file (JSON from browser export or Netscape format)'
    )
    login_group.add_argument(
        '--interactive-login',
        action='store_true',
        help='Open Playwright browser for manual interactive login'
    )
    login_group.add_argument(
        '--login-email',
        metavar='EMAIL',
        help='Email for programmatic login (requires --login-password)'
    )
    login_group.add_argument(
        '--login-password',
        metavar='PASSWORD',
        help='Password for programmatic login (or use FINDPENGUINS_PASSWORD env var)'
    )
    login_group.add_argument(
        '--login-env',
        action='store_true',
        help='Read credentials from FINDPENGUINS_EMAIL and FINDPENGUINS_PASSWORD environment variables'
    )
    login_group.add_argument(
        '--save-cookies',
        metavar='FILE',
        help='Save cookies to this file after successful login (for later reuse with --cookies)'
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    output_path = Path(args.output)
    use_browser = not args.no_browser

    if not HAS_PLAYWRIGHT and use_browser:
        log.warning(
            "Playwright not installed. Install it with: pip install playwright\n"
            "Then run: playwright install chromium\n"
            "Falling back to requests (may miss dynamically loaded articles).")

    # ── Resolve login parameters ──
    cookie_file = Path(args.cookies) if args.cookies else None
    login_email = args.login_email
    login_password = args.login_password
    interactive_login = args.interactive_login
    save_cookies_path = Path(args.save_cookies) if args.save_cookies else None

    if args.login_env:
        login_email = os.environ.get('FINDPENGUINS_EMAIL', login_email)
        login_password = os.environ.get('FINDPENGUINS_PASSWORD', login_password)

    if (login_email and not login_password) or (not login_email and login_password):
        parser.error("--login-email and --login-password must be used together")

    if interactive_login and not HAS_PLAYWRIGHT:
        log.error("Playwright is required for interactive login.")
        sys.exit(1)

    if (login_email or interactive_login) and not HAS_PLAYWRIGHT:
        log.warning("Playwright is required for login. Falling back to cookie-only mode.")

    scrape_findpenguins_trip(
        args.url,
        output_base=output_path,
        use_browser=use_browser,
        cookie_file=cookie_file,
        login_email=login_email,
        login_password=login_password,
        interactive_login=interactive_login,
        save_cookies=save_cookies_path,
    )


if __name__ == '__main__':
    main()
