"""Small, resumable same-site HTML-to-Markdown content audit.

stdout: JSONL (scan) or Markdown (report); stderr: diagnostics.
A scan's cached HTML is authoritative unless --refresh is supplied.
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from lxml import html as lxml_html

LOG = logging.getLogger("skupper-audit")
USER_AGENT = "skupper-trafilatura-audit/0.1 (+https://skupper.io/)"
TERMS = ("site", "connector", "listener", "routing key", "token", "link", "tls")
HTML_SUFFIXES = {"", ".html", ".htm"}


def normalize_url(value: str, base: str = "https://skupper.io/") -> str | None:
    """Normalize aliases and discard non-page URLs without dropping version 2."""
    url = urljoin(base, value.strip())
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    scope = urlsplit(base)
    if parts.netloc.lower() != scope.netloc.lower() or parts.scheme != scope.scheme:
        return None
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    elif path == "/index.html":
        path = "/"
    if Path(path).suffix.lower() not in HTML_SUFFIXES:
        return None
    if not Path(path).suffix and not path.endswith("/"):
        path += "/"
    if "v1" in (s.lower() for s in path.split("/") if s):
        return None
    # Avoid path traversal; fragments/queries are not separate content pages.
    if any(s in {"..", "."} or "%2e" in s.lower() for s in path.split("/")):
        return None
    return urlunsplit((scope.scheme, scope.netloc.lower(), path, "", ""))


def discover_links(content: bytes, page_url: str) -> list[str]:
    """All internal HTML links (including nav), not only extracted body links."""
    try:
        document = lxml_html.fromstring(content, base_url=page_url)
    except (ValueError, TypeError, lxml_html.etree.ParserError) as exc:
        LOG.warning("Cannot parse links in %s: %s", page_url, exc)
        return []
    urls = set()
    for value in document.xpath("//a[@href]/@href"):
        canonical = normalize_url(value, page_url)
        if canonical:
            urls.add(canonical)
    return sorted(urls)


def http_get(url: str, timeout: float) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"})
    with urlopen(request, timeout=timeout) as response:
        body = response.read()
        if len(body) > 15_000_000:
            raise ValueError("HTML exceeds 15 MB safety limit")
        return body, response.geturl()


def robot_policy(timeout: float, ignore: bool, base: str = "https://skupper.io/") -> RobotFileParser | None:
    if ignore:
        LOG.warning("Robots checks disabled by --ignore-robots")
        return None
    parser = RobotFileParser()
    try:
        content, _ = http_get(urljoin(base, "/robots.txt"), timeout)
        parser.parse(content.decode("utf-8", errors="replace").splitlines())
    except HTTPError as exc:
        if exc.code == 404:
            LOG.info("No robots.txt found (HTTP 404)")
            parser.parse([])
        else:
            raise RuntimeError(f"Cannot check robots.txt (HTTP {exc.code})") from exc
    except (OSError, ValueError, URLError) as exc:
        raise RuntimeError(f"Cannot check robots.txt: {exc}") from exc
    return parser


def extract_page(html: bytes, url: str, precision: bool) -> tuple[str, dict[str, Any]]:
    """Use Trafilatura twice: rich Markdown plus structured metadata/text."""
    import trafilatura

    params = dict(url=url, include_comments=False, favor_precision=precision)
    markdown = trafilatura.extract(html, output_format="markdown", include_links=True, **params)
    record = trafilatura.extract(html, output_format="json", with_metadata=True, **params)
    if not markdown:
        raise ValueError("Trafilatura found no main content")
    metadata = json.loads(record) if record else {}
    return markdown.strip() + "\n", metadata


def stem_for(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def analyse_page(url: str, content: bytes, out_dir: Path, *, precision: bool) -> dict[str, Any]:
    key = stem_for(url)
    html_name, md_name = f"html/{key}.html", f"markdown/{key}.md"
    markdown, meta = extract_page(content, url, precision)
    (out_dir / "markdown" / f"{key}.md").write_text(markdown, encoding="utf-8")
    plain = meta.get("text") or markdown
    text = re.sub(r"\s+", " ", plain).strip()
    return {
        "url": url,
        "status": "ok",
        "title": meta.get("title") or "",
        "date": meta.get("date"),
        "author": meta.get("author"),
        "words": len(re.findall(r"\b[\w'-]+\b", text)),
        "headings": len(re.findall(r"(?m)^#{1,6} ", markdown)),
        "code_blocks": len(re.findall(r"(?m)^```", markdown)) // 2,
        "body_sha256": hashlib.sha256(text.casefold().encode("utf-8")).hexdigest(),
        "terms": {term: len(re.findall(r"\b" + re.escape(term) + r"\b", text, re.I)) for term in TERMS},
        "links": discover_links(content, url),
        "html_file": html_name,
        "markdown_file": md_name,
    }


def load_urls(path: str, base: str = "https://skupper.io/") -> list[str]:
    source = sys.stdin if path == "-" else open(path, encoding="utf-8")
    try:
        urls = (normalize_url(line, base) for line in source if line.strip() and not line.lstrip().startswith("#"))
        return sorted({url for url in urls if url})
    finally:
        if source is not sys.stdin:
            source.close()


def seed_sitemap(enabled: bool, base: str = "https://skupper.io/") -> list[str]:
    if not enabled:
        return []
    try:
        from trafilatura.sitemaps import sitemap_search
        urls = sitemap_search(base, sleep_time=1.0)
    except Exception as exc:  # third-party sitemap discovery can fail independently
        LOG.warning("Sitemap discovery failed: %s (falling back to link crawl)", exc)
        return []
    return sorted({url for raw in urls if (url := normalize_url(raw, base))})


def write_jsonl(record: dict[str, Any], handle: Any) -> None:
    line = json.dumps(record, sort_keys=True, ensure_ascii=False)
    print(line, file=handle)
    print(line, file=sys.stdout, flush=True)


def scan(args: argparse.Namespace) -> int:
    out_dir = args.output_dir
    # Do not replace another site's inventory when an output directory is reused.
    manifest_path = out_dir / "pages.jsonl"
    if manifest_path.exists():
        try:
            origin = urlsplit(args.seed)[:2]
            if any(urlsplit(record["url"])[:2] != origin for record in read_records(str(manifest_path))):
                LOG.error("Output directory contains another site's inventory; choose a separate --output-dir")
                return 1
        except (OSError, ValueError, KeyError, TypeError) as exc:
            LOG.error("Cannot validate existing inventory: %s", exc)
            return 1
    (out_dir / "html").mkdir(parents=True, exist_ok=True)
    (out_dir / "markdown").mkdir(parents=True, exist_ok=True)
    try:
        robots = robot_policy(args.timeout, args.ignore_robots, args.seed)
    except RuntimeError as exc:
        LOG.error("%s", exc)
        return 1

    if args.urls:
        initial = load_urls(args.urls, args.seed)
        follow = False  # explicit URLs are independent jobs, suitable for GNU Parallel
        discovered = 0
    else:
        from_sitemap = seed_sitemap(not args.no_sitemap, args.seed)
        discovered = len(from_sitemap)
        initial = sorted(set([normalize_url(args.seed, args.seed)] + from_sitemap) - {None})
        follow = True
    if not initial:
        LOG.error("No in-scope URLs found")
        return 1

    pending = deque(initial)
    queued = set(initial)
    processed: set[str] = set()
    records: list[dict[str, Any]] = []
    last_fetch = 0.0
    with (out_dir / "pages.jsonl").open("w", encoding="utf-8") as manifest:
        while pending and len(processed) < args.max_pages:
            url = pending.popleft()
            if url in processed:
                continue
            processed.add(url)
            key = stem_for(url)
            html_path = out_dir / "html" / f"{key}.html"
            record: dict[str, Any]
            links: list[str] = []
            if robots and not robots.can_fetch(USER_AGENT, url):
                record = {"url": url, "status": "blocked", "error": "robots.txt disallows this URL"}
                links = []
            else:
                try:
                    if html_path.exists() and not args.refresh:
                        source = html_path.read_bytes()
                        LOG.debug("HTML cache hit: %s", url)
                    else:
                        elapsed = time.monotonic() - last_fetch
                        if elapsed < args.delay:
                            time.sleep(args.delay - elapsed)
                        source, resolved = http_get(url, args.timeout)
                        last_fetch = time.monotonic()
                        if normalize_url(resolved, args.seed) != url:
                            # Redirects to another URL in scope are not duplicated.
                            if normalize_url(resolved, args.seed) is None:
                                raise ValueError(f"Redirected outside scope: {resolved}")
                            LOG.info("Redirect %s -> %s", url, resolved)
                        html_path.write_bytes(source)
                    links = discover_links(source, url)
                    record = analyse_page(url, source, out_dir, precision=args.precision)
                except (OSError, HTTPError, URLError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    LOG.error("Failed %s: %s", url, exc)
                    record = {"url": url, "status": "error", "error": str(exc)}
                    # Keep any discovered links even when content extraction fails.
                    # This prevents one unextractable hub page ending the crawl.
            records.append(record)
            write_jsonl(record, manifest)
            if follow:
                for link in links:
                    if link not in queued:
                        pending.append(link)
                        queued.add(link)
            LOG.info("%s [%s] (%d/%d queued)", url, record["status"], len(processed), len(queued))

    truncated = bool(pending)
    info = {
        "seed": args.seed if not args.urls else None,
        "sitemap_urls": discovered,
        "urls_found": len(queued),
        "pages_processed": len(records),
        "ok": sum(x["status"] == "ok" for x in records),
        "errors": sum(x["status"] == "error" for x in records),
        "blocked": sum(x["status"] == "blocked" for x in records),
        "truncated": truncated,
    }
    (out_dir / "run.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    LOG.info("Finished: %s", info)
    if truncated:
        LOG.error("Page limit reached; increase --max-pages for full coverage")
    if truncated:
        return 2
    return 1 if info["errors"] or info["blocked"] else 0


def read_records(input_file: str) -> list[dict[str, Any]]:
    stream = sys.stdin if input_file == "-" else open(input_file, encoding="utf-8")
    try:
        return [json.loads(line) for line in stream if line.strip()]
    finally:
        if stream is not sys.stdin:
            stream.close()


def report(records: Iterable[dict[str, Any]]) -> str:
    # Deduplicate manifest entries when merging independent worker streams.
    pages = {entry["url"]: entry for entry in records}
    successes = [p for p in pages.values() if p.get("status") == "ok"]
    errors = [p for p in pages.values() if p.get("status") != "ok"]
    terms = Counter()
    hashes: dict[str, list[str]] = {}
    for page in successes:
        terms.update(page.get("terms", {}))
        hashes.setdefault(page["body_sha256"], []).append(page["url"])
    groups = [sorted(urls) for urls in hashes.values() if len(urls) > 1]
    groups.sort(key=lambda urls: (-len(urls), urls[0]))
    lowest = sorted(successes, key=lambda p: (p.get("words", 0), p["url"]))[:15]
    sections: dict[str, list[dict[str, Any]]] = {}
    for page in successes:
        segments = [seg for seg in urlsplit(page["url"]).path.split("/") if seg]
        section = segments[0] if segments else "(home)"
        sections.setdefault(section, []).append(page)
    scanned = set(pages)
    linked = {url for p in successes for url in p.get("links", [])}
    unvisited = sorted(linked - scanned)
    lines = [
        "# Skupper website content audit", "",
        f"- Pages in manifest: **{len(pages)}**",
        f"- Successfully extracted: **{len(successes)}**",
        f"- Failed or robots-blocked: **{len(errors)}**",
        f"- Extracted words (sum across pages): **{sum(p.get('words', 0) for p in successes):,}**",
        f"- Exact extracted-text duplicate groups: **{len(groups)}**", "",
        "## By URL section", "", "| Section | Pages | Words |", "| --- | ---: | ---: |",
    ]
    for section, group in sorted(sections.items()):
        lines.append(f"| {section} | {len(group)} | {sum(p.get('words', 0) for p in group):,} |")
    lines += ["", "## Terminology", "", "| Term | Occurrences | Pages |", "| --- | ---: | ---: |"]
    for term in TERMS:
        count = sum(p.get("terms", {}).get(term, 0) > 0 for p in successes)
        lines.append(f"| {term} | {terms[term]} | {count} |")
    lines += ["", "## Shortest extracted pages", "", "| Words | Title | URL |", "| ---: | --- | --- |"]
    for page in lowest:
        title = str(page.get("title") or "(untitled)").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {page['words']} | {title} | {page['url']} |")
    lines += ["", "## Exact duplicate groups", ""]
    if groups:
        for urls in groups[:20]:
            lines.append(f"- {len(urls)} pages: " + ", ".join(urls))
    else:
        lines.append("No exact matches in extracted body text.")
    lines += ["", "## Discovered but not scanned", ""]
    if unvisited:
        lines.extend(f"- {url}" for url in unvisited[:30])
        if len(unvisited) > 30:
            lines.append(f"- ... and {len(unvisited) - 30} more (see JSONL)")
    else:
        lines.append("None found from links in successful pages.")
    lines += ["", "## Failed pages", ""]
    if errors:
        for page in sorted(errors, key=lambda p: p["url"]):
            lines.append(f"- `{page['status']}` {page['url']}: {page.get('error', '')}")
    else:
        lines.append("None in the manifest.")
    lines += ["", "_Counts reflect extracted text, not the source repository. Duplicate matching is exact after whitespace/case normalization; it does not detect paraphrases. Website coverage depends on successful discovery and crawl limits._", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="Log diagnostics to stderr")
    commands = parser.add_subparsers(dest="command", required=True)
    crawl = commands.add_parser("scan", help="Crawl, cache HTML, extract Markdown, output JSONL")
    crawl.add_argument("--seed", default="https://skupper.io/", help="Initial URL; its origin defines the crawl scope")
    crawl.add_argument("--urls", metavar="FILE", help="Only scan URLs in FILE; '-' reads stdin")
    crawl.add_argument("--output-dir", type=Path, default=Path("out"))
    crawl.add_argument("--max-pages", type=int, default=1000)
    crawl.add_argument("--delay", type=float, default=0.5, help="Seconds between HTTP page requests")
    crawl.add_argument("--timeout", type=float, default=20.0)
    crawl.add_argument("--refresh", action="store_true", help="Fetch HTML again instead of using cache")
    crawl.add_argument("--precision", action="store_true", help="Favor content precision over recall")
    crawl.add_argument("--no-sitemap", action="store_true", help="Skip sitemap; follow links from seed")
    crawl.add_argument("--ignore-robots", action="store_true", help="Only for use with explicit permission")
    rep = commands.add_parser("report", help="Read JSONL, print Markdown report")
    rep.add_argument("--input", default="-", help="JSONL file, or '-' for stdin")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(levelname)s: %(message)s")
    if args.command == "scan":
        if args.max_pages < 1 or args.delay < 0 or args.timeout <= 0:
            parser.error("--max-pages >= 1, --delay >= 0, --timeout > 0 are required")
        try:
            seed = urlsplit(args.seed)
            if (seed.scheme not in ("http", "https") or not seed.hostname
                    or seed.username is not None or seed.password is not None
                    or normalize_url(args.seed, args.seed) is None):
                parser.error("--seed must be an absolute HTTP(S) page URL without credentials")
            seed.port  # Validate malformed ports before any requests or output changes.
        except ValueError:
            parser.error("--seed has an invalid hostname or port")
        return scan(args)
    try:
        print(report(read_records(args.input)), end="")
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
        LOG.error("Cannot generate report: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
