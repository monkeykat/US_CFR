"""Build a searchable SQLite copy of the downloaded eCFR corpus."""

import heapq
import itertools
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from html.entities import html5 as HTML_ENTITIES
from pathlib import Path


SCHEMA = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE api_payloads (
    endpoint TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
);
CREATE TABLE titles (
    number INTEGER PRIMARY KEY,
    name TEXT,
    latest_amended_on TEXT,
    latest_issue_date TEXT,
    up_to_date_as_of TEXT,
    reserved INTEGER NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE TABLE agencies (
    slug TEXT PRIMARY KEY,
    parent_slug TEXT,
    name TEXT NOT NULL,
    short_name TEXT,
    display_name TEXT,
    sortable_name TEXT,
    raw_json TEXT NOT NULL
);
CREATE TABLE agency_cfr_references (
    id INTEGER PRIMARY KEY,
    agency_slug TEXT NOT NULL REFERENCES agencies(slug),
    title_number INTEGER NOT NULL,
    subtitle TEXT,
    chapter TEXT,
    subchapter TEXT,
    part TEXT,
    subpart TEXT,
    section TEXT,
    raw_json TEXT NOT NULL
);
CREATE INDEX agency_refs_by_title ON agency_cfr_references(title_number);
CREATE INDEX agency_refs_by_agency ON agency_cfr_references(agency_slug);
CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    document_key TEXT,
    title_number INTEGER NOT NULL REFERENCES titles(number),
    source_date TEXT NOT NULL,
    parent_id INTEGER REFERENCES documents(id),
    type TEXT NOT NULL,
    identifier TEXT,
    label TEXT,
    label_level TEXT,
    label_description TEXT,
    reserved INTEGER,
    section_range TEXT,
    subtitle TEXT,
    chapter TEXT,
    subchapter TEXT,
    part TEXT,
    subpart TEXT,
    section TEXT,
    issue_date TEXT,
    amendment_date TEXT,
    last_modified_on TEXT,
    body TEXT NOT NULL DEFAULT '',
    path_json TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX documents_title ON documents(title_number);
CREATE INDEX documents_last_modified ON documents(last_modified_on);
CREATE INDEX documents_type ON documents(type);
CREATE UNIQUE INDEX documents_document_key ON documents(document_key);
CREATE TABLE xml_nodes (
    id INTEGER PRIMARY KEY,
    title_number INTEGER NOT NULL REFERENCES titles(number),
    parent_id INTEGER REFERENCES xml_nodes(id),
    document_id INTEGER REFERENCES documents(id),
    source_order INTEGER NOT NULL,
    level INTEGER NOT NULL,
    xml_tag TEXT NOT NULL,
    xml_path TEXT NOT NULL,
    type TEXT,
    identifier TEXT,
    volume TEXT,
    heading TEXT,
    attributes_json TEXT NOT NULL,
    UNIQUE(title_number, source_order)
);
CREATE INDEX xml_nodes_parent ON xml_nodes(parent_id, source_order);
CREATE INDEX xml_nodes_type ON xml_nodes(title_number, type, source_order);
CREATE INDEX xml_nodes_document ON xml_nodes(document_id);
CREATE TABLE xml_text_locations (
    id INTEGER PRIMARY KEY,
    title_number INTEGER NOT NULL REFERENCES titles(number),
    document_id INTEGER NOT NULL REFERENCES documents(id),
    source_order INTEGER NOT NULL,
    relative_xml_path TEXT NOT NULL,
    xml_tag TEXT NOT NULL,
    body_start INTEGER,
    body_end INTEGER
);
CREATE INDEX xml_text_locations_document
    ON xml_text_locations(document_id, source_order);
CREATE TABLE document_agencies (
    document_id INTEGER NOT NULL REFERENCES documents(id),
    agency_slug TEXT NOT NULL REFERENCES agencies(slug),
    reference_id INTEGER NOT NULL REFERENCES agency_cfr_references(id),
    PRIMARY KEY (document_id, agency_slug, reference_id)
);
CREATE INDEX document_agencies_by_slug ON document_agencies(agency_slug);
CREATE TABLE content_versions (
    id INTEGER PRIMARY KEY,
    title_number INTEGER NOT NULL REFERENCES titles(number),
    identifier TEXT,
    name TEXT,
    part TEXT,
    section TEXT,
    version_date TEXT,
    amendment_date TEXT,
    issue_date TEXT,
    type TEXT,
    removed INTEGER,
    substantive INTEGER,
    raw_json TEXT NOT NULL
);
CREATE INDEX content_versions_lookup
    ON content_versions(title_number, identifier, issue_date);
CREATE TABLE sync_changes (
    document_key TEXT PRIMARY KEY,
    change_tag TEXT NOT NULL CHECK (change_tag IN ('NEW', 'CHANGED', 'REMOVE')),
    title_number INTEGER NOT NULL,
    document_type TEXT NOT NULL,
    identifier TEXT,
    path_json TEXT NOT NULL,
    old_state_json TEXT,
    from_snapshot_date TEXT,
    to_snapshot_date TEXT NOT NULL,
    upload_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (upload_status IN ('pending', 'uploaded'))
);
CREATE INDEX sync_changes_pending ON sync_changes(upload_status, change_tag);
CREATE VIEW pending_changes AS
    SELECT c.change_tag, c.document_key, c.title_number, c.document_type,
           c.identifier, c.path_json AS old_path_json, c.old_state_json,
           c.from_snapshot_date, c.to_snapshot_date,
           d.id AS new_document_id, d.label AS new_label,
           d.label_level AS new_label_level,
           d.label_description AS new_label_description,
           d.issue_date AS new_issue_date,
           d.amendment_date AS new_amendment_date,
           d.last_modified_on AS new_last_modified_on,
           d.body AS new_body, d.path_json AS new_path_json,
           d.raw_json AS new_raw_json
    FROM sync_changes AS c
    LEFT JOIN documents AS d ON d.document_key = c.document_key
    WHERE c.upload_status = 'pending';
"""


TYPE_TO_PATH_FIELD = {
    "subtitle": "subtitle",
    "chapter": "chapter",
    "subchapter": "subchapter",
    "subchap": "subchapter",
    "part": "part",
    "subpart": "subpart",
    "section": "section",
}
XML_ENTITY = re.compile(rb"&([A-Za-z][A-Za-z0-9]+);")
XML_BUILTIN_ENTITIES = {b"amp", b"lt", b"gt", b"apos", b"quot"}
XML_PARAGRAPH_TAG = re.compile(
    r"^(?:P(?:-\d+|\d+(?:-\d+)?)?|FP(?:-\d+|\d+-\d+)?|FP-DASH|FRP|PSPACE)$"
)
XML_LOCATION_TAGS = {
    "AUTH", "CITA", "EXAMPLE", "EXTRACT", "FTNT", "FTREF", "HEAD",
    "HED", "HED1", "HED2", "HED3", "HD1", "HD2", "HD3", "NOTE",
    "SOURCE", "TR",
}


class _XmlEntityReader:
    """Resolve named SGML entities while streaming eCFR XML into ElementTree."""

    def __init__(self, source):
        self.source = source
        self.pending = b""
        self.eof = False

    @staticmethod
    def _replace_entities(data):
        def replace(match):
            name = match.group(1)
            if name in XML_BUILTIN_ENTITIES:
                return match.group(0)
            value = HTML_ENTITIES.get(name.decode("ascii") + ";")
            if value is None:
                return b"&amp;" + name + b";"
            value = value.replace("&", "&amp;").replace("<", "&lt;")
            return value.encode("utf-8")

        return XML_ENTITY.sub(replace, data)

    def read(self, size=-1):
        if size < 0:
            chunks = [self.pending]
            while True:
                chunk = self.source.read(1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            self.pending = b""
            return self._replace_entities(b"".join(chunks))

        while len(self.pending) < size and not self.eof:
            chunk = self.source.read(size - len(self.pending))
            if chunk:
                self.pending += chunk
            else:
                self.eof = True

        if self.eof:
            result, self.pending = self.pending, b""
            return self._replace_entities(result)

        safe_size = size
        while True:
            ampersand = self.pending.rfind(b"&", 0, safe_size)
            semicolon = self.pending.rfind(b";", 0, safe_size)
            if ampersand <= semicolon:
                break
            safe_size = ampersand
            if safe_size:
                break
            chunk = self.source.read(size)
            if not chunk:
                self.eof = True
                return self.read(size)
            self.pending += chunk
            safe_size = size

        result, self.pending = self.pending[:safe_size], self.pending[safe_size:]
        return self._replace_entities(result)


def _normalise(value):
    if value is None:
        return ""
    value = str(value).replace("§", " ")
    return re.sub(r"\s+", " ", value).strip().casefold()


def _document_identity(title_number, node_type, identifier, path_json):
    """Return a stable identity shared by successive builds of one document."""
    try:
        path = json.loads(path_json or "{}")
    except (TypeError, json.JSONDecodeError):
        path = {}
    normalized_path = {
        str(key): _normalise(value) for key, value in sorted(path.items())
    }
    return json.dumps(
        [int(title_number), _normalise(node_type), _normalise(identifier), normalized_path],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _document_key(identity, occurrence):
    # Padding keeps these keys in the same order as the source rows.
    return json.dumps(
        [identity, f"{occurrence:010d}"],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _register_document_identity(connection):
    connection.create_function("document_identity", 4, _document_identity)
    connection.create_function("stable_document_key", 2, _document_key)


def _flatten_xml_text(element):
    return " ".join(" ".join(element.itertext()).split())


def _is_xml_location_tag(tag):
    return bool(XML_PARAGRAPH_TAG.fullmatch(tag)) or tag in XML_LOCATION_TAGS


def _iter_xml_text_locations(root, body):
    """Locate useful text blocks in a section without copying their text."""
    locations = []

    def visit(
        element, xml_path, lower_bound, upper_bound, is_root=False,
        inside_table_row=False,
    ):
        tag = str(element.tag).upper()
        element_text = _flatten_xml_text(element)
        if is_root:
            start, end = 0, len(body)
        elif element_text:
            start = body.find(element_text, lower_bound, upper_bound)
            end = start + len(element_text) if start >= 0 else None
        else:
            start, end = None, None

        if start is not None:
            if (
                _is_xml_location_tag(tag)
                and element_text
                and not inside_table_row
            ):
                locations.append(
                    (
                        xml_path,
                        tag,
                        start,
                        end,
                    )
                )
            child_lower = start
            child_upper = end if end is not None else upper_bound
        else:
            child_lower = lower_bound
            child_upper = upper_bound

        child_counts = defaultdict(int)
        last_end = end if end is not None else lower_bound
        for child in element:
            child_tag = str(child.tag).upper()
            child_counts[child_tag] += 1
            child_path = f"{xml_path}/{child_tag}[{child_counts[child_tag]}]"
            child_end = visit(
                child,
                child_path,
                child_lower,
                child_upper,
                inside_table_row=inside_table_row or tag == "TR",
            )
            child_lower = max(child_lower, child_end)
            last_end = max(last_end, child_end)
        return last_end

    visit(root, ".", 0, len(body), is_root=True)
    return locations


def _truth(value):
    return None if value is None else int(bool(value))


def _insert_agencies(connection, payload):
    references_by_title = defaultdict(list)
    cursor = connection.cursor()

    def visit(agency, parent_slug=None):
        slug = agency.get("slug")
        if not slug:
            return
        cursor.execute(
            """INSERT INTO agencies
               (slug, parent_slug, name, short_name, display_name, sortable_name, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                slug,
                parent_slug,
                agency.get("name", ""),
                agency.get("short_name"),
                agency.get("display_name"),
                agency.get("sortable_name"),
                json.dumps(
                    {key: value for key, value in agency.items() if key != "children"},
                    ensure_ascii=False,
                ),
            ),
        )
        for reference in agency.get("cfr_references", []):
            title_number = reference.get("title")
            if title_number is None:
                continue
            cursor.execute(
                """INSERT INTO agency_cfr_references
                   (agency_slug, title_number, subtitle, chapter, subchapter,
                    part, subpart, section, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    slug,
                    int(title_number),
                    reference.get("subtitle"),
                    reference.get("chapter"),
                    reference.get("subchapter"),
                    reference.get("part"),
                    reference.get("subpart"),
                    reference.get("section"),
                    json.dumps(reference, ensure_ascii=False),
                ),
            )
            reference_id = cursor.lastrowid
            references_by_title[int(title_number)].append(
                (reference_id, slug, reference)
            )
        for child in agency.get("children", []):
            visit(child, slug)

    for agency in payload.get("agencies", []):
        visit(agency)
    return references_by_title


def _insert_structure(connection, title_number, snapshot_date, payload):
    cursor = connection.cursor()
    document_ids = defaultdict(list)

    def visit(node, parent_id=None, parent_path=None):
        parent_path = parent_path or {}
        node_type = str(node.get("type", "unknown"))
        identifier = node.get("identifier")
        path = dict(parent_path)
        path_field = TYPE_TO_PATH_FIELD.get(node_type.casefold())
        if path_field and identifier is not None:
            path[path_field] = str(identifier)

        cursor.execute(
            """INSERT INTO documents
               (title_number, source_date, parent_id, type, identifier, label,
                label_level, label_description, reserved, section_range,
                subtitle, chapter, subchapter, part, subpart, section,
                path_json, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title_number,
                snapshot_date,
                parent_id,
                node_type,
                None if identifier is None else str(identifier),
                node.get("label"),
                node.get("label_level"),
                node.get("label_description"),
                _truth(node.get("reserved")),
                node.get("section_range"),
                path.get("subtitle"),
                path.get("chapter"),
                path.get("subchapter"),
                path.get("part"),
                path.get("subpart"),
                path.get("section"),
                json.dumps(path, ensure_ascii=False),
                json.dumps(
                    {key: value for key, value in node.items() if key != "children"},
                    ensure_ascii=False,
                ),
            ),
        )
        document_id = cursor.lastrowid
        document_ids[(node_type.casefold(), _normalise(identifier))].append(document_id)
        for child in node.get("children", []):
            visit(child, document_id, path)

    visit(payload)
    return document_ids


