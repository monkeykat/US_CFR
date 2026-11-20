"""Search the local SQLite eCFR corpus using text and metadata filters."""

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path


def _validate_date(value, option):
    if value is None:
        return None
    try:
        dt.date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{option} must use YYYY-MM-DD format") from error
    return value


def search(database_path, query, agency_slugs=(), snapshot_date=None,
           last_modified_after=None, last_modified_on_or_after=None,
           last_modified_before=None, last_modified_on_or_before=None, limit=20):
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        stored_date = metadata.get("snapshot_date")
        if snapshot_date and snapshot_date != stored_date:
            raise ValueError(
                f"This database contains snapshot {stored_date}; rebuild it to search another date."
            )

        conditions = []
        parameters = []
        use_fts = metadata.get("fts5_available") == "true"
        if use_fts:
            words = re.findall(r"[\w]+", query, flags=re.UNICODE)
            if not words:
                raise ValueError("Enter at least one searchable word.")
            match_query = " AND ".join('"' + word.replace('"', '""') + '"' for word in words)
            conditions.append("documents_fts MATCH ?")
            parameters.append(match_query)
            from_clause = (
                "documents_fts JOIN documents d ON d.id = documents_fts.rowid"
            )
            select_fields = "d.id, d.title_number, d.type, d.identifier, d.label, " \
                            "d.body, d.last_modified_on, d.path_json, " \
                            "snippet(documents_fts, 1, '[', ']', ' … ', 18) AS excerpt"
            order_by = "rank"
        else:
            terms = [term for term in re.findall(r"[\w]+", query, flags=re.UNICODE)]
            if not terms:
                raise ValueError("Enter at least one searchable word.")
            conditions.extend(["(d.label LIKE ? OR d.body LIKE ?)" for _ in terms])
            for term in terms:
                like_query = "%" + term + "%"
                parameters.extend([like_query, like_query])
            from_clause = "documents d"
            select_fields = "d.id, d.title_number, d.type, d.identifier, d.label, " \
                            "d.body, d.last_modified_on, d.path_json, " \
                            "substr(d.body, 1, 300) AS excerpt"
            order_by = "d.title_number, d.id"

        if agency_slugs:
            placeholders = ", ".join("?" for _ in agency_slugs)
            conditions.append(
                "EXISTS (SELECT 1 FROM document_agencies da "
                f"WHERE da.document_id = d.id AND da.agency_slug IN ({placeholders}))"
            )
            parameters.extend(agency_slugs)

        date_filters = (
            (last_modified_after, ">"),
            (last_modified_on_or_after, ">="),
            (last_modified_before, "<"),
            (last_modified_on_or_before, "<="),
        )
        for value, operator in date_filters:
            if value:
                conditions.append(f"d.last_modified_on {operator} ?")
                parameters.append(value)

        sql = f"SELECT {select_fields} FROM {from_clause}"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += f" ORDER BY {order_by} LIMIT ?"
        parameters.append(limit)
        rows = connection.execute(sql, parameters).fetchall()

        results = []
        for row in rows:
            item = dict(row)
            item["path"] = json.loads(item.pop("path_json"))
            results.append(item)
        return stored_date, results
    finally:
        connection.close()


def main():
    default_database = Path(__file__).resolve().parents[1] / "downloads" / "ecfr.sqlite3"
    parser = argparse.ArgumentParser(
        description="Search the local eCFR SQLite database."
    )
    parser.add_argument("query", help="search heading and full text")
    parser.add_argument("--database", type=Path, default=default_database)
    parser.add_argument("--agency-slugs", nargs="+", default=[])
    parser.add_argument("--date", help="snapshot date in YYYY-MM-DD format")
    parser.add_argument("--last-modified-after")
    parser.add_argument("--last-modified-on-or-after")
    parser.add_argument("--last-modified-before")
    parser.add_argument("--last-modified-on-or-before")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    try:
        for option in (
            "date",
            "last_modified_after",
            "last_modified_on_or_after",
            "last_modified_before",
            "last_modified_on_or_before",
        ):
            setattr(args, option, _validate_date(getattr(args, option), option))
        if args.limit < 1:
            raise ValueError("--limit must be greater than zero")
        if not args.database.is_file():
            raise ValueError(
                f"Database not found: {args.database}. Run src/download_cfr.py first."
            )

        snapshot_date, results = search(
            args.database,
            args.query,
            agency_slugs=args.agency_slugs,
            snapshot_date=args.date,
            last_modified_after=args.last_modified_after,
            last_modified_on_or_after=args.last_modified_on_or_after,
            last_modified_before=args.last_modified_before,
            last_modified_on_or_before=args.last_modified_on_or_before,
            limit=args.limit,
        )
        print(f"Snapshot: {snapshot_date}; {len(results)} result(s)")
        for result in results:
            path = result["path"]
            citation = f"{result['title_number']} CFR"
            if path.get("section"):
                citation += f" § {path['section']}"
            print(f"\n{citation} [{result['type']}] {result.get('label') or ''}")
            if result.get("last_modified_on"):
                print(f"Last modified: {result['last_modified_on']}")
            excerpt = result.get("excerpt") or result.get("body", "")[:300]
            if excerpt:
                print(excerpt)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Search failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
