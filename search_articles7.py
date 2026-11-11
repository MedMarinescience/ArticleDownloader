#!/usr/bin/env python3
"""Keyword search for academic articles -> CSV with an APA 7 reference column.

Sources:
  semantic_scholar  relevance-ranked search (abstracts, journal info, OA PDFs)
  openalex          public works search (abstracts, journal, volume, issue, pages)
  scopus            Elsevier Scopus search, only if ELSEVIER_API_KEY is set
  yok_tez, tr_dizin, dergipark  search supplied exports or configured OAI metadata
  crossref, europe_pmc       international metadata discovery

Optional keys (environment variables, never write them in this file):
  SEMANTIC_SCHOLAR_API_KEY, ELSEVIER_API_KEY, OPENALEX_API_KEY
  UNPAYWALL_EMAIL   (required for Unpaywall OA PDF lookup; use your academic email)

Ways to run it:
  1) Press the Run button: uses DEFAULT_QUERIES below.
  2) Terminal, your own keywords (these replace DEFAULT_QUERIES):
       python search_articles.py "riverine nutrient flux" "Uluabat Lake" --max 100 --out test.csv
  3) Only recent papers:
       python search_articles.py --year-from 2005 --out recent.csv
  4) Download PDFs for a reviewed selection through direct Sci-Hub lookup:
       python search_articles7.py --from-csv selected.csv --download-pdfs --out selected_with_pdfs.csv
     Use --pdf-provider unpaywall for Unpaywall only, or both for OA then Sci-Hub.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
import warnings
import json
import html
import math
import hashlib
import unicodedata
from urllib.parse import quote, unquote
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

# Hides the harmless urllib3/chardet version warning that appears when importing requests.
warnings.filterwarnings("ignore", message=".*doesn't match a supported version.*")
import requests  # noqa: E402
from scihub_downloader import (
    PDF_FIELDS, SciHubDownloader, add_scihub_arguments, download_rows, read_selected,
)
"""Plan Turkish catalogue searches and normalize permitted exports. No web scraping."""
import argparse
import csv
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
SOURCES = {'yok_tez': {'name': 'YOKTez', 'url': 'https://tez.yok.gov.tr/UlusalTezMerkezi/', 'kind': 'thesis catalogue', 'instruction': 'Use catalogue advanced search; search thesis title, abstract and keywords separately where available. Include all thesis access statuses; PDF permission is not a metadata inclusion criterion.'}, 'tr_dizin': {'name': 'TRDizin', 'url': 'https://search.trdizin.gov.tr/', 'kind': 'national index', 'instruction': 'Use the portal search fields and retain the exact executed expression. Review title, abstract and keyword matches; export metadata only through a supported interface.'}, 'dergipark': {'name': 'DergiPark', 'url': 'https://dergipark.org.tr/', 'kind': 'journal platform', 'instruction': 'Use article search and inspect journal-specific metadata. Do not assume a hosted journal is TR Dizin indexed; retain both source labels when found in both.'}}
QUERIES = ['submarine groundwater discharge', 'denizaltı yeraltısuyu boşalımı', 'deniz altı yeraltı suyu boşalımı', 'yeraltısuyu deniz deşarjı', 'kıyısal yeraltı suyu', 'kıyı akiferi', 'karstik kaynak', 'yeraltı suyu besin tuzu', 'yeraltısuyu nitrat', 'groundwater nutrient flux', 'subterranean estuary', 'radon yeraltı suyu', 'radyum kıyı', 'Mersin yeraltı suyu', 'Göksu besin tuzu', 'Goksu nutrient', 'Seyhan besin tuzu', 'Ceyhan besin tuzu', 'Berdan yeraltı suyu', 'Lamas karst', 'Limonlu kaynak', 'Deliçay nitrat', 'Susurluk besin tuzu', 'Marmara ötrofikasyon', 'Akdeniz yeraltı suyu', 'İskenderun Körfezi besin']
PLAN_FIELDS = ['Search_ID', 'Source', 'Source_type', 'Portal', 'Planned_query', 'Field_to_check', 'Status', 'Executed_query', 'Executed_at', 'Results_reported', 'Records_exported', 'Export_file', 'Restrictions_or_notes', 'Instructions']

def write_plan(path, queries=None, sources=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open('x', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=PLAN_FIELDS)
        writer.writeheader()
        for source in SOURCES if sources is None else sources:
            info = SOURCES[source]
            for query in QUERIES if queries is None else queries:
                count += 1
                writer.writerow({'Search_ID': f'{source}-{count:04d}', 'Source': info['name'], 'Source_type': info['kind'], 'Portal': info['url'], 'Planned_query': query, 'Field_to_check': 'title / abstract / keywords (as available)', 'Status': 'pending_manual_search', 'Instructions': info['instruction']})
    return count

def header_key(value):
    text = unicodedata.normalize('NFKD', value.replace('ı', 'i').casefold())
    return re.sub('[^a-z0-9]', '', ''.join((c for c in text if not unicodedata.combining(c))))
ALIASES = {'Title': ['Title', 'Başlık', 'Makale Adı', 'Tez Adı', 'Tez Başlığı', 'Eser Adı'], 'Authors': ['Authors', 'Author', 'Yazar', 'Yazarlar', 'Yazar Adı'], 'Year': ['Year', 'Yıl', 'Yayın Yılı', 'Tarih'], 'Journal': ['Journal', 'Journal Name', 'Dergi', 'Dergi Adı'], 'DOI': ['DOI', 'Digital Object Identifier'], 'Link': ['Link', 'URL', 'Bağlantı', 'Erişim Adresi'], 'Institution': ['Institution', 'University', 'Üniversite', 'Üniversite Adı', 'Kurum'], 'Language': ['Language', 'Dil', 'Tez Dili', 'Yayın Dili'], 'Keywords': ['Keywords', 'Anahtar Kelimeler', 'Anahtar Sözcükler'], 'Thesis_ID': ['Thesis_ID', 'Tez No', 'Tez Numarası', 'Thesis Number'], 'Document_type': ['Document_type', 'Publication Type', 'Yayın Türü', 'Tez Türü'], 'Volume': ['Volume', 'Cilt'], 'Issue': ['Issue', 'Sayı'], 'Pages': ['Pages', 'Sayfa'], 'Record_IDs': ['Record_IDs', 'Record ID', 'Kayıt No', 'Kayıt Numarası'], 'ORCIDs': ['ORCIDs', 'ORCID']}
ABSTRACT_HEADERS = ['Abstract', 'Özet', 'Türkçe Özet', 'English Abstract', 'İngilizce Özet']

def map_csv_record(record):
    indexed = {}
    for label, value in record.items():
        if label is None:
            raise ValueError('CSV row has extra cells; check delimiter and quoting')
        if not value:
            continue
        key = header_key(label)
        if key in indexed and indexed[key] != value:
            raise ValueError('Ambiguous normalized column: ' + label)
        indexed[key] = value.strip()
    row = {}
    for field, aliases in ALIASES.items():
        for label in aliases:
            if indexed.get(header_key(label)):
                row[field] = indexed[header_key(label)]
                break
    abstracts = []
    for label in ABSTRACT_HEADERS:
        value = indexed.get(header_key(label))
        if value and value not in [v for _, v in abstracts]:
            abstracts.append((label, value))
    row['Abstract'] = abstracts[0][1] if len(abstracts) == 1 else '\n\n'.join((f'[{label}] {text}' for label, text in abstracts))
    return row

def read_ris(path):
    """Strict tagged RIS with multiline fields; refuses silently truncated records."""
    record, last = ({}, None)
    with Path(path).open(encoding='utf-8-sig') as handle:
        for number, line in enumerate(handle, 1):
            line = line.rstrip('\r\n')
            match = re.match('^([A-Z0-9]{2})  - ?(.*)$', line)
            if match:
                tag, value = match.groups()
                if tag == 'TY':
                    if record:
                        raise ValueError(f'RIS missing ER before line {number}')
                    record, last = ({'TY': [value]}, 'TY')
                elif tag == 'ER':
                    if not record:
                        raise ValueError(f'RIS ER without TY at line {number}')
                    yield record
                    record, last = ({}, None)
                else:
                    if not record:
                        raise ValueError(f'RIS field outside a record at line {number}')
                    record.setdefault(tag, []).append(value)
                    last = tag
            elif line.strip():
                if not record or not last:
                    raise ValueError(f'Unsupported RIS text at line {number}')
                record[last][-1] += '\n' + line.strip()
    if record:
        raise ValueError('RIS ended without ER; export may be incomplete')

def map_ris_record(record):

    def first(*tags):
        return next((record[tag][0] for tag in tags if record.get(tag)), '')
    kind = first('TY').upper()
    year = re.search('\\b(?:18|19|20|21)\\d{2}\\b', first('PY', 'Y1', 'DA'))
    first_page, last_page = (first('SP'), first('EP'))
    return {'Title': first('TI', 'T1'), 'Authors': '; '.join(record.get('AU') or record.get('A1') or []), 'Year': year.group() if year else '', 'Journal': first('JO', 'JF', 'JA', 'T2') if kind != 'THES' else '', 'Abstract': first('AB', 'N2'), 'DOI': first('DO'), 'Link': first('UR'), 'Record_IDs': first('ID'), 'Language': first('LA'), 'Keywords': '; '.join(record.get('KW', [])), 'Document_type': {'THES': 'thesis', 'JOUR': 'article', 'CONF': 'conference', 'CPAPER': 'conference'}.get(kind, kind.lower()), 'Volume': first('VL'), 'Issue': first('IS'), 'Pages': first_page + ('-' + last_page if last_page else '')}

def _turkish_norm_doi(value):
    from urllib.parse import unquote
    text = unquote((value or '').strip()).lower()
    return re.sub('^(?:https?://(?:dx\\.)?doi\\.org/|doi:\\s*)', '', text).strip()

def _write_turkish_csv(path, rows):
    fields = list(dict.fromkeys(list(ALIASES) + ['Abstract', 'Source', 'Abstract_source', 'Provenance', 'Record_source_file', 'Query', 'Import_notes'] + [key for row in rows for key in row]))
    with Path(path).open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

def load_records(source, input_path, delimiter=','):
    """Return normalized metadata and original export fields without writing files."""
    path = Path(input_path)
    info = SOURCES[source]
    if path.suffix.casefold() == '.ris':
        records = [(raw, map_ris_record(raw)) for raw in read_ris(path)]
    elif path.suffix.casefold() == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            if not reader.fieldnames or any((not h for h in reader.fieldnames)) or len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise ValueError('CSV requires distinct nonempty column headers')
            records = [(raw, map_csv_record(raw)) for raw in reader]
    else:
        raise ValueError('Supported import formats: .csv and .ris')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rows, raw_entries = ([], [])
    for index, (raw, row) in enumerate(records, 1):
        if not any((row.get(k) for k in ('Title', 'DOI', 'Record_IDs', 'Thesis_ID'))):
            raise ValueError(f'Record {index} has no mapped title or identifier; check field mapping')
        row['DOI'] = _turkish_norm_doi(row.get('DOI'))
        row['Source'] = info['name']
        row['Record_source_file'] = path.name
        row['Provenance'] = f"{info['name']}; sha256:{digest}; record:{index}"
        row['Query'] = 'authorized export import (search expression recorded in search plan)'
        if source == 'yok_tez':
            row['Document_type'] = 'thesis'
        elif not row.get('Document_type'):
            row['Document_type'] = 'article'
        if row.get('Thesis_ID'):
            row['Record_IDs'] = info['name'] + ':thesis:' + row['Thesis_ID']
        elif row.get('Record_IDs'):
            row['Record_IDs'] = info['name'] + ':' + row['Record_IDs']
        if row.get('Abstract'):
            row['Abstract_source'] = info['name']
        row['Import_notes'] = 'Source label supplied by operator; native portal export schema and authenticity not independently verified.'
        raw_entries.append({'source': info['name'], 'input_sha256': digest, 'record': index, 'fields': raw, 'imported_at': datetime.now(timezone.utc).isoformat()})
        rows.append(row)
    return (rows, raw_entries)

def import_records(source, input_path, output_path, delimiter=','):
    output = Path(output_path)
    if output.exists() or Path(str(output) + '.import.jsonl').exists():
        raise FileExistsError('Choose a new output path to preserve existing imports')
    rows, raw_entries = load_records(source, input_path, delimiter)
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_turkish_csv(str(output), rows)
    with Path(str(output) + '.import.jsonl').open('x', encoding='utf-8') as log:
        for entry in raw_entries:
            log.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return len(rows)

class SourceUnavailable(RuntimeError):
    """A selected catalogue has no configured metadata retrieval route."""

def parse_bindings(values, allowed=SOURCES):
    result = {}
    for binding in values:
        source, separator, value = binding.partition('=')
        if not separator or source not in allowed or (not value.strip()):
            raise ValueError('Expected SOURCE=VALUE with source yok_tez, tr_dizin or dergipark')
        if source in result:
            raise ValueError('Duplicate configuration for ' + source)
        result[source] = value.strip()
    return result

def search_terms(text):
    text = unicodedata.normalize('NFKD', text.replace('ı', 'i').casefold())
    return set(re.findall('[^\\W_]+', ''.join((c for c in text if not unicodedata.combining(c)))))

def parse_oai_xml(content):
    import xml.etree.ElementTree as ET
    if len(content) > 10 * 1024 * 1024 or re.search(b'<!\\s*(?:DOCTYPE|ENTITY)', content, re.I):
        raise ValueError('Unsupported or oversized OAI XML response')
    root = ET.fromstring(content)
    ns = '{http://www.openarchives.org/OAI/2.0/}'
    if root.tag != ns + 'OAI-PMH':
        raise ValueError('Endpoint did not return OAI-PMH XML')
    error = root.find(ns + 'error')
    if error is not None and error.get('code') != 'noRecordsMatch':
        raise RuntimeError('OAI error: ' + error.get('code', 'unknown'))
    return root

def oai_record(element, source):
    """Map unqualified Dublin Core; descriptions are not guaranteed abstracts."""
    oai, dc = ('{http://www.openarchives.org/OAI/2.0/}', '{http://purl.org/dc/elements/1.1/}')
    header = element.find(oai + 'header')
    if header is None or header.get('status') == 'deleted':
        return None
    metadata = element.find(oai + 'metadata')
    if metadata is None:
        return None

    def fields(name):
        return [' '.join(node.itertext()).strip() for node in metadata.iter(dc + name) if ''.join(node.itertext()).strip()]
    identifiers, dates = (fields('identifier'), fields('date'))
    doi = next((match.group(1) for value in identifiers if (match := re.fullmatch('(?:https?://(?:dx\\.)?doi\\.org/|doi:\\s*)?(10\\.\\d{4,9}/\\S+)', value, re.I))), '')
    year = next((match.group() for value in dates if (match := re.search('\\b(?:18|19|20|21)\\d{2}\\b', value))), '')
    types = fields('type')
    row = {'Title': ' / '.join(fields('title')), 'Authors': '; '.join(fields('creator')), 'Abstract': '\n\n'.join(fields('description')), 'Year': year, 'DOI': doi, 'Link': next((v for v in identifiers if v.startswith(('https://', 'http://'))), ''), 'Record_IDs': SOURCES[source]['name'] + ':' + (header.findtext(oai + 'identifier') or ''), 'Keywords': '; '.join(fields('subject')), 'Language': '; '.join(fields('language')), 'Source': SOURCES[source]['name'], 'Document_type': 'thesis' if source == 'yok_tez' else 'article' if source == 'dergipark' else '; '.join(types), 'Abstract_source': SOURCES[source]['name'] + ':dc.description' if fields('description') else '', 'Import_notes': 'OAI Dublin Core descriptions may include notes rather than abstracts. Publication year inferred from dc:date; verify its meaning.'}
    return row if row['Title'] or doi else None

class TurkishSearchClient:
    """Search configured exports or OAI-PMH metadata once per source per run.

    OAI-PMH provides harvesting, not keyword search; result coverage is bounded by
    the harvest budget, selected set and date range. No portal endpoint is assumed.
    """

    def __init__(self, exports=None, endpoints=None, delimiter=',', harvest_limit=1000, sets=None, since=None, raw_log=None):
        from urllib.parse import urlsplit
        self.exports, self.endpoints = (exports or {}, endpoints or {})
        if self.exports.keys() & self.endpoints.keys():
            raise ValueError('Choose either an export or an OAI endpoint for each source')
        for endpoint in self.endpoints.values():
            url = urlsplit(endpoint)
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError('OAI endpoint must be an HTTPS base URL without credentials, query or fragment')
        if harvest_limit < 1 or len(delimiter) != 1:
            raise ValueError('Harvest limit must be positive and delimiter must be one character')
        if since:
            from datetime import date
            date.fromisoformat(since)
        self.delimiter, self.harvest_limit = (delimiter, harvest_limit)
        self.sets, self.since, self.raw_log = (sets or {}, since, raw_log)
        self.cache, self.details, self.failures = ({}, {}, {})
        self.requests = {}

    def _log(self, entry):
        if self.raw_log:
            with Path(self.raw_log).open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def _fetch(self, sess, source, params):
        import time
        time.sleep(1)
        response = sess.get(self.endpoints[source], params=params, timeout=60)
        if response.status_code != 200:
            raise RuntimeError(f'OAI HTTP {response.status_code}')
        root = parse_oai_xml(response.content)
        self._log({'source': source, 'endpoint': self.endpoints[source], 'params': params, 'retrieved_at': datetime.now(timezone.utc).isoformat(), 'xml': response.content.decode('utf-8', errors='replace')})
        return root

    def _load(self, sess, source):
        if source in self.failures:
            raise self.failures[source]
        if source in self.cache:
            return
        self.cache[source] = []
        try:
            if source in self.exports:
                rows, raw = load_records(source, self.exports[source], self.delimiter)
                for entry in raw:
                    self._log(entry)
                self.cache[source] = rows
                self.details[source] = {'mode': 'export_search', 'inventory_records': len(rows), 'scope': 'supplied export only; catalogue coverage unknown', 'harvest_truncated': False}
            elif source in self.endpoints:
                self.details[source] = {'mode': 'oai_harvest_search', 'harvest_limit': self.harvest_limit, 'records_scanned': 0, 'harvest_truncated': False, 'scope': 'configured endpoint/set/date only; not complete catalogue coverage'}
                ns = '{http://www.openarchives.org/OAI/2.0/}'
                identity = self._fetch(sess, source, {'verb': 'Identify'})
                if identity.find(ns + 'Identify') is None:
                    raise ValueError('OAI Identify response was not valid')
                params = {'verb': 'ListRecords', 'metadataPrefix': 'oai_dc'}
                if self.sets.get(source):
                    params['set'] = self.sets[source]
                if self.since:
                    params['from'] = self.since
                seen_tokens = set()
                while True:
                    root = self._fetch(sess, source, params)
                    records = root.findall('.//' + ns + 'ListRecords/' + ns + 'record')
                    scanned = self.details[source]['records_scanned']
                    remaining = self.harvest_limit - scanned
                    for element in records[:remaining]:
                        self.details[source]['records_scanned'] += 1
                        row = oai_record(element, source)
                        if row:
                            row['Provenance'] = 'OAI:' + self.endpoints[source]
                            self.cache[source].append(row)
                    token = root.findtext('.//' + ns + 'resumptionToken', '').strip()
                    if len(records) > remaining or (token and self.details[source]['records_scanned'] >= self.harvest_limit):
                        self.details[source]['harvest_truncated'] = True
                        break
                    if not token:
                        break
                    if token in seen_tokens:
                        raise RuntimeError('OAI repeated a resumption token; harvest incomplete')
                    seen_tokens.add(token)
                    params = {'verb': 'ListRecords', 'resumptionToken': token}
                self.details[source]['inventory_records'] = len(self.cache[source])
            else:
                raise SourceUnavailable('Configure --turkish-export ' + source + '=FILE or --turkish-oai ' + source + '=HTTPS_ENDPOINT; no verified built-in portal API')
        except Exception as exc:
            self.failures[source] = exc
            raise

    def search(self, sess, source, query, limit, year_from=None):
        if re.search('\\b(?:AND|OR|NOT)\\b', query):
            raise ValueError('Turkish metadata search accepts plain words; use separate queries instead of Boolean operators')
        self._load(sess, source)
        terms = search_terms(query)
        matches = []
        for row in self.cache[source]:
            year = re.search('\\b\\d{4}\\b', str(row.get('Year', '')))
            if year_from and year and (int(year.group()) < year_from):
                continue
            text = ' '.join((row.get(field, '') for field in ('Title', 'Abstract', 'Keywords', 'Authors')))
            if terms <= search_terms(text):
                matches.append(dict(row, Query=query))
        details = dict(self.details[source], matching_records=len(matches), returned=min(limit, len(matches)), match_rule='all normalized query tokens across title/abstract/keywords/authors; no stemming')
        details['status'] = 'possible_truncation' if len(matches) > limit or details['harvest_truncated'] else 'completed_local_scope'
        self.requests[source, query] = details
        return matches[:limit]

TURKISH_SOURCE_NAMES = set(SOURCES)
TURKISH_SOURCES = SOURCES
CATALOGUE_QUERIES = QUERIES
TURKISH_HELPER_AVAILABLE = True

S2_BASE = "https://api.semanticscholar.org/graph/v1"
OPENALEX_WORKS = "https://api.openalex.org/works"
SCOPUS_SEARCH = "https://api.elsevier.com/content/search/scopus"
CROSSREF_WORKS = "https://api.crossref.org/works"
UNPAYWALL_DOI = "https://api.unpaywall.org/v2"
S2_FIELDS = "title,authors,abstract,year,url,externalIds,openAccessPdf,citationCount,venue,journal"
OPENALEX_SELECT = (
    "id,doi,display_name,authorships,primary_location,publication_year,"
    "abstract_inverted_index,biblio,cited_by_count"
)
USER_AGENT = "literature-keyword-search/2.1 (mailto:local-research-script)"
SCRIPT_VERSION = "v7.1: metadata discovery, Turkish workflow and direct Sci-Hub PDF lookup"
AUDIT = []
RESPONSE_LOG = None
AUTHOR_FILTER = ""

# ---------------------------------------------------------------------------
# EDIT THESE TWO LISTS TO MATCH YOUR NEEDS
# ---------------------------------------------------------------------------

# Used when you run the script without typing any keywords.
# The lists are grouped so you can see which kind of search each one is.

TOPIC_QUERIES = [
    # Nutrient fluxes from rivers to the sea
    "riverine nutrient flux",
    "catchment nutrient export",
    "dissolved inorganic nitrogen load river",
    "nitrogen phosphorus river export to coastal sea",
    # High-frequency monitoring and load estimation
    "high-frequency water quality monitoring",
    "high-frequency nutrient dynamics river",
    "in situ nitrate sensor river",
    "sampling frequency nutrient load estimation",
    "WRTDS nutrient load",
    "LOADEST nutrient load",
    # Concentration-discharge and events
    "concentration-discharge relationship",
    "nitrate concentration-discharge hysteresis",
    "storm event nutrient export",
    "chemostatic nutrient export",
    # Lakes, reservoirs, dams
    "lake nutrient retention",
    "reservoir nitrogen retention",
    "dam impact riverine nutrient fluxes",
    # Land use, sources, budgets
    "land use nutrient export catchment",
    "wastewater nutrient loading river",
    "catchment nutrient budget mass balance",
    "SWAT nutrient load model",
    # Receiving seas
    "Sea of Marmara eutrophication",
    "Marmara river nutrient input",
    "Turkish river nutrient export",
    "Black Sea riverine nutrient input",
    "Mediterranean river nutrient inputs",
]

# Your rivers. Each one gets a nutrient query and a flow/catchment query.
# Turkish letters are written both ways (Göksu/Goksu, Deliçay/Delicay).
# Other names: Lamas is also called Limonlu; Asi is also called Orontes.
RIVER_QUERIES = [
    # Mersin Bay and Cilician Basin
    "Seyhan River nutrient",
    "Seyhan River catchment flow",
    "Ceyhan River nutrient",
    "Ceyhan River catchment flow",
    "Goksu River nutrient",
    "Göksu River catchment flow",
    "Berdan River nutrient",
    "Berdan River catchment flow",
    "Lamas River Mersin nutrient",
    "Limonlu River karst groundwater",
    "Anamur Mersin river nutrient",
    "Anamur River catchment flow",
    "Deliçay River Mersin nutrient",
    "Deliçay Tarsus groundwater nitrate",
    "Delicay Mersin river catchment",
    # Hatay (Iskenderun Bay)
    "Asi River nutrient",
    "Orontes River nutrient",
    # Susurluk basin (Marmara)
    "Susurluk Stream water quality",
    "Susurluk River nutrient",
    "Manyas Lake nutrient",
    "Uluabat Lake nutrient",
    "Ulubat Lake nutrient",
    "Nilüfer River nutrient",
    "Nilufer River nutrient",
    # Susurluk sub-basins
    "Karadere River nutrient",
    "Mustafakemalpaşa stream nutrient",
    "Çapraz stream nutrient",
]

# Turkish words. Many regional theses and Turkish journals are written in Turkish.
TURKISH_QUERIES = [
    "Seyhan Nehri besin tuzu",
    "Ceyhan Nehri besin tuzu",
    "Göksu Nehri besin tuzu",
    "Berdan Çayı besin",
    "Lamas Çayı yeraltısuyu",
    "Limonlu karst kaynak",
    "Deliçay yeraltısuyu nitrat",
    "Susurluk Çayı kirlilik",
    "Mersin yeraltısuyu nitrat",
    "karstik kaynak deniz altı yeraltısuyu boşalımı",
]

# Groundwater flow, karst and submarine groundwater discharge (SGD).
GROUNDWATER_QUERIES = [
    "submarine groundwater discharge Turkey",
    "karst submarine springs Turkey",
    "coastal karst groundwater Mediterranean",
    "submarine groundwater discharge Mediterranean nutrients",
    "radium submarine groundwater discharge Mediterranean",
    "groundwater flow Mediterranean coast",
    "coastal aquifer nitrate Mersin",
    "coastal aquifer nitrate Turkey Mediterranean",
    "Cilician Basin groundwater",
    "Cilician Basin nutrient",
    "Mersin Bay eutrophication",
    "Iskenderun Bay nutrient",
]

DEFAULT_QUERIES = TOPIC_QUERIES + RIVER_QUERIES + TURKISH_QUERIES + GROUNDWATER_QUERIES

# ---------------------------------------------------------------------------
# Relevance filter: a paper is kept if its title or abstract matches one rule.
#   1. It names one of your rivers, lakes or areas (RIVER_NAME_TERMS), or
#   2. It has a nutrient word AND a river/catchment/basin word or a region word, or
#   3. It has a groundwater/karst/SGD word AND a region, river or nutrient word.
#   Method terms such as "concentration-discharge" are kept on their own.
# Turn the filter off with --no-filter.
# ---------------------------------------------------------------------------

RIVER_NAME_TERMS = [
    r"seyhan", r"[cj]eyhan", r"g[oö]ksu", r"berdan", r"limonlu", r"orontes",
    r"susurluk", r"uluabat", r"ulubat", r"manyas", r"nil[uü]fer",
    r"karadere", r"mustafakemal", r"[cç]apraz",
]
# Names that are also ordinary words (lamas = "mud" in Portuguese) or town names.
# They count only when a local place word is in the same title or abstract.
AMBIGUOUS_NAME_TERMS = [r"(?:lamas|lemas)", r"deli[çcıi]ay", r"asi\b", r"anamur", r"tarsus"]
LOCAL_REGION_TERMS = [
    r"mersin", r"erdemli", r"silifke", r"cilician", r"mediterranean", r"akdeniz",
    r"kazanl", r"hatay", r"iskenderun", r"adana",
]
NUTRIENT_TERMS = [
    r"nutrient", r"nitrat", r"nitrogen", r"phosph", r"eutroph", r"solute",
    r"\bDIN\b", r"\bDIP\b", r"\bloads?\b", r"\bfluxes?\b", r"\bexport",
    r"besin", r"azot", r"fosfor",
]
SETTING_TERMS = [
    r"river", r"stream", r"catchment", r"watershed", r"\bbasin", r"\blakes?\b",
    r"reservoir", r"estuar", r"\bdelta", r"discharge", r"nehir", r"çay",
]
GROUNDWATER_TERMS = [r"groundwater", r"submarine", r"aquifer", r"karst", r"\bsprings?\b", r"yeralt"]
REGION_TERMS = [
    r"turkey", r"türkiye", r"turkish", r"mediterranean", r"cilician", r"mersin",
    r"adana", r"hatay", r"iskenderun", r"antalya", r"marmara", r"bursa",
    r"balıkesir", r"balikesir",
]


def _compile(terms: list[str]) -> re.Pattern:
    # Each term must start at the beginning of a word, so "asi" does not match "Asia".
    return re.compile(r"\b(?:" + "|".join(terms) + r")", re.IGNORECASE)


RIVER_RE = _compile(RIVER_NAME_TERMS)
AMBIGUOUS_RE = _compile(AMBIGUOUS_NAME_TERMS)
LOCAL_REGION_RE = _compile(LOCAL_REGION_TERMS)
NUTRIENT_RE = _compile(NUTRIENT_TERMS)
SETTING_RE = _compile(SETTING_TERMS)
GROUNDWATER_RE = _compile(GROUNDWATER_TERMS)
REGION_RE = _compile(REGION_TERMS)
# Method terms that are central to your thesis; a paper with one of these is kept on its own.
METHOD_RE = _compile([r"concentration[- ]discharge", r"hysteresis", r"chemostatic",
                      r"high-frequency", r"WRTDS", r"LOADEST"])
REVIEW_COMMENT_RE = re.compile(r"^(reply on|comment on|referee comment|review of)\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def session_with_retries() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def get_json(sess: requests.Session, url: str, *, params=None, headers=None, json_body=None,
             method="GET", min_interval: float = 1.05) -> Any:
    """HTTP JSON with 429 backoff. min_interval keeps Semantic Scholar at ~1 req/s."""
    time.sleep(min_interval)
    last_error = None
    for attempt in range(6):
        try:
            if method == "POST":
                resp = sess.post(url, params=params, headers=headers, json=json_body, timeout=60)
            else:
                resp = sess.get(url, params=params, headers=headers, timeout=60)
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429:
            try:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt + 2))
            except ValueError:
                try:
                    wait = max(0, (parsedate_to_datetime(resp.headers["Retry-After"]) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, KeyError):
                    wait = 2 ** attempt + 2
            wait = min(wait, 300)
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code} from {url.split('?')[0]}")
        data = resp.json()
        if RESPONSE_LOG:
            with open(RESPONSE_LOG, "a", encoding="utf-8") as log:
                log.write(json.dumps({"retrieved_at": datetime.now(timezone.utc).isoformat(),
                                      "endpoint": url.split("?")[0], "response": data}, ensure_ascii=False) + "\n")
        return data
    raise RuntimeError(f"Retries exhausted for {url.split('?')[0]} ({type(last_error).__name__})")


def clean_text(value: Any) -> str:
    """Remove HTML tags like <i> and extra whitespace."""
    if not value:
        return ""
    text = html.unescape(re.sub(r"</?[A-Za-z][^>]*>", " ", str(value)))
    return re.sub(r"\s+", " ", text).strip()


def to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def doi_link(doi: str | None) -> str:
    if not doi:
        return ""
    doi = doi.strip()
    if doi.lower().startswith("http"):
        return doi
    return f"https://doi.org/{doi}"


def norm_doi(value: str | None) -> str:
    if not value:
        return ""
    v = unquote(value.strip()).lower()
    v = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", v)
    return v.strip()


def title_key(title: str) -> str:
    """Title in lowercase without spaces/punctuation, so near-duplicates match."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", title).casefold())


