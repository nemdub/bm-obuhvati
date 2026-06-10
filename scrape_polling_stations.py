#!/usr/bin/env python3
"""
Download all Serbian polling-station ("glasačka mesta") documents for the
January 2022 referendum from the Republic Electoral Commission (RIK) website.

Source page:
    https://www.rik.parlament.gov.rs/tekst/sr/12021/glasacka-mesta.php

The page lists ~180+ files (mostly .doc/.docx) organized alphabetically by
municipality, plus special-category documents (penal institutions, abroad,
military). Every file link follows the pattern /extfile/sr/<ID>/<filename>.

Usage:
    pip install requests beautifulsoup4
    python3 scrape_polling_stations.py
    python3 scrape_polling_stations.py --out my_folder --delay 0.5 --force

Files are saved into ./polling_stations_2022/ by default. Existing files are
skipped so the script can resume an interrupted run; pass --force to overwrite.
"""

import argparse
import os
import sys
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, quote, unquote

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit(
        "Missing dependencies. Install them with:\n"
        "    pip install requests beautifulsoup4"
    )

DEFAULT_URL = "https://www.rik.parlament.gov.rs/tekst/sr/12021/glasacka-mesta.php"
DEFAULT_OUT = "polling_stations_2022"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0  # seconds, multiplied by attempt number


def encode_url(url):
    """Percent-encode only the path of a URL so requests can fetch filenames
    that contain spaces or Cyrillic characters, leaving already-encoded parts
    intact."""
    parts = urlsplit(url)
    path = quote(parts.path, safe="/%")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def sanitize_filename(name):
    """Turn a URL path segment into a safe on-disk filename."""
    name = unquote(name)
    # Drop directory separators and strip odd leading characters seen in the
    # source (e.g. ". Ada.doc").
    name = name.replace("/", "_").replace("\\", "_").strip()
    name = name.lstrip(". ").strip()
    return name or "unnamed"


def find_document_links(session, page_url):
    """Fetch the index page and return a de-duplicated, ordered list of
    absolute /extfile/ document URLs."""
    resp = session.get(page_url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    seen = set()
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/extfile/" not in href:
            continue
        absolute = urljoin(page_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append(absolute)
    return links


def save_name_for(url, used_names):
    """Derive a unique on-disk filename for a document URL.

    URLs look like /extfile/sr/<ID>/<filename>. We use the filename, and on a
    collision we prefix the <ID> segment to keep both files.
    """
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    raw_name = segments[-1] if segments else "unnamed"
    name = sanitize_filename(raw_name)

    if name in used_names:
        # Find the numeric ID segment (the one before the filename) to
        # disambiguate.
        file_id = segments[-2] if len(segments) >= 2 else "dup"
        name = f"{file_id}_{name}"
        # Extremely unlikely, but guard against a second collision.
        counter = 2
        base = name
        while name in used_names:
            name = f"{counter}_{base}"
            counter += 1

    used_names.add(name)
    return name


def download(session, url, dest_path, delay):
    """Download a single file with retries. Returns True on success."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with session.get(encode_url(url), timeout=60, stream=True) as r:
                r.raise_for_status()
                tmp_path = dest_path + ".part"
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp_path, dest_path)
            if delay:
                time.sleep(delay)
            return True
        except Exception as exc:  # noqa: BLE001 - report and retry
            print(f"    attempt {attempt}/{MAX_RETRIES} failed: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Download Serbian 2022 polling-station documents from RIK."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Index page URL.")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output folder.")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="Seconds to wait between downloads (politeness).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download files that already exist on disk.",
    )
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    print(f"Fetching index page: {args.url}")
    try:
        links = find_document_links(session, args.url)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Failed to fetch or parse the index page: {exc}")

    print(f"Found {len(links)} document link(s).\n")
    if not links:
        sys.exit("No /extfile/ document links found — the page layout may have changed.")

    used_names = set()
    downloaded = skipped = failed = 0
    failures = []

    for i, url in enumerate(links, 1):
        name = save_name_for(url, used_names)
        dest = os.path.join(args.out, name)

        if os.path.exists(dest) and not args.force:
            print(f"[{i}/{len(links)}] skip (exists): {name}")
            skipped += 1
            continue

        print(f"[{i}/{len(links)}] downloading: {name}")
        if download(session, url, dest, args.delay):
            downloaded += 1
        else:
            failed += 1
            failures.append(url)
            print(f"    GIVING UP on {url}")

    print("\n=== Summary ===")
    print(f"  Found:      {len(links)}")
    print(f"  Downloaded: {downloaded}")
    print(f"  Skipped:    {skipped}")
    print(f"  Failed:     {failed}")
    print(f"  Output dir: {os.path.abspath(args.out)}")

    if failures:
        print("\nFailed URLs:")
        for url in failures:
            print(f"  - {url}")
        sys.exit(1)


if __name__ == "__main__":
    main()
