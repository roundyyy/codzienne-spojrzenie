#!/usr/bin/env python3
"""
Builds data/edition.js: a snapshot of the daily edition (news + article images).

Works like the app's update check:
    1. Today's edition already in the previous snapshot (the published site,
       or the local file)? -> nothing to do until tomorrow.
    2. Otherwise download the small .meta files (edition date, DDMMYYYY).
       Same dates as the previous snapshot -> nothing to do.
    3. New date -> download the news JSON, extract every article's main image
       once, and write a new snapshot (with the upload time, used by the
       website's "next news in..." countdown).

If a .meta date changed but its JSON content did not (the upload isn't finished
yet), that feed keeps its old date, so the next run tries again.

Image extraction follows the same priority as the app, using only the
Python standard library:
    og:image -> twitter:image -> JSON-LD -> <img> in <article>/<main> -> any large <img>

Usage:
    python scripts/build_edition.py                    # compare with local data/edition.js
    python scripts/build_edition.py --published URL    # compare with the live site (GitHub Actions)
    python scripts/build_edition.py --force            # rebuild even if the dates match
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

# Daily edition sources (same files as the app uses).
SOURCES = {
    "general": {
        "json": "https://dl.dropboxusercontent.com/scl/fi/k300wq34dxk3xux3z9uzi/DailyWorldPodcast_general.json?rlkey=pomtl8rb7qau3yd7qosqfpl3w",
        "meta": "https://dl.dropboxusercontent.com/scl/fi/gnao1evgfwyw2ov1sz5ea/DailyWorldPodcast_general.meta?rlkey=t2ywfi6aoqnwtjyt2q5ic33up",
    },
    "tech": {
        "json": "https://dl.dropboxusercontent.com/scl/fi/m16ciuuz5fox1b20o4muc/DailyWorldPodcast_tech.json?rlkey=ub1wye3ag1pkrdqq7f5qkl5u8",
        "meta": "https://dl.dropboxusercontent.com/scl/fi/ie9wj7ocj8g93mjknjhin/DailyWorldPodcast_tech.meta?rlkey=rkve10h1wzir9s0qdjttlkoeq",
    },
}

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data" / "edition.js"
FORMAT_VERSION = 1

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = 15
RETRIES = 3
MAX_BYTES = 2_000_000
WORKERS = 8
HARD_FAIL = (401, 403, 404, 410, 451)  # blocked or gone: retrying won't help


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def http_get(url: str, *, accept: str = "*/*", limit: int | None = None) -> tuple[str, str]:
    """Returns (final_url, text). Raises on network/HTTP errors."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.7,pl;q=0.5",
            "Referer": "https://www.google.com/",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read(limit) if limit else resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.geturl(), raw.decode(charset, errors="replace")


# --------------------------------------------------------------------------
# Image extraction (same priority as the app)
# --------------------------------------------------------------------------
CONTENT_CONTAINERS = {"article", "main"}
ICON_HINTS = ("icon", "logo", "favicon", "avatar", "sprite", "pixel", "spacer")


class ImageCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.jsonld: list[str] = []
        self.content_imgs: list[dict[str, str]] = []
        self.all_imgs: list[dict[str, str]] = []
        self._container_depth = 0
        self._in_jsonld = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").strip().lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()
        elif tag == "link" and a.get("rel", "").lower() == "image_src" and a.get("href"):
            self.meta.setdefault("image_src", a["href"].strip())
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_jsonld, self._buf = True, []
        elif tag in CONTENT_CONTAINERS or a.get("role", "").lower() == "main":
            self._container_depth += 1
        elif tag == "img":
            src = a.get("src") or a.get("data-src") or a.get("data-lazy-src") or ""
            if not src and a.get("srcset"):
                src = a["srcset"].split(",")[0].strip().split(" ")[0]
            if src and not src.startswith("data:"):
                a["_src"] = src.strip()
                self.all_imgs.append(a)
                if self._container_depth > 0:
                    self.content_imgs.append(a)

    def handle_endtag(self, tag):
        if tag == "script" and self._in_jsonld:
            self.jsonld.append("".join(self._buf))
            self._in_jsonld = False
        elif tag in CONTENT_CONTAINERS and self._container_depth > 0:
            self._container_depth -= 1

    def handle_data(self, data):
        if self._in_jsonld:
            self._buf.append(data)