def authors_to_text(authors: Any) -> str:
    if not authors:
        return ""
    names = []
    for a in authors:
        if isinstance(a, dict):
            names.append(a.get("name") or a.get("display_name") or "")
        else:
            names.append(str(a))
    return "; ".join(n for n in names if n)


def reconstruct_openalex_abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[i] = word
    if not positions:
        return ""
    return " ".join(positions[i] for i in range(max(positions) + 1) if i in positions)


def join_unique(a: str, b: str) -> str:
    out: list[str] = []
    for part in (a or "").split("|") + (b or "").split("|"):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return "|".join(out)


def merge_rows(existing: dict, incoming: dict) -> dict:
    out = dict(existing)
    for field in ("Title", "Authors", "Link", "Abstract", "Year", "DOI",
                  "Journal", "Volume", "Issue", "Pages", "OA_PDF", "Pdf_path", "Abstract_source", "DOI_match", "Document_type", "Institution", "Language", "Keywords", "Thesis_ID"):
        if not out.get(field) and incoming.get(field):
            out[field] = incoming[field]
    for field in ("Author_IDs", "ORCIDs", "Record_IDs", "Author_match", "Provenance", "Record_source_file", "Import_notes"):
        out[field] = join_unique(existing.get(field, ""), incoming.get(field, ""))
    if len((incoming.get("Authors") or "").split(";")) > len((existing.get("Authors") or "").split(";")):
        out["Authors"] = incoming["Authors"]
    out["Citations"] = max(to_int(existing.get("Citations")), to_int(incoming.get("Citations")))
    out["Source"] = join_unique(existing.get("Source", ""), incoming.get("Source", ""))
    out["Query"] = join_unique(existing.get("Query", ""), incoming.get("Query", ""))
    # Keep annotations and PDF audit columns from imported selections.
    for field, value in incoming.items():
        if not out.get(field) and value:
            out[field] = value
    return out