def _extract_xml_hierarchy(connection, title_number, snapshot_date, xml_path, document_ids):
    cursor = connection.cursor()
    element_stack = []
    xml_path_stack = []
    path_child_counts = []
    xml_node_stack = []
    source_order = 0
    location_order = 0
    fields_for_xml_type = {
        "TITLE": "title",
        "SUBTITLE": "subtitle",
        "CHAPTER": "chapter",
        "SUBCHAP": "subchapter",
        "SUBCHAPTER": "subchapter",
        "PART": "part",
        "SUBPART": "subpart",
        "SUBJGRP": "subject_group",
        "SECTION": "section",
        "APPENDIX": "appendix",
    }
    structure_type_aliases = {
        "subchap": "subchapter",
        "subjgrp": "subject_group",
    }

    try:
        with open(xml_path, "rb") as source:
            events = ET.iterparse(
                _XmlEntityReader(source), events=("start", "end")
            )
            for event, element in events:
                if event == "start":
                    element_stack.append(element)
                    tag = str(element.tag).upper()
                    if xml_path_stack:
                        path_child_counts[-1][tag] += 1
                        xml_path_value = (
                            f"{xml_path_stack[-1]}/{tag}"
                            f"[{path_child_counts[-1][tag]}]"
                        )
                    else:
                        xml_path_value = f"/{tag}[1]"
                    xml_path_stack.append(xml_path_value)
                    path_child_counts.append(defaultdict(int))
                    if re.fullmatch(r"DIV[1-9]", tag):
                        source_order += 1
                        xml_type = element.attrib.get("TYPE", "")
                        normalized_type = structure_type_aliases.get(
                            xml_type.casefold(), xml_type.casefold()
                        )
                        identifier = element.attrib.get("N")
                        matches = document_ids.get(
                            (normalized_type, _normalise(identifier)), []
                        )
                        parent_id = xml_node_stack[-1][1] if xml_node_stack else None
                        cursor.execute(
                            """INSERT INTO xml_nodes
                               (title_number, parent_id, document_id, source_order,
                                level, xml_tag, xml_path, type, identifier, volume,
                                attributes_json)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                title_number,
                                parent_id,
                                matches[0] if matches else None,
                                source_order,
                                int(tag[3:]),
                                tag,
                                xml_path_value,
                                xml_type or None,
                                identifier,
                                element.attrib.get("VOLUME"),
                                json.dumps(dict(element.attrib), ensure_ascii=False),
                            ),
                        )
                        xml_node_stack.append(
                            (element, cursor.lastrowid, matches[0] if matches else None,
                             normalized_type, identifier)
                        )
                    continue

                if xml_node_stack and xml_node_stack[-1][0] is element:
                    _, xml_node_id, document_id, node_type, identifier = xml_node_stack[-1]
                    heading = ""
                    for child in element:
                        if str(child.tag).upper() == "HEAD":
                            heading = " ".join(" ".join(child.itertext()).split())
                            break
                    cursor.execute(
                        "UPDATE xml_nodes SET heading = ? WHERE id = ?",
                        (heading or None, xml_node_id),
                    )

                    path = {}
                    for ancestor in xml_node_stack:
                        field = fields_for_xml_type.get(
                            str(ancestor[0].attrib.get("TYPE", "")).upper()
                        )
                        value = ancestor[0].attrib.get("N")
                        if field and value:
                            path[field] = value

                    if node_type in ("section", "appendix"):
                        identifier = identifier or ""
                        key = (node_type, _normalise(identifier))
                        text = _flatten_xml_text(element)
                        if document_id is None:
                            cursor.execute(
                                """INSERT INTO documents
                                   (title_number, source_date, type, identifier, label,
                                    label_description, subtitle, chapter, subchapter,
                                    part, subpart, section, body, path_json, raw_json)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    title_number,
                                    snapshot_date,
                                    node_type,
                                    identifier,
                                    heading or identifier,
                                    heading,
                                    path.get("subtitle"),
                                    path.get("chapter"),
                                    path.get("subchapter"),
                                    path.get("part"),
                                    path.get("subpart"),
                                    path.get("section"),
                                    text,
                                    json.dumps(path, ensure_ascii=False),
                                    json.dumps(dict(element.attrib), ensure_ascii=False),
                                ),
                            )
                            document_id = cursor.lastrowid
                            cursor.execute(
                                "UPDATE xml_nodes SET document_id = ? WHERE id = ?",
                                (document_id, xml_node_id),
                            )
                        else:
                            cursor.execute(
                                "UPDATE documents SET body = ?, label = COALESCE(label, ?), "
                                "path_json = ? WHERE id = ?",
                                (text, heading or None, json.dumps(path, ensure_ascii=False), document_id),
                            )

                        locations = _iter_xml_text_locations(element, text)
                        location_rows = [
                            (
                                title_number,
                                document_id,
                                location_order + index,
                                block_path,
                                block_tag,
                                body_start,
                                body_end,
                            )
                            for index, (
                                block_path,
                                block_tag,
                                body_start,
                                body_end,
                            ) in enumerate(locations, start=1)
                        ]
                        cursor.executemany(
                            """INSERT INTO xml_text_locations
                               (title_number, document_id, source_order,
                                relative_xml_path, xml_tag, body_start, body_end)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            location_rows,
                        )
                        location_order += len(location_rows)

                    xml_node_stack.pop()
                    element.clear()
                    if len(element_stack) > 1:
                        element_stack[-2].remove(element)
                element_stack.pop()
                xml_path_stack.pop()
                path_child_counts.pop()
    except ET.ParseError as error:
        raise ValueError(f"Could not parse {xml_path}: {error}") from error


def _insert_versions(connection, title_number, payload, snapshot_date):
    if isinstance(payload, dict):
        records = payload.get("content_versions", payload.get("versions", []))
    else:
        records = payload
    if not isinstance(records, list):
        return

    cursor = connection.cursor()
    latest_by_identifier = {}
    for record in records:
        identifier = record.get("identifier") or record.get("section")
        issue_date = record.get("issue_date")
        amendment_date = record.get("amendment_date")
        version_date = record.get("date")
        cursor.execute(
            """INSERT INTO content_versions
               (title_number, identifier, name, part, section, version_date,
                amendment_date, issue_date, type, removed, substantive, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title_number,
                identifier,
                record.get("name"),
                record.get("part"),
                record.get("section"),
                version_date,
                amendment_date,
                issue_date,
                record.get("type"),
                _truth(record.get("removed")),
                _truth(record.get("substantive")),
                json.dumps(record, ensure_ascii=False),
            ),
        )
        candidate_date = issue_date or amendment_date or version_date
        if not identifier or not candidate_date or candidate_date > snapshot_date:
            continue
        key = _normalise(identifier)
        previous = latest_by_identifier.get(key)
        if previous is None or candidate_date > previous[0]:
            latest_by_identifier[key] = (
                candidate_date,
                issue_date,
                amendment_date,
            )

    for (node_type, identifier), document_ids in _current_document_keys(connection, title_number):
        if node_type not in ("section", "appendix"):
            continue
        current = latest_by_identifier.get(identifier)
        if current:
            _, issue_date, amendment_date = current
            last_modified = issue_date or amendment_date
            cursor.executemany(
                "UPDATE documents SET issue_date = ?, amendment_date = ?, "
                "last_modified_on = ? WHERE id = ?",
                [(issue_date, amendment_date, last_modified, doc_id) for doc_id in document_ids],
            )


