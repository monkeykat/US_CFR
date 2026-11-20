# US CFR data

Download the current eCFR titles and build a local SQLite database with:

```bash
python3 src/download_cfr.py
```

The downloader saves each title's XML, hierarchy JSON, and version metadata under
`downloads/`, along with an `ecfr.sqlite3` database. The database stores title metadata,
agency slugs and CFR references, the title hierarchy, section text, and amendment
and issue dates. Raw title, agency, structure, and version API responses are kept
in `api_payloads`. The XML hierarchy is preserved in `xml_nodes`, with parent links,
source document order, and XML paths. Section text remains linked to those nodes.
The `xml_text_locations` table records paragraph, heading, citation, footnote, and
table-row paths relative to their section or appendix, plus character offsets
into the extracted `documents.body`. Offsets are zero-based, end-exclusive Python
Unicode character positions. The text is not duplicated in the location table;
retrieve it with `substr` and combine the relative path with the section's XML path:

```sql
SELECT d.document_key, d.source_date,
       'title-' || d.title_number || '.xml' AS source_file,
       n.xml_path || substr(l.relative_xml_path, 2) AS xml_path, l.xml_tag,
       l.body_start, l.body_end,
       substr(d.body, l.body_start + 1, l.body_end - l.body_start) AS source_text
FROM xml_text_locations AS l
JOIN documents AS d ON d.id = l.document_id
JOIN xml_nodes AS n ON n.document_id = d.id
                   AND n.type IN ('SECTION', 'APPENDIX')
WHERE d.document_key = ?
ORDER BY l.source_order;
```

These locations let a future chunker associate a chunk's offsets in `documents.body`
with its XML element path without adding chunking or embeddings to this downloader.
Nested XML locations can overlap; a chunk spanning multiple paragraphs can retain
the paths for each intersecting location. Table rows are recorded as one location
each, with the row's full XML path identifying its cells in the source file.
`source_date` identifies the snapshot the XML path belongs to. This downloader
does not create vector chunks or embeddings.
The full-text index covers structure labels and section text.

Each run compares the newly downloaded corpus with the previous SQLite database.
The `sync_changes` table tags each changed hierarchy item as `NEW`, `CHANGED`, or
`REMOVE`. It stores the old state for changed or removed items; the new state is
available by joining `sync_changes.document_key` to `documents.document_key`.
Added items have no old state, and removed items have no current document row.
The `pending_changes` view joins those states for downstream processing:

```sql
SELECT change_tag, document_key, title_number, document_type, identifier,
       old_state_json, new_document_id, new_label, new_body, new_path_json
FROM pending_changes
ORDER BY title_number, document_type, identifier;
```

After the other project successfully uploads a change, mark it as consumed:

```sql
UPDATE sync_changes
SET upload_status = 'uploaded'
WHERE document_key = ?;
```

The next daily run removes acknowledged tags. Unacknowledged tags are coalesced
against the newest download, keeping only the original old state and latest new
state; if a pending change is reversed before upload, its tag is cleared. This
keeps the feed bounded to one outstanding old/new comparison per item rather than
storing a full snapshot history.

To read section text in the original title order, join each XML node to its text:

```sql
SELECT n.title_number, n.identifier, n.heading, d.body
FROM xml_nodes AS n
JOIN documents AS d ON d.id = n.document_id
WHERE n.type = 'SECTION'
ORDER BY n.title_number, n.source_order;
```

To rebuild the SQLite database from files already in `downloads/` without fetching
the titles again, run:

```bash
python3 src/download_cfr.py --build-database-only
```

Search the local database by text and agency:

```bash
python3 src/search_ecfr_db.py "emissions" --agency-slugs environmental-protection-agency
```

The search command also accepts `--date`, `--last-modified-after`,
`--last-modified-on-or-after`, `--last-modified-before`, and
`--last-modified-on-or-before`, all using `YYYY-MM-DD`. The database contains one
eCFR snapshot, so `--date` must match the snapshot date printed by the downloader.
Modification filters use each section's latest issue date, falling back to its
amendment date when no issue date is available.