def add_row(merged: dict[str, dict], doi_index: dict[str, str], row: dict) -> None:
    """DOI identity first; title matching needs compatible year and first author.

    Different known DOIs are never collapsed, even with identical titles.
    """
    if not (row.get("Title") or row.get("DOI") or row.get("Record_IDs")):
        return
    row = dict(row)
    doi = norm_doi(row.get("DOI"))
    row["DOI"] = doi
    if row.get("Abstract") and not row.get("Abstract_source"):
        row["Abstract_source"] = row.get("Source", "")
    if doi and doi in doi_index:
        key = doi_index[doi]
    else:
        key = None
        candidates = []
        for candidate, old in merged.items():
            old_doi = norm_doi(old.get("DOI"))
            if doi and old_doi and doi != old_doi:
                continue
            if old.get("Thesis_ID") and row.get("Thesis_ID") and old["Thesis_ID"] != row["Thesis_ID"]:
                continue
            old_type, new_type = old.get("Document_type", ""), row.get("Document_type", "")
            if old_type and new_type and old_type != new_type:
                continue
            if ((old_type == "thesis" and new_type != "thesis" and row.get("Journal"))
                    or (new_type == "thesis" and old_type != "thesis" and old.get("Journal"))):
                continue
            old_ids = set(filter(None, old.get("Record_IDs", "").split("|")))
            new_ids = set(filter(None, row.get("Record_IDs", "").split("|")))
            if old_ids & new_ids:
                candidates.append(candidate)
                continue
            if not row.get("Title") or title_key(old.get("Title", "")) != title_key(row["Title"]):
                continue
            if not old.get("Year") or str(old["Year"]) != str(row.get("Year")):
                continue
            first_old = title_key(old.get("Authors", "").split(";")[0])
            first_new = title_key(row.get("Authors", "").split(";")[0])
            if first_old and first_old == first_new:
                candidates.append(candidate)
        if len(candidates) == 1:
            key = candidates[0]
        if key is None:
            key = "doi:" + doi if doi else "record:" + str(len(merged))
    merged[key] = merge_rows(merged[key], row) if key in merged else row
    final_doi = norm_doi(merged[key].get("DOI"))
    if final_doi:
        doi_index[final_doi] = key


