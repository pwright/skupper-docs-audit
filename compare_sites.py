"""Compare two crawl inventories by URL path, optionally showing Markdown diffs."""
from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit


def page_path(url: str) -> str:
    path = urlsplit(url).path or "/"
    if path.endswith("/index.html"):
        path = path[:-len("index.html")]
    if not Path(path).suffix:
        path = path.rstrip("/") + "/"
    return path


def inventory(path: Path) -> dict[str, dict]:
    pages = {}
    origins = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        key = page_path(record["url"])
        if key in pages:
            raise ValueError(f"{path}:{number}: duplicate page path {key}")
        parts = urlsplit(record["url"])
        origins.add((parts.scheme, parts.netloc))
        pages[key] = record
    if len(origins) != 1:
        raise ValueError(f"{path}: expected one nonempty site's inventory")
    return pages


def markdown(manifest: Path, record: dict) -> str | None:
    if not record.get("markdown_file"):
        return None
    root = manifest.resolve().parent
    path = (root / record["markdown_file"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Markdown file outside crawl directory: {path}")
    if not path.is_file():
        return None
    parts = urlsplit(record["url"])
    # Ignore deployment-origin differences in otherwise identical Markdown.
    return path.read_text(encoding="utf-8").replace(f"{parts.scheme}://{parts.netloc}", "")


def internal_links(record: dict) -> set[str]:
    origin = urlsplit(record["url"])[:2]
    return {page_path(url) if urlsplit(url)[:2] == origin else url for url in record.get("links", [])}


def compare(left: Path, right: Path, *, text_diff: bool = False) -> str:
    before, after = inventory(left), inventory(right)
    only_left = sorted(before.keys() - after.keys())
    only_right = sorted(after.keys() - before.keys())
    changed = []
    unchanged = 0
    uncomparable = []
    for path in sorted(before.keys() & after.keys()):
        a, b = before[path], after[path]
        differences = []
        for field in ("status", "title", "words", "headings", "code_blocks", "terms", "error", "reason"):
            if a.get(field) != b.get(field):
                differences.append(f"{field}: {json.dumps(a.get(field), ensure_ascii=False)} → {json.dumps(b.get(field), ensure_ascii=False)}")
        if internal_links(a) != internal_links(b):
            removed = sorted(internal_links(a) - internal_links(b))
            added = sorted(internal_links(b) - internal_links(a))
            differences.append(f"internal links removed: {json.dumps(removed)}; added: {json.dumps(added)}")
        diff = ""
        if a.get("status") == b.get("status") == "ok":
            old, new = markdown(left, a), markdown(right, b)
            content_changed = old != new if old is not None and new is not None else a.get("body_sha256") != b.get("body_sha256")
            if content_changed:
                differences.insert(0, "extracted content changed")
                if text_diff:
                    if old is not None and new is not None:
                        diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile="left" + path, tofile="right" + path))
                    else:
                        differences.append("Text diff unavailable: cached Markdown is missing; compared body hashes")
        else:
            uncomparable.append(path)
        if differences:
            changed.append((path, differences, diff))
        elif path not in uncomparable:
            unchanged += 1
    lines = ["# Website comparison", "", f"- Left: `{left}`", f"- Right: `{right}`",
             f"- Only in left inventory: {len(only_left)}", f"- Only in right inventory: {len(only_right)}",
             f"- Changed shared pages: {len(changed)}", f"- Unchanged successful pages: {unchanged}",
             f"- Shared paths with failed/blocked captures: {len(uncomparable)}", "",
             "Pages are matched by path. Missing inventory entries do not prove a page was removed; check crawl coverage and run.json.", ""]
    for title, paths in (("Only in left inventory", only_left), ("Only in right inventory", only_right), ("Shared paths without two successful captures", uncomparable)):
        lines += ["## " + title, ""]
        lines += [f"- `{path}`" for path in paths] or ["None."]
        lines.append("")
    lines += ["## Changed shared pages", ""]
    if not changed:
        lines.append("None.")
    for path, differences, diff in changed:
        lines += ["### " + path, ""] + ["- " + item for item in differences] + [""]
        if diff:
            fence = "`" * max(3, max((len(s) for s in __import__('re').findall(r'`+', diff)), default=0) + 1)
            lines += [fence + "diff", diff.rstrip("\n"), fence, ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", type=Path, default=Path("out/pages.jsonl"))
    parser.add_argument("--right", type=Path, default=Path("out/skupper-docs.netlify.app/pages.jsonl"))
    parser.add_argument("--text-diff", action="store_true", help="Include unified diffs of cached Markdown")
    args = parser.parse_args(argv)
    try:
        print(compare(args.left, args.right, text_diff=args.text_diff), end="")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Comparison failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
