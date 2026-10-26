"""Import a pages.jsonl inventory into SQLite with SQL views for common queries."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


def create_db(source: Path, destination: Path) -> int:
    """Replace the inventory in one transaction; retain every original JSON field."""
    if source.resolve() == destination.resolve():
        raise ValueError("Input and database paths must differ")
    records = []
    with source.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or not isinstance(record.get("url"), str):
                    raise ValueError("Expected an object with a string URL")
                records.append((json.dumps(record, ensure_ascii=False, allow_nan=False),))
            except ValueError as exc:
                raise ValueError(f"{source}:{number}: {exc}") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(destination) as db:
        # Confirm JSON support before modifying an existing database.
        db.execute("SELECT json_extract('{}', '$.url')")
        db.execute("BEGIN")
        db.execute("CREATE TABLE IF NOT EXISTS pages (data TEXT NOT NULL)")
        db.execute("DELETE FROM pages")
        db.executemany("INSERT INTO pages (data) VALUES (?)", records)
        for name in ("page_summary", "page_terms", "page_links"):
            db.execute(f"DROP VIEW IF EXISTS {name}")
        fields = ("url", "title", "status", "date", "author", "words", "headings",
                  "code_blocks", "body_sha256", "html_file", "markdown_file", "error", "reason")
        columns = ", ".join(f"json_extract(data, '$.{field}') AS {field}" for field in fields)
        db.execute(f"CREATE VIEW page_summary AS SELECT {columns} FROM pages")
        db.execute("""
            CREATE VIEW page_terms AS
            SELECT json_extract(p.data, '$.url') AS url,
                   t.key AS term, t.value AS mentions
            FROM pages AS p, json_each(p.data, '$.terms') AS t
        """)
        db.execute("""
            CREATE VIEW page_links AS
            SELECT json_extract(p.data, '$.url') AS source_url,
                   link.value AS target_url
            FROM pages AS p, json_each(p.data, '$.links') AS link
        """)
    return len(records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("out/pages.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("out/pages.db"))
    args = parser.parse_args(argv)
    try:
        count = create_db(args.input, args.output)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Database import failed: {exc}", file=sys.stderr)
        return 1
    print(f"Imported {count} pages into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