# ---------------------------------------------------------------------------
# APA 7 reference
# ---------------------------------------------------------------------------

NAME_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da", "dos", "das",
                  "du", "la", "le", "el", "al", "bin", "ibn", "ter", "ten"}


def initials_from(given: str) -> str:
    out: list[str] = []
    for token in given.replace(",", " ").split():
        if re.fullmatch(r"(?:[^\W\d_]\.)+", token):          # already initials: "A." or "A.B."
            out.extend(re.findall(r"[^\W\d_]\.", token))
        elif "-" in token:                                    # Jean-Pierre -> J.-P.
            out.append("-".join(f"{p[0].upper()}." for p in token.split("-") if p))
        else:
            out.append(f"{token[0].upper()}.")
    return " ".join(out)


def split_name(full: str) -> tuple[str, str]:
    """'Korhan Özkan' -> ('Özkan', 'K.'). Also handles 'Özkan K.' and 'Özkan, Korhan'."""
    full = clean_text(full)
    if not full:
        return "", ""
    m = re.match(r"^(.+?)\s+((?:[^\W\d_]\.\s?)+)$", full)    # Scopus style "Ozkan K."
    if m:
        family, given = m.group(1), m.group(2)
    elif "," in full:
        family, given = [p.strip() for p in full.split(",", 1)]
    else:
        parts = full.split()
        if len(parts) == 1:
            return parts[0], ""
        i = len(parts) - 1
        while i > 0 and parts[i - 1].lower() in NAME_PARTICLES:
            i -= 1
        family, given = " ".join(parts[i:]), " ".join(parts[:i])
    return family, initials_from(given)


def format_authors_apa(authors: str) -> str:
    names = []
    for a in (x.strip() for x in (authors or "").split(";")):
        if not a:
            continue
        family, initials = split_name(a)
        if family:
            names.append(f"{family}, {initials}" if initials else family)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) <= 20:
        return ", ".join(names[:-1]) + ", & " + names[-1]
    return ", ".join(names[:19]) + ", . . . " + names[-1]       # APA 7: 21+ authors


def apa_reference(row: dict) -> str:
    authors = format_authors_apa(row.get("Authors") or "")
    year = str(row.get("Year") or "").strip()
    year = year[:4] if year[:4].isdigit() else "n.d."
    title = clean_text(row.get("Title"))
    if not title.endswith(("?", "!")):
        title = title.rstrip(".") + "."
    journal = clean_text(row.get("Journal"))
    volume = clean_text(row.get("Volume"))
    issue = clean_text(row.get("Issue"))
    pages = re.sub(r"(?<=\d)\s*[-‐‑‒]\s*(?=\d)", "–", clean_text(row.get("Pages")))

    venue = ""
    if journal:
        venue = journal
        if volume:
            venue += f", {volume}"
            if issue:
                venue += f"({issue})"
        if pages:
            venue += f", {pages}"
        venue += "."
    url = doi_link(row.get("DOI")) or (row.get("Link") or "")

    if authors:
        head = f"{authors if authors.endswith('.') else authors + '.'} ({year}). {title}"
    else:
        head = f"{title} ({year})."
    return " ".join(p for p in (head, venue, url) if p)


def apa_check(row: dict) -> str:
    """Tells you what to verify by hand for this reference."""
    issues = []
    if not row.get("Authors"):
        issues.append("no authors")
    if not clean_text(row.get("Journal")):
        issues.append("no journal")
    elif not clean_text(row.get("Volume")):
        issues.append("no volume")
    if not row.get("DOI"):
        issues.append("no DOI")
    if not str(row.get("Year") or "").strip():
        issues.append("no year")
    return "; ".join(issues)


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

def s2_headers() -> dict:
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    return {"x-api-key": key} if key else {}


def search_semantic_scholar(sess: requests.Session, query: str, limit: int,
                            year_from: int | None = None) -> list[dict]:
    """Relevance-ranked search (max 1000 results per query; 100 per request)."""
    headers = s2_headers()
    rows: list[dict] = []
    offset = 0
    q = query
    while len(rows) < limit and offset < 1000:
        n = min(100, limit - len(rows), 1000 - offset)
        params = {"query": q, "offset": offset, "limit": n, "fields": S2_FIELDS}
        if year_from:
            params["year"] = f"{year_from}-"
        data = get_json(sess, f"{S2_BASE}/paper/search", params=params, headers=headers)
        papers = data.get("data") or []
        if not papers:
            break
        for paper in papers:
            ext = paper.get("externalIds") or {}
            doi = ext.get("DOI") or ""
            oa = paper.get("openAccessPdf") or {}
            jr = paper.get("journal") or {}
            rows.append({
                "Title": clean_text(paper.get("title")),
                "Authors": authors_to_text(paper.get("authors")),
                "Year": paper.get("year") or "",
                "Journal": clean_text(jr.get("name") or paper.get("venue")),
                "Volume": clean_text(jr.get("volume")),
                "Issue": "",
                "Pages": clean_text(jr.get("pages")),
                "DOI": doi,
                "Link": doi_link(doi) or paper.get("url") or "",
                "Record_IDs": "S2:" + str(paper.get("paperId", "")),
                "Author_IDs": "|".join("S2:" + str(a["authorId"]) for a in paper.get("authors", []) if a.get("authorId")),
                "Citations": paper.get("citationCount") or 0,
                "Abstract": clean_text(paper.get("abstract")),
                "Source": "SemanticScholar",
                "Query": query,
            })
        offset += len(papers)
        if data.get("next") is None:
            break
    return rows


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

def search_openalex(sess: requests.Session, query: str, limit: int,
                    year_from: int | None = None) -> list[dict]:
    rows: list[dict] = []
    cursor = "*"
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    while len(rows) < limit and cursor:
        params = {
            "search": query,
            "per_page": min(200, limit - len(rows)),
            "cursor": cursor,
            "select": OPENALEX_SELECT,
        }
        if not query:
            params.pop("search", None)
        if year_from:
            params["filter"] = f"from_publication_date:{year_from}-01-01"
        if AUTHOR_FILTER:
            params["filter"] = ",".join(filter(None, [params.get("filter"), AUTHOR_FILTER]))
        if key:
            params["api_key"] = key
        data = get_json(sess, OPENALEX_WORKS, params=params, min_interval=0.2)
        results = data.get("results") or []
        if not results:
            break
        for work in results:
            loc = work.get("primary_location") or {}
            source = loc.get("source") or {}
            biblio = work.get("biblio") or {}
            first, last = biblio.get("first_page") or "", biblio.get("last_page") or ""
            pages = f"{first}-{last}" if first and last and first != last else first
            doi = (work.get("doi") or "").replace("https://doi.org/", "")
            rows.append({
                "Title": clean_text(work.get("display_name")),
                "Authors": authors_to_text([a.get("author") or {} for a in (work.get("authorships") or [])]),
                "Year": work.get("publication_year") or "",
                "Journal": clean_text(source.get("display_name")),
                "Volume": clean_text(biblio.get("volume")),
                "Issue": clean_text(biblio.get("issue")),
                "Pages": clean_text(pages),
                "DOI": doi,
                "Link": doi_link(doi) or loc.get("landing_page_url") or work.get("id") or "",
                "Record_IDs": work.get("id") or "",
                "Author_IDs": "|".join(a["author"]["id"] for a in work.get("authorships", []) if (a.get("author") or {}).get("id")),
                "ORCIDs": "|".join(a["author"]["orcid"] for a in work.get("authorships", []) if (a.get("author") or {}).get("orcid")),
                "Citations": work.get("cited_by_count") or 0,
                "Abstract": clean_text(reconstruct_openalex_abstract(work.get("abstract_inverted_index"))),
                "Source": "OpenAlex",
                "Query": query,
            })
            if len(rows) >= limit:
                break
        cursor = (data.get("meta") or {}).get("next_cursor")
    return rows