def is_icon_or_logo(img: dict[str, str]) -> bool:
    haystack = " ".join((img["_src"], img.get("class", ""), img.get("alt", ""))).lower()
    return any(h in haystack for h in ICON_HINTS)


def is_small(img: dict[str, str]) -> bool:
    for dim in ("width", "height"):
        m = re.match(r"\d+", img.get(dim, ""))
        if m and int(m.group()) < 100:
            return True
    return False


def find_jsonld_image(node) -> str | None:
    if isinstance(node, str):
        return node if node.startswith(("http", "/")) else None
    if isinstance(node, list):
        for item in node:
            found = find_jsonld_image(item)
            if found:
                return found
        return None
    if isinstance(node, dict):
        if "image" in node:
            img = node["image"]
            if isinstance(img, dict):
                img = img.get("url") or img.get("contentUrl")
            found = find_jsonld_image(img)
            if found:
                return found
        if "thumbnailUrl" in node:
            found = find_jsonld_image(node["thumbnailUrl"])
            if found:
                return found
        for key in ("@graph", "mainEntity", "mainEntityOfPage"):
            if key in node:
                found = find_jsonld_image(node[key])
                if found:
                    return found
    return None


def extract_image(html: str, base_url: str) -> str | None:
    p = ImageCollector()
    try:
        p.feed(html)
    except Exception:
        pass  # malformed HTML: use whatever was collected

    candidates: list[str | None] = [
        p.meta.get("og:image:secure_url") or p.meta.get("og:image") or p.meta.get("og:image:url"),
        p.meta.get("twitter:image") or p.meta.get("twitter:image:src"),
    ]
    for block in p.jsonld:
        try:
            candidates.append(find_jsonld_image(json.loads(block.strip())))
        except (ValueError, RecursionError):
            m = re.search(r'"image"\s*:\s*"([^"]+)"', block)
            candidates.append(m.group(1) if m else None)
    candidates.append(p.meta.get("image_src"))
    candidates += [i["_src"] for i in p.content_imgs if not is_icon_or_logo(i)][:1]
    candidates += [i["_src"] for i in p.all_imgs if not is_icon_or_logo(i) and not is_small(i)][:1]

    for c in candidates:
        if c and c.strip():
            url = urljoin(base_url, c.strip().replace("\\/", "/"))
            if url.startswith("http://"):
                url = "https://" + url[len("http://"):]
            if url.startswith("https://"):
                return url
    return None


def image_for(article_url: str) -> str:
    """Returns image URL or "" when nothing could be found."""
    for attempt in range(1, RETRIES + 1):
        try:
            final_url, html = http_get(
                article_url,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                limit=MAX_BYTES,
            )
            return extract_image(html, final_url) or ""
        except urllib.error.HTTPError as e:
            if e.code in HARD_FAIL:
                return ""
        except Exception:
            pass
        if attempt < RETRIES:
            time.sleep(1.5 * attempt)
    return ""


def images_for_host(urls: list[str]) -> list[tuple[str, str]]:
    """One host at a time, sequentially, so sites don't rate-limit us."""
    out = []
    for i, url in enumerate(urls):
        if i:
            time.sleep(0.5)
        out.append((url, image_for(url)))
    return out


def fetch_images(urls: list[str]) -> dict[str, str]:
    by_host: dict[str, list[str]] = {}
    for u in urls:
        by_host.setdefault(urlparse(u).netloc.lower(), []).append(u)
    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for batch in pool.map(images_for_host, by_host.values()):
            results.update(batch)

    # Second, slower pass for transient failures (timeouts, rate limits).
    misses = [u for u in urls if not results.get(u)]
    if misses:
        time.sleep(3)
        for url in misses:
            results[url] = image_for(url)
    return results