def _current_document_keys(connection, title_number):
    rows = connection.execute(
        "SELECT id, type, identifier FROM documents WHERE title_number = ?",
        (title_number,),
    )
    grouped = defaultdict(list)
    for document_id, node_type, identifier in rows:
        grouped[(node_type.casefold(), _normalise(identifier))].append(document_id)
    return grouped.items()


DOCUMENT_STATE_COLUMNS = (
    "title_number, type, identifier, label, label_level, label_description, "
    "reserved, section_range, subtitle, chapter, subchapter, part, subpart, "
    "section, issue_date, amendment_date, last_modified_on, body, path_json, raw_json"
)


def _assign_document_keys(connection):
    """Persist stable natural keys, including an ordinal for duplicate citations."""
    cursor = connection.execute(
        "SELECT id, document_identity(title_number, type, identifier, path_json) "
        "FROM documents ORDER BY 2, id"
    )
    previous_identity = None
    occurrence = 0
    updates = []
    for document_id, identity in cursor:
        if identity == previous_identity:
            occurrence += 1
        else:
            previous_identity = identity
            occurrence = 0
        updates.append((_document_key(identity, occurrence), document_id))
        if len(updates) >= 2000:
            connection.executemany(
                "UPDATE documents SET document_key = ? WHERE id = ?", updates
            )
            updates.clear()
    if updates:
        connection.executemany(
            "UPDATE documents SET document_key = ? WHERE id = ?", updates
        )