# ---------------------------------------------------------------------------
# Scopus (Elsevier)
# ---------------------------------------------------------------------------

def search_scopus(sess: requests.Session, query: str, limit: int,
                  year_from: int | None = None) -> list[dict]:
    key = os.environ.get("ELSEVIER_API_KEY", "").strip()
    if not key:
        print("Skipping Scopus: ELSEVIER_API_KEY is not set.", file=sys.stderr)
        return []
    headers = {"X-ELS-APIKey": key, "Accept": "application/json"}
    scopus_q = f"TITLE-ABS-KEY({query})"
    if year_from:
        scopus_q += f" AND PUBYEAR > {year_from - 1}"
    rows: list[dict] = []
    start = 0
    page_size = min(25, limit)
    while len(rows) < limit:
        params = {"query": scopus_q, "count": min(page_size, limit - len(rows)), "start": start}
        data = get_json(sess, SCOPUS_SEARCH, params=params, headers=headers, min_interval=0.3)
        results = data.get("search-results") or {}
        entries = results.get("entry") or []
        if not entries or "error" in (entries[0] if entries else {}):
            break
        for item in entries:
            doi = item.get("prism:doi") or ""
            scopus_url = ""
            for link in item.get("link") or []:
                if link.get("@ref") == "scopus":
                    scopus_url = link.get("@href") or ""
            rows.append({
                "Title": clean_text(item.get("dc:title")),
                "Authors": clean_text(item.get("dc:creator")),   # Scopus gives the first author only
                "Year": (item.get("prism:coverDate") or "")[:4],
                "Journal": clean_text(item.get("prism:publicationName")),
                "Volume": clean_text(item.get("prism:volume")),
                "Issue": clean_text(item.get("prism:issueIdentifier")),
                "Pages": clean_text(item.get("prism:pageRange")),
                "DOI": doi,
                "Link": doi_link(doi) or scopus_url,
                "Citations": item.get("citedby-count") or 0,
                "Abstract": "",
                "Source": "Scopus",
                "Query": query,
            })
            if len(rows) >= limit:
                break
        start += len(entries)
        total = int(results.get("opensearch:totalResults") or 0)
        if start >= total or start >= 5000:
            break
    return rows


# ---------------------------------------------------------------------------
# Crossref (free DOI / metadata) and Unpaywall (open-access PDF links only)
# ---------------------------------------------------------------------------

def crossref_mailto() -> str:
    return (
        os.environ.get("CROSSREF_MAILTO")
        or os.environ.get("UNPAYWALL_EMAIL")
        or ""
    ).strip()


def enrich_crossref(sess: requests.Session, rows: list[dict]) -> None:
    """Exact DOI lookup, or conservative title/year/author agreement; no guessed DOIs."""
    for row in rows:
        doi = norm_doi(row.get("DOI"))
        try:
            if doi:
                item = get_json(sess, CROSSREF_WORKS + "/" + quote(doi, safe=""), min_interval=0.2).get("message", {})
                if norm_doi(item.get("DOI")) != doi:
                    continue
                match = "exact DOI"
            else:
                data = get_json(sess, CROSSREF_WORKS, params={"query.bibliographic": row.get("Title", ""), "rows": 5}, min_interval=0.2)
                candidates = [i for i in data.get("message", {}).get("items", []) if crossref_agrees(row, i)]
                if len(candidates) != 1:
                    row["DOI_match"] = "unresolved: no unique title/year/author match"
                    continue
                item, match = candidates[0], "exact normalized title/year/first-author surname"
            metadata = crossref_row(item, "DOI enrichment")
            updated = merge_rows(row, metadata)
            updated["DOI_match"] = match
            row.update(updated)
        except Exception as exc:
            row["DOI_match"] = "lookup failed: " + type(exc).__name__
            AUDIT.append({"source": "crossref_enrichment", "status": "failed", "error_type": type(exc).__name__})