# --------------------------------------------------------------------------
# Snapshot I/O
# --------------------------------------------------------------------------
def parse_snapshot(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    ok = (
        data.get("version") == FORMAT_VERSION
        and isinstance(data.get("feeds"), dict)
        and isinstance(data.get("dates"), dict)
        and isinstance(data.get("hashes"), dict)
    )
    return data if ok else None


def load_previous(published_url: str | None) -> tuple[dict | None, str]:
    if published_url:
        sep = "&" if "?" in published_url else "?"
        try:
            _, text = http_get(f"{published_url}{sep}t={int(time.time())}")
            return parse_snapshot(text), "published site"
        except Exception as e:
            print(f"Previous snapshot not available online ({e}); building from scratch.")
            return None, "none"
    if OUTPUT.exists():
        return parse_snapshot(OUTPUT.read_text(encoding="utf-8")), "local file"
    return None, "none"


def write_snapshot(snapshot: dict) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        "/* Generated by scripts/build_edition.py - do not edit by hand. */\n"
        "window.CC_EDITION = "
        + json.dumps(snapshot, ensure_ascii=False, indent=1)
        + ";\n",
        encoding="utf-8",
    )


def set_output(name: str, value: str) -> None:
    """Exposes a value to later GitHub Actions steps."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def today_ddmmyyyy() -> str:
    """Today's date in Ireland (DDMMYYYY), the same format as the .meta files."""
    try:
        from zoneinfo import ZoneInfo

        now = dt.datetime.now(ZoneInfo("Europe/Dublin"))
    except Exception:  # no time-zone data (e.g. plain Python on Windows)
        now = dt.datetime.now(dt.timezone.utc)
    return now.strftime("%d%m%Y")