def _iter_document_states(connection, has_document_key):
    """Yield (stable key, comparable state) in key order without loading all text."""
    if has_document_key:
        query = (
            f"SELECT document_key, {DOCUMENT_STATE_COLUMNS} FROM documents "
            "ORDER BY document_key"
        )
        for row in connection.execute(query):
            key = row[0]
            state_values = row[1:]
            yield key, _state_from_row(state_values)
        return

    # Databases created before change tracking have no persisted keys yet.
    query = (
        f"SELECT stable_document_key(identity, occurrence), {DOCUMENT_STATE_COLUMNS} "
        "FROM ("
        "SELECT id, document_identity(title_number, type, identifier, path_json) "
        "AS identity, ROW_NUMBER() OVER ("
        "PARTITION BY document_identity(title_number, type, identifier, path_json) "
        "ORDER BY id) - 1 AS occurrence, "
        f"{DOCUMENT_STATE_COLUMNS} FROM documents"
        ") ORDER BY 1"
    )
    for row in connection.execute(query):
        yield row[0], _state_from_row(row[1:])


def _state_from_row(row):
    (
        title_number, node_type, identifier, label, label_level,
        label_description, reserved, section_range, subtitle, chapter,
        subchapter, part, subpart, section, issue_date, amendment_date,
        last_modified_on, body, path_json, raw_json,
    ) = row
    return {
        "title_number": title_number,
        "type": node_type,
        "identifier": identifier,
        "label": label,
        "label_level": label_level,
        "label_description": label_description,
        "reserved": reserved,
        "section_range": section_range,
        "subtitle": subtitle,
        "chapter": chapter,
        "subchapter": subchapter,
        "part": part,
        "subpart": subpart,
        "section": section,
        "issue_date": issue_date,
        "amendment_date": amendment_date,
        "last_modified_on": last_modified_on,
        "body": body or "",
        "path": json.loads(path_json or "{}"),
        "raw": json.loads(raw_json or "{}"),
    }