def _abstract_lookup(sess, provider, doi, links):
    """Return an abstract only when the response DOI agrees with the input DOI."""
    if provider == "OpenAlex":
        params = {}
        if os.environ.get("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        item = get_json(sess, "https://api.openalex.org/works/" + quote("https://doi.org/" + doi, safe=""), params=params)
        for loc in item.get("locations") or []:
            if loc.get("landing_page_url"):
                links.append(loc["landing_page_url"])
        if norm_doi(item.get("doi")) == doi:
            return clean_text(reconstruct_openalex_abstract(item.get("abstract_inverted_index")))
    elif provider == "SemanticScholar":
        item = get_json(sess, S2_BASE + "/paper/DOI:" + quote(doi, safe=""),
                        params={"fields": "title,abstract,externalIds"}, headers=s2_headers())
        if norm_doi((item.get("externalIds") or {}).get("DOI")) == doi:
            return clean_text(item.get("abstract"))
    elif provider == "EuropePMC":
        data = get_json(sess, "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                        params={"query": 'DOI:"' + doi + '"', "format": "json", "resultType": "core", "pageSize": 100})
        for item in data.get("resultList", {}).get("result", []):
            if norm_doi(item.get("doi")) == doi and item.get("abstractText"):
                return clean_text(item["abstractText"])
    elif provider == "Crossref":
        item = get_json(sess, CROSSREF_WORKS + "/" + quote(doi, safe=""), min_interval=1).get("message", {})
        link = ((item.get("resource") or {}).get("primary") or {}).get("URL")
        if link:
            links.append(link)
        if norm_doi(item.get("DOI")) == doi:
            return clean_text(item.get("abstract"))
    return ""


def _landing_abstract(sess, doi, links, allowed_hosts):
    """Read standard HTML metadata from explicitly enabled hosts, respecting robots."""
    from html.parser import HTMLParser
    from urllib.parse import urlsplit, urljoin
    from urllib.robotparser import RobotFileParser

    class Metadata(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = {}

        def handle_starttag(self, tag, attrs):
            if tag.lower() == "meta":
                attrs = dict(attrs)
                key = (attrs.get("name") or attrs.get("property") or "").lower()
                self.values.setdefault(key, []).append(attrs.get("content", ""))

    def permitted(url):
        parts = urlsplit(url)
        return parts.scheme == "https" and parts.hostname in allowed_hosts and not parts.username and not parts.password

    for original in dict.fromkeys(links):
        url = original
        for _ in range(4):
            if not permitted(url):
                break
            parts = urlsplit(url)
            robots_url = f"https://{parts.netloc}/robots.txt"
            time.sleep(1)
            robots = sess.get(robots_url, timeout=30, allow_redirects=False)
            if robots.status_code == 200:
                policy = RobotFileParser()
                policy.parse(robots.text.splitlines())
                if not policy.can_fetch(USER_AGENT, url):
                    break
                delay = policy.crawl_delay(USER_AGENT) or policy.crawl_delay("*") or 1
                if delay > 60:
                    break
                time.sleep(max(1, delay))
            elif robots.status_code != 404:
                break
            page = sess.get(url, timeout=30, allow_redirects=False, stream=True)
            try:
                if page.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, page.headers.get("Location", ""))
                    continue
                if page.status_code != 200 or "text/html" not in page.headers.get("Content-Type", "").lower():
                    break
                body = bytearray()
                for chunk in page.iter_content(16384):
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        break
                if len(body) > 2_000_000:
                    break
                metadata = Metadata()
                metadata.feed(body.decode(page.encoding or "utf-8", errors="replace"))
                ids = [v for key in ("citation_doi", "dc.identifier", "dc.identifier.doi", "prism.doi") for v in metadata.values.get(key, [])]
                if doi not in {norm_doi(v) for v in ids}:
                    break
                for key in ("citation_abstract", "dc.description", "dcterms.abstract"):
                    for value in metadata.values.get(key, []):
                        if clean_text(value):
                            return clean_text(value), url
                break
            finally:
                page.close()
    return "", ""


def enrich_abstracts(sess, rows, metadata_hosts=()):
    """Bounded, DOI-based fallbacks. Existing abstracts are never overwritten."""
    cache, recovered = {}, 0
    for index, row in enumerate(rows, 1):
        if clean_text(row.get("Abstract")):
            row["Abstract_check"] = "present"
            continue
        doi = norm_doi(row.get("DOI"))
        if not re.fullmatch(r"10\.\d{4,9}/\S+", doi) or '"' in doi:
            row["Abstract_check"] = "manual review: missing or invalid DOI"
            continue
        print(f"Abstract check {index}/{len(rows)}: {doi}", flush=True)
        if doi not in cache:
            abstract, source, link, errors = "", "", "", False
            links = [row["Link"]] if row.get("Link") else []
            for provider in ("OpenAlex", "SemanticScholar", "EuropePMC", "Crossref"):
                try:
                    abstract = _abstract_lookup(sess, provider, doi, links)
                    AUDIT.append({"source": provider, "operation": "abstract_lookup", "doi": doi,
                                  "status": "recovered" if abstract else "no_abstract_or_doi_mismatch"})
                except Exception as exc:
                    missing = isinstance(exc, RuntimeError) and str(exc).startswith("HTTP 404 ")
                    errors |= not missing
                    AUDIT.append({"source": provider, "operation": "abstract_lookup", "doi": doi,
                                  "status": "not_found" if missing else "failed", "error_type": type(exc).__name__})
                    continue
                if abstract:
                    source = provider
                    break
            if not abstract and metadata_hosts:
                try:
                    abstract, link = _landing_abstract(sess, doi, links, set(metadata_hosts))
                    source = "PublisherRepositoryMetadata" if abstract else ""
                    AUDIT.append({"source": "PublisherRepositoryMetadata", "operation": "abstract_lookup", "doi": doi,
                                  "status": "recovered" if abstract else "no_accessible_verified_metadata"})
                except Exception as exc:
                    errors = True
                    AUDIT.append({"source": "PublisherRepositoryMetadata", "operation": "abstract_lookup", "doi": doi,
                                  "status": "failed", "error_type": type(exc).__name__})
            check = "recovered" if abstract else "unresolved: lookup errors" if errors else "not found in checked sources"
            if not abstract and not metadata_hosts:
                check += "; publisher hosts not enabled"
            cache[doi] = abstract, source, link, check
        abstract, source, link, check = cache[doi]
        row["Abstract_check"] = check
        if abstract:
            row.update(Abstract=abstract, Abstract_source=source, Abstract_link=link or doi_link(doi))
            row["Source"] = join_unique(row.get("Source", ""), source)
            recovered += 1
    print(f"Abstract enrichment: filled {recovered}; {sum(not clean_text(r.get('Abstract')) for r in rows)} still missing.")


def unpaywall_email() -> str:
    return (os.environ.get("UNPAYWALL_EMAIL") or "").strip()


def lookup_unpaywall_pdf(sess: requests.Session, doi: str, email: str) -> str:
    data = get_json(
        sess,
        f"{UNPAYWALL_DOI}/{doi}",
        params={"email": email},
        min_interval=0.2,
    )
    best = data.get("best_oa_location") or {}
    pdf = best.get("url_for_pdf") or ""
    if pdf:
        return pdf
    for loc in data.get("oa_locations") or []:
        if loc.get("url_for_pdf"):
            return loc["url_for_pdf"]
    return best.get("url") or ""


def safe_pdf_name(row: dict, index: int) -> str:
    doi = norm_doi(row.get("DOI"))
    if doi:
        base = re.sub(r"[^\w.\-]+", "_", doi)
    else:
        base = re.sub(r"[^\w.\-]+", "_", (row.get("Title") or f"paper_{index}")[:80])
    return f"{base}.pdf"


def download_oa_pdfs(sess: requests.Session, rows: list[dict], pdf_dir: str,
                     *, mirrors=None, delay=2, max_mb=100, timeout=30) -> int:
    """Download and validate Unpaywall PDFs; report every unsuccessful row."""
    email = unpaywall_email()
    failed = saved = 0
    with SciHubDownloader(mirrors=mirrors, pdf_dir=pdf_dir, delay=delay,
                          max_mb=max_mb, timeout=timeout) as downloader:
        for i, row in enumerate(rows, start=1):
            doi = norm_doi(row.get("DOI"))
            # Clear stale results imported from an earlier attempt.
            for field in PDF_FIELDS:
                row[field] = ""
            row["PDF_provider"] = "Unpaywall"
            if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
                row.update(PDF_status="missing_or_invalid_doi", PDF_error="Missing or invalid DOI")
                failed += 1
                continue
            if not email or "@" not in email:
                row.update(PDF_status="not_downloaded", PDF_error="Set UNPAYWALL_EMAIL for Unpaywall lookup")
                failed += 1
                continue
            try:
                pdf_url = lookup_unpaywall_pdf(sess, doi, email)
                if not pdf_url:
                    raise RuntimeError("Unpaywall returned no PDF location")
                row["OA_PDF"] = pdf_url
                # A DOI hash avoids collisions after filename sanitization.
                suffix = hashlib.sha256(doi.encode()).hexdigest()[:12]
                dest = Path(pdf_dir).resolve() / (Path(safe_pdf_name(row, i)).stem[:100] + "-" + suffix + ".pdf")
                if dest.exists():
                    check = downloader.inspect(dest, doi)
                    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
                    result = {"PDF_status": "already_present", "PDF_URL": "", "PDF_provider": "Local cache",
                              "Pdf_path": str(dest), "PDF_DOI_check": check, "PDF_SHA256": digest}
                else:
                    result = downloader.save(pdf_url, dest, doi)
                if result.get("PDF_status") != "already_present":
                    result["PDF_provider"] = "Unpaywall"
                row.update(result)
                saved += 1
            except Exception as exc:
                row.update(PDF_status="not_downloaded", PDF_error=f"{type(exc).__name__}: {exc}", Pdf_path="")
                failed += 1
                print(f"  Unpaywall PDF failed for {doi}: {exc}", file=sys.stderr)
    print(f"Unpaywall: saved/reused {saved} PDFs; {failed} unavailable rows.")
    return failed


# ---------------------------------------------------------------------------
# Running everything
# ---------------------------------------------------------------------------

def crossref_row(item: dict, query: str) -> dict:
    parts = (item.get("published") or item.get("issued") or {}).get("date-parts", [[]])
    names = [a.get("name") or " ".join(filter(None, [a.get("given"), a.get("family")])) for a in item.get("author", [])]
    return {"Title": clean_text(next(iter(item.get("title") or []), "")),
            "Authors": "; ".join(names), "Year": parts[0][0] if parts and parts[0] else "",
            "Journal": clean_text(next(iter(item.get("container-title") or []), "")),
            "DOI": norm_doi(item.get("DOI")), "Abstract": clean_text(item.get("abstract")),
            "Abstract_source": "Crossref" if item.get("abstract") else "",
            "Volume": item.get("volume", ""), "Issue": item.get("issue", ""), "Pages": item.get("page", ""),
            "Link": doi_link(item.get("DOI")), "Citations": item.get("is-referenced-by-count", 0),
            "Source": "Crossref", "Query": query, "Record_IDs": "DOI:" + norm_doi(item.get("DOI")),
            "ORCIDs": "|".join(a["ORCID"] for a in item.get("author", []) if a.get("ORCID"))}


def crossref_agrees(row: dict, item: dict) -> bool:
    candidate = crossref_row(item, "")
    family = next(iter(item.get("author") or []), {}).get("family", "")
    # Missing year or author evidence deliberately leaves the DOI unresolved.
    return bool(row.get("Year") and family and row.get("Authors")
                and row.get("Title") and title_key(row.get("Title", "")) == title_key(candidate["Title"])
                and str(row["Year"]) == str(candidate["Year"])
                and re.search(r"(?<!\w)" + re.escape(family.casefold()) + r"(?!\w)", row["Authors"].split(";")[0].casefold()))


def search_crossref(sess: requests.Session, query: str, limit: int,
                    year_from: int | None = None) -> list[dict]:
    rows, cursor = [], "*"
    while len(rows) < limit:
        params = {"query.bibliographic": query, "rows": min(1000, limit - len(rows)), "cursor": cursor}
        if crossref_mailto():
            params["mailto"] = crossref_mailto()
        if year_from:
            params["filter"] = f"from-pub-date:{year_from}-01-01"
        msg = get_json(sess, CROSSREF_WORKS, params=params, min_interval=1).get("message", {})
        items = msg.get("items") or []
        if not items:
            break
        rows.extend(crossref_row(item, query) for item in items)
        nxt = msg.get("next-cursor")
        if not nxt or nxt == cursor or len(items) < params["rows"]:
            break
        cursor = nxt
    return rows


def search_europe_pmc(sess: requests.Session, query: str, limit: int,
                      year_from: int | None = None) -> list[dict]:
    """Europe PMC includes MEDLINE/PubMed records, PMC and other collections.

    This is not a separate direct PubMed integration.
    """
    rows, cursor = [], "*"
    q = f"({query})" + (f" AND FIRST_PDATE:[{year_from}-01-01 TO 3000-12-31]" if year_from else "")
    while len(rows) < limit:
        data = get_json(sess, "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                        params={"query": q, "format": "json", "resultType": "core",
                                "pageSize": min(1000, limit - len(rows)), "cursorMark": cursor}, min_interval=0.4)
        items = data.get("resultList", {}).get("result", [])
        if not items:
            break
        for item in items:
            authors = item.get("authorList", {}).get("author", [])
            ji = item.get("journalInfo") or {}
            rows.append({"Title": clean_text(item.get("title")),
                         "Authors": "; ".join(a.get("fullName", "") for a in authors) or item.get("authorString", ""),
                         "Year": item.get("pubYear", ""), "Journal": ji.get("journal", {}).get("title", ""),
                         "Volume": ji.get("volume", ""), "Issue": ji.get("issue", ""), "Pages": item.get("pageInfo", ""),
                         "DOI": norm_doi(item.get("doi")), "Abstract": clean_text(item.get("abstractText")),
                         "Link": doi_link(item.get("doi")) or f"https://europepmc.org/article/{item.get('source', '')}/{item.get('id', '')}",
                         "Source": "EuropePMC", "Query": query,
                         "Record_IDs": "EuropePMC:" + item.get("source", "") + ":" + item.get("id", "")})
        nxt = data.get("nextCursorMark")
        if not nxt or nxt == cursor or len(rows) >= int(data.get("hitCount", 0)):
            break
        cursor = nxt
    return rows


def resolve_openalex_author(sess: requests.Session, identifier: str) -> str:
    value = identifier.strip().rstrip("/").split("/")[-1]
    if re.fullmatch(r"A[0-9]+", value):
        target = value
    elif re.fullmatch(r"[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]", value):
        target = "https://orcid.org/" + value
    else:
        raise ValueError("Author identifier must be an OpenAlex A-number or ORCID")
    params = {"api_key": os.environ["OPENALEX_API_KEY"]} if os.environ.get("OPENALEX_API_KEY") else {}
    data = get_json(sess, "https://api.openalex.org/authors/" + quote(target, safe=""), params=params)
    resolved = data.get("id", "").rsplit("/", 1)[-1]
    if not re.fullmatch(r"A[0-9]+", resolved):
        raise ValueError("OpenAlex response did not identify an author")
    if target.startswith("https://orcid.org/") and (data.get("orcid") or "").rstrip("/") != target:
        raise ValueError("OpenAlex response ORCID does not match the selected identifier")
    if target.startswith("A") and resolved != target:
        raise ValueError("OpenAlex author ID was redirected; review the returned profile before selecting it")
    return resolved


def run_search(queries: list[str], sources: list[str], limit: int, year_from: int | None, turkish=None) -> list[dict]:
    sess = session_with_retries()
    collected = []
    dispatch = {
        "semantic_scholar": search_semantic_scholar,
        "openalex": search_openalex,
        "scopus": search_scopus,
        "crossref": search_crossref,
        "europe_pmc": search_europe_pmc,
    }
    for number, query in enumerate(queries, start=1):
        print(f"[{number}/{len(queries)}] Searching: {query}")
        for source in sources:
            print(f"  {source} ...")
            try:
                if source == "scopus" and not os.environ.get("ELSEVIER_API_KEY"):
                    AUDIT.append({"source": source, "query": query, "status": "skipped_missing_key"})
                    continue
                if source in TURKISH_SOURCES:
                    if turkish is None:
                        raise SourceUnavailable("No Turkish metadata retrieval configured")
                    found = turkish.search(sess, source, query, limit, year_from)
                else:
                    found = dispatch[source](sess, query, limit, year_from)
            except Exception as exc:
                AUDIT.append({"source": source, "query": query, "status": "unavailable" if isinstance(exc, SourceUnavailable) else "failed", "error": str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__})
                print(f"  {source} failed ({type(exc).__name__}); see audit", file=sys.stderr)
                continue
            if source in TURKISH_SOURCES:
                details = turkish.requests[(source, query)]
                AUDIT.append(dict(details, source=source, query=query))
                print(f"  {source}: {len(found)} rows ({details['status']}; {details['mode']})")
                collected.extend(found)
                continue
            cap = min(limit, 1000 if source == "semantic_scholar" else 5000 if source == "scopus" else limit)
            AUDIT.append({"source": source, "query": query, "status": "possible_truncation" if len(found) >= cap else "completed_below_cap",
                          "records": len(found), "effective_cap": cap})
            print(f"  {source}: {len(found)} rows")
            collected.extend(found)
    return collected


def is_relevant(title: str, abstract: str) -> bool:
    text = f"{title} {abstract}"
    if RIVER_RE.search(text) or METHOD_RE.search(text):
        return True
    if AMBIGUOUS_RE.search(text) and LOCAL_REGION_RE.search(text):
        return True
    if NUTRIENT_RE.search(text) and (SETTING_RE.search(text) or REGION_RE.search(text)):
        return True
    return bool(GROUNDWATER_RE.search(text)
                and (REGION_RE.search(text) or SETTING_RE.search(text) or NUTRIENT_RE.search(text)))


def filter_relevant(rows: list[dict]) -> list[dict]:
    return [
        r for r in rows
        if is_relevant(r.get("Title", ""), r.get("Abstract", ""))
        and not REVIEW_COMMENT_RE.match(r.get("Title", ""))
    ]


def finalize(rows: list[dict]) -> list[dict]:
    """Adds APA columns and sorts: papers found by more queries first, then most cited."""
    for r in rows:
        queries = {q.strip() for q in (r.get("Query") or "").split("|") if q.strip()}
        r["Queries_matched"] = len(queries)
        r["APA_reference"] = apa_reference(r)
        r["APA_check"] = apa_check(r)

    def year_value(r: dict) -> int:
        y = str(r.get("Year") or "")[:4]
        return int(y) if y.isdigit() else 0

    rows.sort(key=lambda r: (-r["Queries_matched"], -to_int(r.get("Citations")), -year_value(r)))
    return rows


FIELDNAMES = ["Title", "APA_reference", "APA_check", "Authors", "Year", "Journal", "Volume",
              "Issue", "Pages", "DOI", "Link", "OA_PDF", "Pdf_path", "Citations",
              "Queries_matched", "Source", "Query", "Abstract", "Abstract_source", "Record_IDs",
              "Author_IDs", "ORCIDs", "Author_match", "DOI_match", "Screening", "Metadata_status",
              "Document_type", "Institution", "Language", "Keywords", "Thesis_ID",
              "Provenance", "Record_source_file", "Import_notes", "Abstract_check", "Abstract_link"]
FIELDNAMES = list(dict.fromkeys(FIELDNAMES + PDF_FIELDS))


def one_line(value: Any) -> Any:
    """Turns line breaks and runs of spaces inside a cell into a single space."""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return value


READING_FIELDNAMES = ["Title", "APA_reference", "Authors", "Year", "Journal", "Volume",
                     "Issue", "Pages", "DOI", "Link", "Abstract", "Citations",
                     "Queries_matched", "Source", "Query", "Screening", "Metadata_status"]


def write_csv(path: str, rows: list[dict], fields=None) -> None:
    # utf-8-sig lets Excel show Turkish letters (ö, ş, ğ, ı) correctly.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        columns = list(dict.fromkeys((FIELDNAMES if fields is None else fields)
                                     + [key for row in rows for key in row]))
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: one_line(value) for key, value in row.items()})