def fetch_meta(feed: str) -> tuple[str, str]:
    """Returns (edition date DDMMYYYY, upload time ISO-UTC), or ("", "") on failure.

    The upload time comes from Dropbox's ETag, which starts with a millisecond
    timestamp - the same value the app uses for its "next news in..." countdown.
    """
    req = urllib.request.Request(
        SOURCES[feed]["meta"], headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            value = resp.read().decode("utf-8", "replace").strip()
            etag = resp.headers.get("Etag", "")
    except Exception as e:
        print(f"[{feed}] could not read .meta: {e}")
        return "", ""
    if not re.fullmatch(r"\d{8}", value):
        return "", ""
    uploaded = ""
    digits = re.sub(r"\D", "", etag)[:13]
    if len(digits) == 13:
        ts = dt.datetime.fromtimestamp(int(digits) / 1000, dt.timezone.utc)
        if 2020 <= ts.year <= 2100:
            uploaded = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value, uploaded


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--published", metavar="URL", help="URL of the live data/edition.js to compare with")
    ap.add_argument("--force", action="store_true", help="rebuild even if the edition dates match")
    args = ap.parse_args()

    prev, prev_source = load_previous(args.published)
    print(f"Previous snapshot ({prev_source}): {prev['dates'] if prev else 'none'}")

    def keep_previous(reason: str, changed: bool = False) -> int:
        print(reason)
        if prev_source == "published site" or changed:
            write_snapshot(prev)  # so a code-only deploy keeps the current edition
        set_output("changed", "true" if changed else "false")
        return 0

    # ---- 1a. Today's edition already published? Don't even ask Dropbox. ----
    today = today_ddmmyyyy()
    if (
        prev
        and not args.force
        and prev.get("uploaded")
        and all(prev["dates"].get(f) == today for f in SOURCES)
    ):
        return keep_previous(f"Today's edition ({today}) is already published - nothing to do until tomorrow.")

    metas: dict[str, str] = {}
    uploads: dict[str, str] = {}
    for feed in SOURCES:
        metas[feed], uploads[feed] = fetch_meta(feed)
    print(f"Dropbox dates: {metas}  uploaded: {uploads}")

    # ---- 1b. Nothing new on Dropbox? --------------------------------------
    if prev and not args.force:
        if all(metas.values()) and all(prev["dates"].get(f) == metas[f] for f in SOURCES):
            added = False
            if not prev.get("uploaded"):
                # Snapshot made by an older version of this script: add the
                # upload times so the website can show its countdown.
                prev["uploaded"] = uploads
                added = True
            return keep_previous("Edition is up to date - nothing to do.", changed=added)
        if not any(metas.values()):
            return keep_previous("Dropbox .meta unavailable - keeping the previous edition.")

    # ---- 2. Download news ------------------------------------------------
    # Every article URL already tried, including misses ("") - never scraped twice.
    old_images: dict[str, str] = {}
    if prev:
        for items in prev["feeds"].values():
            for it in items:
                if it.get("source_url"):
                    old_images[it["source_url"]] = it.get("image", "")

    prev_uploads = (prev or {}).get("uploaded") or {}

    def kept_upload(feed: str) -> str:
        """Upload time for a feed whose previous articles are kept."""
        if prev_uploads.get(feed):
            return prev_uploads[feed]
        same_edition = prev and prev["dates"].get(feed) == metas.get(feed)
        return uploads.get(feed, "") if same_edition else ""

    feeds: dict[str, list] = {}
    dates: dict[str, str] = {}
    hashes: dict[str, str] = {}
    uploaded: dict[str, str] = {}
    for feed, src in SOURCES.items():
        try:
            _, text = http_get(src["json"], accept="application/json")
            items = json.loads(text.lstrip("﻿"))
            if not isinstance(items, list):
                raise ValueError("JSON is not a list")
        except Exception as e:
            if prev and feed in prev["feeds"]:
                print(f"[{feed}] download failed ({e}) - keeping previous articles.")
                feeds[feed] = prev["feeds"][feed]
                dates[feed] = prev["dates"].get(feed, "")
                hashes[feed] = prev["hashes"].get(feed, "")
                uploaded[feed] = kept_upload(feed)
                continue
            print(f"[{feed}] ERROR: cannot download news JSON: {e}", file=sys.stderr)
            set_output("changed", "false")
            return 1

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if prev and not args.force and prev["hashes"].get(feed) == digest:
            # The date moved but the content didn't: upload probably not finished.
            # Keep the old date so the next run checks again.
            print(f"[{feed}] content unchanged - keeping date {prev['dates'].get(feed)}")
            feeds[feed] = prev["feeds"][feed]
            dates[feed] = prev["dates"].get(feed, "")
            hashes[feed] = digest
            uploaded[feed] = kept_upload(feed)
            continue

        feeds[feed] = [
            {
                "title": str(i.get("title", "")).strip(),
                "summary": str(i.get("summary", "")).strip(),
                "news_category": str(i.get("news_category", "")).strip(),
                "score": i.get("score", 0),
                "source_url": str(i.get("source_url", "")).strip(),
                "opinions": str(i.get("opinions", "")).strip(),
                "image": "",
            }
            for i in items
            if isinstance(i, dict) and str(i.get("title", "")).strip()
        ]
        dates[feed] = metas[feed]
        hashes[feed] = digest
        uploaded[feed] = uploads.get(feed, "")
        print(f"[{feed}] {len(feeds[feed])} articles, edition {metas[feed] or '?'}")

    # ---- 3. Images (once per article) -------------------------------------
    todo = []
    for items in feeds.values():
        for it in items:
            url = it.get("source_url", "")
            if url and not it.get("image"):
                if url in old_images:
                    it["image"] = old_images[url]
                elif url not in todo:
                    todo.append(url)

    if todo:
        print(f"Extracting images for {len(todo)} articles...")
        found = fetch_images(todo)
        for items in feeds.values():
            for it in items:
                if it.get("source_url") in found and not it.get("image"):
                    it["image"] = found[it["source_url"]]
        for url in todo:
            print(f"  {'OK  ' if found.get(url) else 'MISS'} {url[:90]}")

    total = sum(len(v) for v in feeds.values())
    with_img = sum(1 for v in feeds.values() for it in v if it.get("image"))
    print(f"Images: {with_img}/{total}")

    # ---- 4. Save ---------------------------------------------------------
    snapshot = {
        "version": FORMAT_VERSION,
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dates": dates,
        "uploaded": uploaded,
        "hashes": hashes,
        "feeds": feeds,
    }
    changed = not prev or any(
        prev.get(k) != snapshot[k] for k in ("dates", "uploaded", "hashes", "feeds")
    )
    if changed or prev_source != "local file":
        write_snapshot(snapshot if changed else prev)
    print("New edition saved to data/edition.js" if changed else "No changes.")
    set_output("changed", "true" if changed else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
