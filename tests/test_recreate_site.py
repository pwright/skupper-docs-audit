import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import recreate_site as restore


class RestoreTests(unittest.TestCase):
    def test_unsafe_paths(self):
        for path in ("/%2e%2e/secret", "/a/../secret", "/a%5cb"):
            with self.assertRaises(ValueError):
                restore.destination(Path("/tmp/site"), "https://skupper.io" + path)

    def test_restore_and_css_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "home.html").write_text('<link rel="stylesheet" href="/main.css"><a href="https://skupper.io/docs/#top">Docs</a>')
            (base / "doc.html").write_text("<h1>Docs</h1>")
            records = [
                {"status": "ok", "url": "https://skupper.io/", "html_file": "home.html"},
                {"status": "ok", "url": "https://skupper.io/docs/", "html_file": "doc.html"},
                {"status": "error", "url": "https://skupper.io/missing/"},
            ]
            manifest = base / "pages.jsonl"
            manifest.write_text("\n".join(json.dumps(r) for r in records))
            site = base / "site"
            fetched = []

            def fetch(request, timeout):
                url = request.full_url
                fetched.append(url)
                response = io.BytesIO(b"body {background:url('/images/bg.png')}" if url.endswith(".css") else b"image")
                response.headers = {"Content-Type": "text/css" if url.endswith(".css") else "image/png"}
                response.url = url
                return response

            with patch.object(restore, "urlopen", side_effect=fetch), redirect_stdout(io.StringIO()):
                result = restore.main(["--input", str(manifest), "--output-dir", str(site), "--fetch-assets", "--delay", "0"])
            self.assertEqual(result, 0)
            self.assertIn('href="/docs/#top"', (site / "index.html").read_text())
            self.assertEqual((site / "docs/index.html").read_text(), "<h1>Docs</h1>")
            self.assertEqual((site / "images/bg.png").read_bytes(), b"image")
            self.assertEqual(len(fetched), 2)


if __name__ == "__main__":
    unittest.main()
