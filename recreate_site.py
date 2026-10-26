"""Restore a static site from a crawl manifest and its cached original HTML."""
from __future__ import annotations

import argparse
from collections import deque
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


def destination(root: Path, url: str, *, page: bool = False) -> Path:
    path = unquote(urlsplit(url).path)
    if "\\" in path or "\x00" in path or any(p in {".", ".."} for p in path.split("/")):
        raise ValueError(f"Unsafe URL path: {url}")
    if page and (path.endswith("/") or not Path(path).suffix):
        path = path.rstrip("/") + "/index.html"
    target = (root / path.lstrip("/")).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise ValueError(f"Path outside output directory: {url}")
    return target


class Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for name in ("src", "poster"):
            if values.get(name):
                self.urls.add(values[name])
        if tag == "link" and set(values.get("rel", "").lower().split()) & {"stylesheet", "icon", "preload"}:
            if values.get("href"):
                self.urls.add(values["href"])


CSS_URL = re.compile(r"url\(\s*['\"]?([^'\"\s)]+)['\"]?\s*\)|@import\s+['\"]([^'\"]+)['\"]", re.I)
ATTRIBUTE = re.compile(r"(\b(?:href|src|poster|action)\s*=\s*)(['\"])(.*?)\2", re.I | re.S)


def localize(content: str, base: str, origin: str) -> str:
    def replace(match: re.Match[str]) -> str:
        value = unescape(match[3])
        parts = urlsplit(urljoin(base, value))
        if value.startswith("#") or f"{parts.scheme}://{parts.netloc}" != origin:
            return match[0]
        local = urlunsplit(("", "", parts.path or "/", parts.query, parts.fragment))
        return match[1] + match[2] + local.replace("&", "&amp;") + match[2]
    return ATTRIBUTE.sub(replace, content)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("out/pages.jsonl"))
    parser.add_argument("--output-dir", type=Path, default=Path("out/site"))
    parser.add_argument("--fetch-assets", action="store_true", help="Download same-origin images, scripts, stylesheets and CSS dependencies")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between asset requests (default: 0.5)")
    args = parser.parse_args(argv)
    if args.delay < 0:
        parser.error("--delay must be nonnegative")
    try:
        records = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
        successful = [r for r in records if r.get("status") == "ok"]
        origins = {f"{urlsplit(r['url']).scheme}://{urlsplit(r['url']).netloc}" for r in successful}
        if len(origins) != 1:
            raise ValueError("Expected successful pages from exactly one origin")
        origin = origins.pop()
        root = args.output_dir.resolve()
        cache = args.input.resolve().parent
        planned = []
        targets: set[Path] = set()
        for record in successful:
            source = (cache / record["html_file"]).resolve()
            if not source.is_relative_to(cache):
                raise ValueError(f"Cache path outside input directory: {source}")
            target = destination(root, record["url"], page=True)
            if source.is_relative_to(root):
                raise ValueError("Output directory must not contain the source HTML cache")
            if target in targets:
                raise ValueError(f"Duplicate output path: {target}")
            targets.add(target)
            planned.append((record["url"], target, source.read_text(encoding="utf-8")))
        pending: deque[str] = deque()
        for url, target, content in planned:
            assets = Assets()
            assets.feed(content)
            pending.extend(urljoin(url, item) for item in assets.urls)
            pending.extend(urljoin(url, a or b) for a, b in CSS_URL.findall(content))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(localize(content, url, origin), encoding="utf-8")
        failures = 0
        downloaded = 0
        seen: set[str] = set()
        while args.fetch_assets and pending:
            url = pending.popleft()
            parts = urlsplit(url)
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
            if url in seen or f"{parts.scheme}://{parts.netloc}" != origin:
                continue
            seen.add(url)
            try:
                target = destination(root, url)
                if target in targets or target.exists():
                    continue
                time.sleep(args.delay)
                request = Request(url, headers={"User-Agent": "skupper-site-restore/1.0"})
                with urlopen(request, timeout=30) as response:
                    data = response.read(20 * 1024 * 1024 + 1)
                    if len(data) > 20 * 1024 * 1024:
                        raise ValueError("Asset exceeds 20 MB")
                    if "text/html" in response.headers.get("Content-Type", ""):
                        raise ValueError("Received HTML instead of an asset")
                    if "text/css" in response.headers.get("Content-Type", "") or parts.path.endswith(".css"):
                        pending.extend(urljoin(response.url, a or b) for a, b in CSS_URL.findall(data.decode("utf-8")))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                downloaded += 1
            except (OSError, ValueError) as exc:
                failures += 1
                print(f"Asset failed: {url}: {exc}", file=sys.stderr)
        skipped = len(records) - len(successful)
        print(f"Restored {len(planned)} pages to {root}; skipped {skipped} unsuccessful records; downloaded {downloaded} assets.")
        if not args.fetch_assets:
            print("Assets were not downloaded. Use --fetch-assets to retrieve same-origin assets.")
        print(f"Preview: python -m http.server 8000 --directory {root}")
        return 1 if failures else 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
