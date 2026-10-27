import json
from pathlib import Path
import tempfile
import unittest

from compare_sites import compare, inventory


class ComparisonTests(unittest.TestCase):
    def test_cross_origin_content_links_and_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifests = []
            for side, origin in (("left", "https://skupper.io"), ("right", "https://skupper-docs.netlify.app")):
                folder = root / side
                folder.mkdir()
                (folder / "home.md").write_text(f"[Guide]({origin}/guide/)\n")
                (folder / "guide.md").write_text("Old guide\n" if side == "left" else "New guide\n")
                records = [
                    {"url": origin + ("/index.html" if side == "left" else "/"), "status": "ok", "body_sha256": side, "markdown_file": "home.md", "links": [origin + "/guide/"]},
                    {"url": origin + "/guide/", "status": "ok", "body_sha256": side, "markdown_file": "guide.md"},
                    {"url": origin + "/" + side + "/", "status": "ok"},
                    {"url": origin + "/failed/", "status": "error", "error": "HTTP 404"},
                ]
                manifest = folder / "pages.jsonl"
                manifest.write_text("\n".join(json.dumps(r) for r in records))
                manifests.append(manifest)
            report = compare(*manifests, text_diff=True)
            self.assertIn("Unchanged successful pages: 1", report)
            self.assertIn("Changed shared pages: 1", report)
            self.assertIn("Only in left inventory: 1", report)
            self.assertIn("Only in right inventory: 1", report)
            self.assertIn("Shared paths with failed/blocked captures: 1", report)
            self.assertIn("-Old guide\n+New guide", report)
            self.assertNotIn("internal links removed", report)
            # Manifest-only comparisons fall back to the extracted-body hash.
            (root / "right/guide.md").unlink()
            self.assertIn("Text diff unavailable", compare(*manifests, text_diff=True))

    def test_duplicate_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "pages.jsonl"
            manifest.write_text('\n'.join(json.dumps({"url": u}) for u in ("https://example.com/", "https://example.com/index.html")))
            with self.assertRaisesRegex(ValueError, "duplicate page path"):
                inventory(manifest)
