from pathlib import Path
import sqlite3
import tempfile
import unittest

from create_db import create_db


class DatabaseTests(unittest.TestCase):
    def test_import_views_and_repeat_import(self):
        fixture = Path(__file__).parent / "fixtures/pages.jsonl"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "pages.db"
            for _ in range(2):
                self.assertEqual(create_db(fixture, database), 3)
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM pages").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT error, words FROM page_summary WHERE status = 'error'").fetchone(), ("HTTP 404", None))
                self.assertEqual(db.execute("SELECT sum(mentions) FROM page_terms WHERE term = 'routing key'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT source_url, target_url FROM page_links").fetchall(), [("https://skupper.io/", "https://skupper.io/docs/")])
            invalid = Path(directory) / "bad.jsonl"
            invalid.write_text(fixture.read_text() + "\n{bad json}\n")
            with self.assertRaisesRegex(ValueError, "bad.jsonl:"):
                create_db(invalid, database)
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM pages").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