def load_local_env() -> None:
    """Read literal KEY=value pairs beside this script; preserve exported values.

    No shell execution, variable expansion or multiline dotenv syntax is supported.
    """
    path = Path(__file__).with_name(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Search academic articles by keyword and export a CSV.")
    p.add_argument("queries", nargs="*", help="Keyword queries in quotes. If empty, DEFAULT_QUERIES is used.")
    p.add_argument("--max", type=int, default=100, help="Max papers per query per source (default 100).")
    p.add_argument("--out", default="articles.csv", help="Output CSV path.")
    p.add_argument("--sources", default="semantic_scholar,openalex,crossref,europe_pmc",
                   help="Comma list: semantic_scholar,openalex,scopus,crossref,europe_pmc,yok_tez,tr_dizin,dergipark. Turkish sources require an export or configured OAI endpoint.")
    p.add_argument("--year-from", type=int, default=None, help="Only papers from this year onward, e.g. 2005.")
    p.add_argument("--no-filter", action="store_true", help="Keep off-topic papers too (skip the relevance filter).")
    p.add_argument("--enrich-crossref", action="store_true",
                   help="Enrich exact DOIs or conservatively match title/year/author via Crossref.")
    p.add_argument("--enrich-abstracts", action="store_true", help="Fill missing abstracts through OpenAlex, Semantic Scholar, Europe PMC and Crossref before export.")
    p.add_argument("--abstract-metadata-host", action="append", default=[], metavar="HOST", help="Optionally enable publisher/repository HTML metadata lookup on this authorized hostname; repeatable.")
    p.add_argument("--download-pdfs", action="store_true",
                   help="Download PDFs for --from-csv through the selected provider (default Sci-Hub).")
    p.add_argument("--pdf-provider", choices=("scihub", "unpaywall", "both"), default="scihub",
                   help="Sci-Hub directly, Unpaywall only, or Unpaywall then Sci-Hub fallback.")
    p.add_argument("--pdf-dir", default="pdfs", help="Folder for PDFs (default: pdfs).")
    add_scihub_arguments(p)
    p.add_argument("--filter-relevant", action="store_true", help="Opt in to the legacy exclusion filter; all harvested records remain in sidecars.")
    p.add_argument("--author-id", action="append", default=[], help="Selected OpenAlex author ID or ORCID; repeat for multiple profiles. Retrieves works independently of keywords.")
    p.add_argument("--author-name", help="Find OpenAlex author candidates, without assuming they are the same person.")
    p.add_argument("--authors-only", action="store_true", help="Skip keyword searches; requires --author-id or --author-name.")
    p.add_argument("--import-csv", action="append", default=[], help="Import normalized CSV using this script's column names; repeatable.")
    p.add_argument("--imports-only", action="store_true", help="Merge --import-csv files without running network keyword searches.")
    p.add_argument("--from-csv", help="Process a reviewed CSV without searching (e.g. with --download-pdfs).")
    p.add_argument("--turkish-export", action="append", default=[], metavar="SOURCE=FILE", help="Search a supplied CSV/RIS export for a Turkish source; repeat for different sources.")
    p.add_argument("--turkish-oai", action="append", default=[], metavar="SOURCE=HTTPS_URL", help="Harvest and search an authorized OAI-PMH endpoint. No default portal endpoint is assumed.")
    p.add_argument("--turkish-oai-set", action="append", default=[], metavar="SOURCE=SET", help="Optional OAI set specifier for a configured source.")
    p.add_argument("--turkish-oai-from", help="OAI modification datestamp from YYYY-MM-DD; distinct from publication --year-from.")
    p.add_argument("--turkish-harvest-limit", type=int, default=1000, help="Maximum OAI records scanned per source, including deleted records (default 1000).")
    p.add_argument("--turkish-csv-delimiter", default=",", help="Delimiter for Turkish CSV export inputs (default comma).")
    p.add_argument("--turkish-plan", metavar="PATH", help="Write the 78-row Turkish checklist; alone, performs no searches.")
    p.add_argument("--turkish-import", action="append", default=[], metavar="SOURCE=FILE", help="Import Turkish CSV/RIS exports into the final CSV; no keyword searches unless queries are supplied.")
    args = p.parse_args()
    if not math.isfinite(args.scihub_delay) or args.scihub_delay < 0:
        p.error("--scihub-delay must be a finite non-negative number")
    if not math.isfinite(args.pdf_max_mb) or args.pdf_max_mb <= 0:
        p.error("--pdf-max-mb must be a finite positive number")
    if not math.isfinite(args.pdf_timeout) or args.pdf_timeout <= 0:
        p.error("--pdf-timeout must be a finite positive number")
    return args


def main() -> int:
    load_local_env()
    args = parse_args()
    if args.download_pdfs:
        try:
            # Validate mirror configuration before discovery or output writes.
            with SciHubDownloader(mirrors=args.scihub_mirror, pdf_dir=args.pdf_dir,
                                  delay=args.scihub_delay, max_mb=args.pdf_max_mb,
                                  timeout=args.pdf_timeout):
                pass
        except (ValueError, OSError) as exc:
            print(f"PDF configuration error: {exc}", file=sys.stderr)
            return 2
    if args.turkish_plan:
        try:
            count = write_plan(args.turkish_plan)
        except OSError as exc:
            print(f"Cannot write Turkish plan: {type(exc).__name__}", file=sys.stderr)
            return 2
        print(f"Saved {count} pending searches to {args.turkish_plan}; no portal search completed.")
        if not args.queries and not (args.turkish_import or args.import_csv or args.from_csv):
            return 0
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    allowed = {"semantic_scholar", "openalex", "scopus", "crossref", "europe_pmc"} | set(TURKISH_SOURCES)
    bad = [s for s in sources if s not in allowed]
    if bad:
        print(f"Unknown sources: {bad}. Use {sorted(allowed)}", file=sys.stderr)
        return 2
    if args.imports_only and not (args.import_csv or args.turkish_import):
        print("--imports-only requires at least one --import-csv", file=sys.stderr)
        return 2
    if args.max < 1:
        print("--max must be >= 1", file=sys.stderr)
        return 2
    if args.max > 1000 and "semantic_scholar" in sources:
        print("Note: Semantic Scholar returns at most 1000 results per query.")

    needs_turkish = bool(set(sources) & TURKISH_SOURCE_NAMES or args.turkish_export
                         or args.turkish_oai or args.turkish_oai_set or args.turkish_oai_from)
    if needs_turkish and not TURKISH_HELPER_AVAILABLE:
        print("Turkish-source searching requires turkish_literature.py in the same folder as "
              + Path(__file__).name + ". International searches do not require this helper.", file=sys.stderr)
        return 2
    turkish = None
    try:
        imported_bindings = parse_bindings(args.turkish_import)
        exports, endpoints, sets = ((parse_bindings(values) for values in (args.turkish_export, args.turkish_oai, args.turkish_oai_set))
                                    if TURKISH_HELPER_AVAILABLE else ({}, {}, {}))
        if (exports.keys() | endpoints.keys()) - set(sources):
            raise ValueError("Configured Turkish sources must also appear in --sources")
        if sets.keys() - endpoints.keys():
            raise ValueError("--turkish-oai-set requires a matching --turkish-oai endpoint")
        if TURKISH_HELPER_AVAILABLE:
            turkish = TurkishSearchClient(exports, endpoints, args.turkish_csv_delimiter,
                                          args.turkish_harvest_limit, sets, args.turkish_oai_from)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if ("openalex" in sources or args.author_name or args.author_id) and "@" in os.environ.get("OPENALEX_API_KEY", "") and not (args.from_csv or args.imports_only or (args.turkish_import and not args.queries)):
        print("OPENALEX_API_KEY contains an email address. Supply the API key from your OpenAlex account, or unset the variable to test keyless access where supported.", file=sys.stderr)
        return 2
    print(f"{Path(__file__).name} {SCRIPT_VERSION}")
    els_key = os.environ.get("ELSEVIER_API_KEY", "").strip()
    if "scopus" in sources:
        print(f"Elsevier/Scopus: {'key present' if els_key else 'SKIPPED (ELSEVIER_API_KEY not exported)'}")
    elif els_key:
        print("Elsevier key is set, but Scopus is off. Add scopus to --sources to use it.")
    else:
        print("Elsevier/Scopus: not used (no key, and scopus not in --sources).")

    global RESPONSE_LOG, AUTHOR_FILTER
    AUDIT.clear()
    if args.authors_only and not (args.author_id or args.author_name):
        print("--authors-only requires --author-id or --author-name", file=sys.stderr)
        return 2
    out = Path(args.out)
    if args.from_csv:
        selected_path = Path(args.from_csv).resolve()
        outputs = [out.resolve(), out.with_suffix(".audit.json").resolve()]
        outputs += [Path(str(out) + suffix).resolve() for suffix in (
            ".responses.jsonl", ".metadata.csv", ".harvested.csv", ".all.csv")]
        if selected_path in outputs:
            print("Choose a different --out path to preserve the selected input CSV.", file=sys.stderr)
            return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    RESPONSE_LOG = str(out) + ".responses.jsonl"
    selected_turkish = [source for source in sources if source in TURKISH_SOURCES]
    default_queries = CATALOGUE_QUERIES if selected_turkish and len(selected_turkish) == len(sources) else list(dict.fromkeys(DEFAULT_QUERIES + (CATALOGUE_QUERIES if selected_turkish else [])))
    queries = [] if args.from_csv or args.authors_only or args.imports_only or (args.turkish_import and not args.queries) else args.queries or default_queries
    if turkish is not None:
        turkish.raw_log = str(out) + ".turkish_responses.jsonl"
    if selected_turkish and queries:
        plan = Path(str(out) + ".turkish_searches.csv")
        outputs = [out] + [Path(str(out) + suffix) for suffix in (
            ".turkish_searches.csv", ".turkish_responses.jsonl", ".turkish_inventory.csv",
            ".responses.jsonl", ".harvested.csv", ".all.csv", ".audit.json")]
        if any(path.exists() for path in outputs):
            print("Output files already exist; choose a new --out path to preserve the previous search.", file=sys.stderr)
            return 2
        Path(turkish.raw_log).write_text("", encoding="utf-8")
        write_plan(plan, queries, selected_turkish)
    Path(RESPONSE_LOG).write_text("", encoding="utf-8")
    print(f"Running {len(queries)} queries on: {', '.join(sources)}")
    rows = run_search(queries, sources, args.max, args.year_from, turkish=turkish)
    if selected_turkish and queries:
        # Preserve loaded metadata even if matching or a later harvest page fails.
        write_csv(str(out) + ".turkish_inventory.csv", [row for inventory in turkish.cache.values() for row in inventory])
    sess = session_with_retries()
    if args.author_name and not (args.from_csv or args.imports_only):
        try:
            params = {"search": args.author_name, "per_page": 25}
            if os.environ.get("OPENALEX_API_KEY"):
                params["api_key"] = os.environ["OPENALEX_API_KEY"]
            candidates = get_json(sess, "https://api.openalex.org/authors", params=params)
            Path(str(out) + ".author_candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
            AUDIT.append({"source": "openalex_author_candidates", "status": "candidates_only", "note": "First 25 candidates; inspect profiles, ORCID, affiliations and known papers before selecting IDs."})
        except Exception as exc:
            AUDIT.append({"source": "openalex_author_candidates", "status": "failed", "error_type": type(exc).__name__})
    for identifier in ([] if args.from_csv or args.imports_only else args.author_id):
        try:
            author_id = resolve_openalex_author(sess, identifier)
            AUTHOR_FILTER = "authorships.author.id:" + author_id
            found = search_openalex(sess, "", args.max, args.year_from)
            for row in found:
                row["Query"] = "author:" + author_id
                row["Author_match"] = "selected OpenAlex profile:" + author_id
            rows.extend(found)
            AUDIT.append({"source": "openalex_author", "identifier": identifier, "records": len(found),
                          "status": "possible_truncation" if len(found) >= args.max else "completed_below_cap"})
        except Exception as exc:
            AUDIT.append({"source": "openalex_author", "identifier": identifier, "status": "failed", "error_type": type(exc).__name__})
        finally:
            AUTHOR_FILTER = ""
    for filename in args.import_csv + ([args.from_csv] if args.from_csv else []):
        try:
            if args.from_csv and filename == args.from_csv:
                _, imported = read_selected(filename)
            else:
                with open(filename, encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    if not reader.fieldnames or "Title" not in reader.fieldnames:
                        raise ValueError("Imported CSV requires a Title column and normalized metadata headers")
                    imported = list(reader)
            for row in imported:
                row["Source"] = row.get("Source") or "Imported:" + Path(filename).name
                rows.append(row)
        except (OSError, ValueError) as exc:
            print(f"CSV import failed: {exc}", file=sys.stderr)
            sess.close()
            return 2
    for source, filename in imported_bindings.items():
        try:
            imported_rows, raw = load_records(source, filename, args.turkish_csv_delimiter)
            rows.extend(imported_rows)
            for entry in raw:
                with open(RESPONSE_LOG, "a", encoding="utf-8") as log:
                    log.write(json.dumps(entry, ensure_ascii=False) + "\n")
            AUDIT.append({"source": source, "status": "imported", "file": filename, "records": len(imported_rows)})
        except (OSError, ValueError) as exc:
            AUDIT.append({"source": source, "status": "failed", "file": filename, "error_type": type(exc).__name__})
            print(f"Import failed for {source}: {type(exc).__name__}", file=sys.stderr)
    # Preserve all harvested records before enrichment, deduplication or screening.
    write_csv(str(out) + ".harvested.csv", rows)
    if args.enrich_crossref:
        enrich_crossref(sess, rows)
    if not args.from_csv:
        merged, doi_index = {}, {}
        for row in rows:
            add_row(merged, doi_index, row)
        rows = list(merged.values())
    # A reviewed selection may intentionally repeat a DOI with different notes.
    # Keep those rows intact; the shared downloader caches duplicate DOI jobs.
    if args.enrich_abstracts:
        enrich_abstracts(sess, rows, args.abstract_metadata_host)
    for row in rows:
        row["Screening"] = "candidate" if is_relevant(row.get("Title", ""), row.get("Abstract", "")) else "manual review"
        row["Metadata_status"] = "; ".join(field + " missing" for field in ("DOI", "Abstract", "Authors", "Year", "Journal") if not row.get(field)) or "core fields present"
    write_csv(str(out) + ".all.csv", finalize(list(rows)))
    if args.filter_relevant and not args.no_filter:
        rows = filter_relevant(rows)
    pdf_failed = 0
    if args.download_pdfs:
        if not args.from_csv:
            print("PDF downloads require --from-csv with your reviewed selection; discovery output saved without downloads.", file=sys.stderr)
            pdf_failed = 1
        else:
            settings = {"mirrors": args.scihub_mirror, "delay": args.scihub_delay,
                        "max_mb": args.pdf_max_mb, "timeout": args.pdf_timeout}
            if args.pdf_provider in {"unpaywall", "both"}:
                pdf_failed = download_oa_pdfs(sess, rows, args.pdf_dir, **settings)
            if args.pdf_provider in {"scihub", "both"}:
                pending = rows if args.pdf_provider == "scihub" else [
                    row for row in rows if row.get("PDF_status") not in
                    {"downloaded", "downloaded_needs_review", "already_present"}
                ]
                prior_errors = {id(row): row.get("PDF_error", "") for row in pending} if args.pdf_provider == "both" else {}
                pdf_failed = download_rows(pending, args.pdf_dir, **settings)
                for row in pending:
                    if prior_errors.get(id(row)):
                        current = row.get("PDF_error", "")
                        row["PDF_error"] = "[Unpaywall] " + prior_errors[id(row)] + ("; [SciHub] " + current if current else "")
            print(f"PDF processing completed; {pdf_failed} rows unavailable.")

    rows = finalize(rows)
    write_csv(str(out) + ".metadata.csv", rows)
    write_csv(args.out, rows, fields=list(dict.fromkeys(READING_FIELDNAMES + PDF_FIELDS)))
    print(f"Saved {len(rows)} article rows to {args.out}")
    report = {"version": SCRIPT_VERSION, "retrieved_at": datetime.now(timezone.utc).isoformat(),
              "queries": queries, "sources": sources, "max_per_query_source": args.max,
              "year_from": args.year_from, "filter_relevant": args.filter_relevant and not args.no_filter,
              "import_files": args.import_csv + ([args.from_csv] if args.from_csv else []) + list(imported_bindings.values()),
              "turkish_sources": turkish.details if turkish is not None else {},
              "exported": len(rows), "jobs": AUDIT,
              "pdf_download": {"requested": args.download_pdfs, "provider": args.pdf_provider,
                               "mirrors": args.scihub_mirror, "pdf_dir": args.pdf_dir,
                               "unavailable_rows": pdf_failed,
                               "rows": [{"DOI": row.get("DOI", ""), **{key: row.get(key, "") for key in PDF_FIELDS}}
                                        for row in rows] if args.download_pdfs else []},
              "limitations": ["Not an exhaustive search; completed_below_cap is not proof of database completeness.",
                              "Failed paginated jobs may retain only raw responses, not normalized rows.",
                              "Review possible truncations and split broad queries by year/topic."]}
    out.with_suffix(".audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = sum(job.get("status") in {"failed", "skipped_missing_key", "unavailable"} for job in AUDIT)
    print(f"Audit saved to {out.with_suffix('.audit.json')}; {failed} failed/skipped/unavailable jobs.")
    sess.close()
    return 1 if failed or pdf_failed else 0


if __name__ == "__main__":
    sys.exit(main())
