"""Download current eCFR title XML and structure JSON into the data folder."""

import argparse
import gzip
import json
import sqlite3
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ecfr_database import build_database


API_BASE_URL = "https://www.ecfr.gov/api/versioner/v1"
ADMIN_API_URL = "https://www.ecfr.gov/api/admin/v1"
USER_AGENT = "US-CFR-title-downloader/1.0"
TIMEOUT_SECONDS = 60


def open_url(url, accept_gzip=False):
    headers = {"User-Agent": USER_AGENT}
    if accept_gzip:
        headers["Accept-Encoding"] = "gzip"
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)


def get_current_titles():
    """Return the current eCFR snapshot date and its title metadata."""
    url = f"{API_BASE_URL}/titles.json"
    with open_url(url) as response:
        payload = json.load(response)

    if not payload.get("meta", {}).get("date") or not isinstance(payload.get("titles"), list):
        raise ValueError("The eCFR API returned an unexpected titles response.")
    if payload.get("meta", {}).get("import_in_progress"):
        raise ValueError("The eCFR API is currently rebuilding title data; try again later.")
    return payload


def get_agencies():
    """Return agency metadata, including agency slugs and CFR references."""
    url = f"{ADMIN_API_URL}/agencies.json"
    with open_url(url) as response:
        payload = json.load(response)
    if not isinstance(payload.get("agencies"), list):
        raise ValueError("The eCFR API returned an unexpected agencies response.")
    return payload


def get_saved_api_metadata(database_path):
    """Reuse the saved snapshot and agency response when rebuilding a database."""
    if not database_path.is_file():
        return None
    connection = None
    try:
        connection = sqlite3.connect(database_path)
        payloads = dict(
            connection.execute(
                "SELECT endpoint, payload_json FROM api_payloads "
                "WHERE endpoint IN (?, ?)",
                ("versioner/titles.json", "admin/agencies.json"),
            )
        )
    except sqlite3.Error:
        return None
    finally:
        if connection is not None:
            connection.close()

    if len(payloads) != 2:
        return None
    titles_payload = json.loads(payloads["versioner/titles.json"])
    agencies_payload = json.loads(payloads["admin/agencies.json"])
    if not titles_payload.get("meta", {}).get("date"):
        return None
    return titles_payload, agencies_payload


def download_title(number, snapshot_date, output_dir):
    """Stream one title's XML to disk, replacing any previous copy on success."""
    url = f"{API_BASE_URL}/full/{snapshot_date}/title-{number}.xml"
    destination = output_dir / f"title-{number}.xml"
    temporary_file = output_dir / f"title-{number}.xml.part"

    try:
        with open_url(url, accept_gzip=True) as response:
            with temporary_file.open("wb") as output_file:
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    with gzip.GzipFile(fileobj=response) as compressed_response:
                        shutil.copyfileobj(
                            compressed_response, output_file, length=1024 * 1024
                        )
                else:
                    shutil.copyfileobj(response, output_file, length=1024 * 1024)
        temporary_file.replace(destination)
    except Exception:
        temporary_file.unlink(missing_ok=True)
        raise

    return destination


def download_json(url, destination):
    """Download and pretty-print a JSON API response atomically."""
    temporary_file = destination.with_name(destination.name + ".part")
    try:
        with open_url(url, accept_gzip=True) as response:
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                with gzip.GzipFile(fileobj=response) as compressed_response:
                    payload = json.load(compressed_response)
            else:
                payload = json.load(response)

        with temporary_file.open("w", encoding="utf-8") as output_file:
            json.dump(payload, output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
        temporary_file.replace(destination)
    except Exception:
        temporary_file.unlink(missing_ok=True)
        raise

    return destination


def download_title_structure(number, snapshot_date, output_dir):
    """Download the API's JSON hierarchy for one title."""
    url = f"{API_BASE_URL}/structure/{snapshot_date}/title-{number}.json"
    return download_json(url, output_dir / f"title-{number}-structure.json")


def download_title_versions(number, output_dir):
    """Download section and appendix version metadata for one title."""
    url = f"{API_BASE_URL}/versions/title-{number}.json"
    return download_json(url, output_dir / f"title-{number}-versions.json")


def main():
    project_downloads_dir = Path(__file__).resolve().parents[1] / "downloads"
    parser = argparse.ArgumentParser(
        description="Download current eCFR title files and build the SQLite database."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=project_downloads_dir,
        help=f"directory for downloaded files and the database (default: {project_downloads_dir})",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite database path (default: <output-dir>/ecfr.sqlite3)",
    )
    parser.add_argument(
        "--build-database-only",
        action="store_true",
        help="rebuild the database from existing downloads without downloading titles",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    database_path = args.database or args.output_dir / "ecfr.sqlite3"

    try:
        saved_metadata = (
            get_saved_api_metadata(database_path) if args.build_database_only else None
        )
        if saved_metadata:
            titles_payload, agencies_payload = saved_metadata
        else:
            titles_payload = get_current_titles()
            agencies_payload = get_agencies()
        snapshot_date = titles_payload["meta"]["date"]
        titles = titles_payload["titles"]
        available_titles = [title for title in titles if not title.get("reserved")]
        if args.build_database_only:
            print(f"Rebuilding database from existing downloads (snapshot {snapshot_date})")
        else:
            print(f"Downloading {len(available_titles)} titles (eCFR snapshot {snapshot_date})")
            for title in available_titles:
                number = title.get("number")
                if not isinstance(number, int):
                    raise ValueError(f"Unexpected title entry: {title!r}")
                xml_path = download_title(number, snapshot_date, args.output_dir)
                structure_path = download_title_structure(
                    number, snapshot_date, args.output_dir
                )
                versions_path = download_title_versions(number, args.output_dir)
                print(f"Saved {xml_path}")
                print(f"Saved {structure_path}")
                print(f"Saved {versions_path}")

        created_database = build_database(
            args.output_dir,
            database_path,
            snapshot_date,
            titles_payload,
            agencies_payload,
        )
        print(f"Created SQLite database {created_database}")
    except (
        OSError,
        urllib.error.URLError,
        ValueError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as error:
        print(f"Download failed: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
