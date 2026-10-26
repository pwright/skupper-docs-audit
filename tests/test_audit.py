"""Unit, offline crawl and golden-file tests (no web access)."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import skupper_audit as audit

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"


class AuditTests(unittest.TestCase):
    def test_normalize_url(self) -> None:
        self.assertEqual(audit.normalize_url("/docs/index.html#top"), "https://skupper.io/docs/")
        self.assertEqual(audit.normalize_url("https://skupper.io/start?foo=1"), "https://skupper.io/start/")
        for value in ("/v1/", "/v1/install/", "https://example.com/", "/install.sh", "mailto:dev@example.com"):
            self.assertIsNone(audit.normalize_url(value))

    def test_html_links(self) -> None:
        html = (FIXTURES / "home.html").read_bytes()
        self.assertEqual(audit.discover_links(html, "https://skupper.io/"), ["https://skupper.io/docs/"])

    def test_other_site_scope(self) -> None:
        base = "https://skupper-docs.netlify.app/"
        self.assertEqual(audit.normalize_url("/docs/index.html#top", base), base + "docs/")
        self.assertIsNone(audit.normalize_url("https://skupper.io/docs/", base))
        self.assertIsNone(audit.normalize_url("http://skupper-docs.netlify.app/docs/", base))
        html = b'<a href="/guide/">Guide</a><a href="https://skupper.io/">Production</a>'
        self.assertEqual(audit.discover_links(html, base), [base + "guide/"])

    def test_other_site_robots_and_sitemap(self) -> None:
        base = "https://skupper-docs.netlify.app/"
        with patch.object(audit, "http_get", return_value=(b"User-agent: *\nDisallow: /private/", base + "robots.txt")) as fetch:
            robots = audit.robot_policy(20, False, base)
            fetch.assert_called_once_with(base + "robots.txt", 20)
            self.assertFalse(robots.can_fetch(audit.USER_AGENT, base + "private/"))
        with patch("trafilatura.sitemaps.sitemap_search", return_value=[base + "docs/", "https://skupper.io/"]) as search:
            self.assertEqual(audit.seed_sitemap(True, base), [base + "docs/"])
            search.assert_called_once_with(base, sleep_time=1.0)

    def test_other_site_crawl_and_output_isolation(self) -> None:
        base = "https://skupper-docs.netlify.app/"
        html = b'<title>Preview</title><main>Preview docs</main><a href="/guide/">Guide</a><a href="https://skupper.io/">Production</a>'
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            args = ["scan", "--seed", base, "--output-dir", str(out), "--delay", "0"]
            with patch.object(audit, "robot_policy", return_value=None), patch.object(audit, "seed_sitemap", return_value=[]), patch.object(audit, "http_get", side_effect=lambda url, timeout: (html, url)) as fetch, patch.object(audit, "extract_page", return_value=("# Preview\n", {"text": "Preview docs"})), redirect_stdout(io.StringIO()):
                self.assertEqual(audit.main(args), 0)
            self.assertEqual({call.args[0] for call in fetch.call_args_list}, {base, base + "guide/"})
            records = audit.read_records(str(out / "pages.jsonl"))
            self.assertEqual({r["url"] for r in records}, {base, base + "guide/"})
            original = (out / "pages.jsonl").read_bytes()
            with patch.object(audit, "http_get", side_effect=AssertionError("Must not fetch")):
                self.assertEqual(audit.main(["scan", "--output-dir", str(out)]), 1)
            self.assertEqual((out / "pages.jsonl").read_bytes(), original)

    def test_other_site_redirect_outside_scope(self) -> None:
        base = "https://skupper-docs.netlify.app/"
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(audit, "robot_policy", return_value=None), patch.object(audit, "seed_sitemap", return_value=[]), patch.object(audit, "http_get", return_value=(b"redirect", "https://skupper.io/")), redirect_stdout(io.StringIO()):
                self.assertEqual(audit.main(["scan", "--seed", base, "--output-dir", directory]), 1)
            record = audit.read_records(str(Path(directory) / "pages.jsonl"))[0]
            self.assertIn("Redirected outside scope", record["error"])

    def test_scan_crawl_and_reuse_cached_html(self) -> None:
        pages = {
            "https://skupper.io/": (FIXTURES / "home.html").read_bytes(),
            "https://skupper.io/docs/": (FIXTURES / "docs.html").read_bytes(),
            "https://skupper.io/start/": (FIXTURES / "start.html").read_bytes(),
        }
        fake = types.ModuleType("trafilatura")

        def fake_extract(html: bytes, *, output_format: str, **kwargs: object) -> str:
            from lxml import html as lxml_html
            tree = lxml_html.fromstring(html)
            title = tree.xpath("//title/text()")[0]
            text = " ".join(tree.xpath("//main//text()"))
            if output_format == "json":
                return json.dumps({"title": title, "text": text})
            return f"# {title}\n\n{text}"

        fake.extract = fake_extract
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory) / "out"
            fetches: list[str] = []

            def fetch(url: str, timeout: float) -> tuple[bytes, str]:
                fetches.append(url)
                return pages[url], url

            with patch.dict(sys.modules, {"trafilatura": fake}), patch.object(audit, "robot_policy", return_value=None), patch.object(audit, "seed_sitemap", return_value=[]), patch.object(audit, "http_get", side_effect=fetch), redirect_stdout(io.StringIO()):
                rc = audit.main(["scan", "--output-dir", str(out_dir), "--delay", "0"])
            self.assertEqual(rc, 0)
            self.assertEqual(set(fetches), set(pages))
            records = audit.read_records(str(out_dir / "pages.jsonl"))
            self.assertEqual(len(records), 3)
            self.assertTrue(all(record["status"] == "ok" for record in records))
            self.assertNotIn("https://skupper.io/v1/", [r["url"] for r in records])
            self.assertTrue(all((out_dir / r["markdown_file"]).exists() for r in records))
            self.assertFalse(json.loads((out_dir / "run.json").read_text())["truncated"])
            with patch.dict(sys.modules, {"trafilatura": fake}), patch.object(audit, "robot_policy", return_value=None), patch.object(audit, "seed_sitemap", return_value=[]), patch.object(audit, "http_get", side_effect=AssertionError("Network should not be used")), redirect_stdout(io.StringIO()):
                self.assertEqual(audit.main(["scan", "--output-dir", str(out_dir)]), 0)

    def test_report_golden(self) -> None:
        input_file = FIXTURES / "pages.jsonl"
        expected = (FIXTURES / "report.md").read_text(encoding="utf-8")
        self.assertEqual(audit.report(audit.read_records(str(input_file))), expected)


if __name__ == "__main__":
    unittest.main()