def _state_json(state):
    if state is None:
        return None
    return json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _table_exists(connection, table_name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def _populate_sync_changes(connection, previous_database, snapshot_date):
    """Compare this build to the prior one and coalesce unacknowledged changes."""
    old_connection = None
    old_snapshot_date = None
    pending = {}
    try:
        if previous_database.is_file():
            old_connection = sqlite3.connect(
                f"file:{previous_database.resolve()}?mode=ro", uri=True
            )
            _register_document_identity(old_connection)
            if _table_exists(old_connection, "metadata"):
                metadata = dict(old_connection.execute("SELECT key, value FROM metadata"))
                old_snapshot_date = metadata.get("snapshot_date")
            if _table_exists(old_connection, "sync_changes"):
                for row in old_connection.execute(
                    "SELECT document_key, old_state_json, from_snapshot_date "
                    "FROM sync_changes WHERE upload_status = 'pending'"
                ):
                    pending[row[0]] = {
                        "old_state_json": row[1],
                        "from_snapshot_date": row[2],
                    }

            document_columns = old_connection.execute(
                "PRAGMA table_info(documents)"
            ).fetchall()
            has_document_key = any(
                column[1] == "document_key" for column in document_columns
            )
            old_states = _iter_document_states(old_connection, has_document_key)
        else:
            old_states = iter(())

        new_states = _iter_document_states(connection, True)
        old_item = next(old_states, None)
        new_item = next(new_states, None)
        # Include the first item already read from each document cursor.
        streams = heapq.merge(
            itertools.chain(
                () if old_item is None else ((old_item[0], "old", old_item[1]),),
                ((key, "old", state) for key, state in old_states),
            ),
            itertools.chain(
                () if new_item is None else ((new_item[0], "new", new_item[1]),),
                ((key, "new", state) for key, state in new_states),
            ),
            ((key, "pending", details) for key, details in sorted(pending.items())),
            key=lambda item: item[0],
        )

        inserted = 0
        for key, grouped in itertools.groupby(streams, key=lambda item: item[0]):
            old_state = None
            new_state = None
            pending_details = None
            for _, source, state in grouped:
                if source == "old":
                    old_state = state
                elif source == "new":
                    new_state = state
                else:
                    pending_details = state

            if pending_details is not None:
                baseline_json = pending_details["old_state_json"]
                baseline_state = json.loads(baseline_json) if baseline_json else None
                from_date = pending_details["from_snapshot_date"]
            else:
                baseline_state = old_state
                baseline_json = _state_json(baseline_state)
                from_date = old_snapshot_date

            if baseline_state == new_state:
                continue

            if baseline_state is None:
                change_tag = "NEW"
            elif new_state is None:
                change_tag = "REMOVE"
            else:
                change_tag = "CHANGED"

            representative = new_state or baseline_state
            connection.execute(
                """INSERT INTO sync_changes
                   (document_key, change_tag, title_number, document_type,
                    identifier, path_json, old_state_json, from_snapshot_date,
                    to_snapshot_date, upload_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    key,
                    change_tag,
                    representative["title_number"],
                    representative["type"],
                    representative["identifier"],
                    json.dumps(representative["path"], ensure_ascii=False),
                    baseline_json,
                    from_date,
                    snapshot_date,
                ),
            )
            inserted += 1

        connection.execute(
            "INSERT INTO metadata VALUES ('changes_from_snapshot_date', ?)",
            (old_snapshot_date or "",),
        )
        connection.execute(
            "INSERT INTO metadata VALUES ('pending_change_count', ?)", (str(inserted),)
        )
    finally:
        if old_connection is not None:
            old_connection.close()


def _link_document_agencies(connection, references_by_title):
    cursor = connection.cursor()
    fields = ("subtitle", "chapter", "subchapter", "part", "subpart", "section")
    for document_id, title_number, path_json in connection.execute(
        "SELECT id, title_number, path_json FROM documents"
    ):
        path = json.loads(path_json)
        for reference_id, slug, reference in references_by_title.get(title_number, []):
            if all(
                not reference.get(field)
                or _normalise(reference[field]) == _normalise(path.get(field))
                for field in fields
            ):
                cursor.execute(
                    "INSERT OR IGNORE INTO document_agencies "
                    "(document_id, agency_slug, reference_id) VALUES (?, ?, ?)",
                    (document_id, slug, reference_id),
                )


def build_database(downloads_dir, database_path, snapshot_date, titles_payload, agencies_payload):
    """Build and atomically replace the local SQLite corpus database."""
    title_metadata = titles_payload.get("titles", [])
    downloads_dir = Path(downloads_dir)
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = database_path.with_name(database_path.name + ".part")
    temporary_path.unlink(missing_ok=True)

    connection = sqlite3.connect(temporary_path)
    try:
        _register_document_identity(connection)
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("INSERT INTO metadata VALUES ('snapshot_date', ?)", (snapshot_date,))
        connection.execute("INSERT INTO metadata VALUES ('source', ?)", ("https://www.ecfr.gov",))
        connection.execute("INSERT INTO api_payloads VALUES (?, ?)", (
            "versioner/titles.json", json.dumps(titles_payload, ensure_ascii=False)
        ))
        connection.execute("INSERT INTO api_payloads VALUES (?, ?)", (
            "admin/agencies.json", json.dumps(agencies_payload, ensure_ascii=False)
        ))

        for title in title_metadata:
            connection.execute(
                """INSERT INTO titles
                   (number, name, latest_amended_on, latest_issue_date,
                    up_to_date_as_of, reserved, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    title["number"],
                    title.get("name"),
                    title.get("latest_amended_on"),
                    title.get("latest_issue_date"),
                    title.get("up_to_date_as_of"),
                    int(bool(title.get("reserved"))),
                    json.dumps(title, ensure_ascii=False),
                ),
            )

        references_by_title = _insert_agencies(connection, agencies_payload)

        for title in title_metadata:
            title_number = int(title["number"])
            if title.get("reserved"):
                continue
            structure_path = downloads_dir / f"title-{title_number}-structure.json"
            versions_path = downloads_dir / f"title-{title_number}-versions.json"
            xml_path = downloads_dir / f"title-{title_number}.xml"
            if not structure_path.is_file() or not versions_path.is_file() or not xml_path.is_file():
                raise ValueError(
                    f"Missing title data for Title {title_number}; run the downloader first."
                )

            structure_payload = json.loads(structure_path.read_text(encoding="utf-8"))
            connection.execute("INSERT INTO api_payloads VALUES (?, ?)", (
                f"versioner/structure/title-{title_number}.json",
                json.dumps(structure_payload, ensure_ascii=False),
            ))
            document_ids = _insert_structure(
                connection, title_number, snapshot_date, structure_payload
            )
            _extract_xml_hierarchy(
                connection, title_number, snapshot_date, xml_path, document_ids
            )

            versions_payload = json.loads(versions_path.read_text(encoding="utf-8"))
            connection.execute("INSERT INTO api_payloads VALUES (?, ?)", (
                f"versioner/versions/title-{title_number}.json",
                json.dumps(versions_payload, ensure_ascii=False),
            ))
            _insert_versions(connection, title_number, versions_payload, snapshot_date)

        _link_document_agencies(connection, references_by_title)

        try:
            connection.execute(
                "CREATE VIRTUAL TABLE documents_fts USING fts5(label, body, "
                "content='documents', content_rowid='id')"
            )
        except sqlite3.OperationalError:
            connection.execute(
                "CREATE TABLE documents_fts (rowid INTEGER PRIMARY KEY, label TEXT, body TEXT)"
            )
            connection.execute(
                "INSERT INTO documents_fts SELECT id, label, body FROM documents"
            )
            connection.execute("INSERT INTO metadata VALUES ('fts5_available', 'false')")
        else:
            connection.execute("INSERT INTO documents_fts(documents_fts) VALUES ('rebuild')")
            connection.execute("INSERT INTO metadata VALUES ('fts5_available', 'true')")

        _assign_document_keys(connection)
        _populate_sync_changes(connection, database_path, snapshot_date)

        connection.commit()
    except Exception:
        connection.close()
        temporary_path.unlink(missing_ok=True)
        raise
    else:
        connection.close()
        temporary_path.replace(database_path)

    return database_path
