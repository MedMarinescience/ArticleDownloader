#!/usr/bin/env python3
"""Plan Turkish catalogue searches, normalize exports, optionally download DOI PDFs."""
import argparse
import csv
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from scihub_downloader import add_scihub_arguments, download_rows

SOURCES = {
    'yok_tez': {'name': 'YOKTez', 'url': 'https://tez.yok.gov.tr/UlusalTezMerkezi/',
                'kind': 'thesis catalogue', 'instruction': 'Use catalogue advanced search; search thesis title, abstract and keywords separately where available. Include all thesis access statuses; PDF permission is not a metadata inclusion criterion.'},
    'tr_dizin': {'name': 'TRDizin', 'url': 'https://search.trdizin.gov.tr/',
                 'kind': 'national index', 'instruction': 'Use the portal search fields and retain the exact executed expression. Review title, abstract and keyword matches; export metadata only through a supported interface.'},
    'dergipark': {'name': 'DergiPark', 'url': 'https://dergipark.org.tr/',
                  'kind': 'journal platform', 'instruction': 'Use article search and inspect journal-specific metadata. Do not assume a hosted journal is TR Dizin indexed; retain both source labels when found in both.'},
}

# Separate plain expressions avoid claiming the portals share a Boolean language.
QUERIES = [
    'submarine groundwater discharge', 'denizaltı yeraltısuyu boşalımı',
    'deniz altı yeraltı suyu boşalımı', 'yeraltısuyu deniz deşarjı',
    'kıyısal yeraltı suyu', 'kıyı akiferi', 'karstik kaynak',
    'yeraltı suyu besin tuzu', 'yeraltısuyu nitrat', 'groundwater nutrient flux',
    'subterranean estuary', 'radon yeraltı suyu', 'radyum kıyı',
    'Mersin yeraltı suyu', 'Göksu besin tuzu', 'Goksu nutrient',
    'Seyhan besin tuzu', 'Ceyhan besin tuzu', 'Berdan yeraltı suyu',
    'Lamas karst', 'Limonlu kaynak', 'Deliçay nitrat',
    'Susurluk besin tuzu', 'Marmara ötrofikasyon',
    'Akdeniz yeraltı suyu', 'İskenderun Körfezi besin',
]
PLAN_FIELDS = ['Search_ID', 'Source', 'Source_type', 'Portal', 'Planned_query',
               'Field_to_check', 'Status', 'Executed_query', 'Executed_at', 'Results_reported',
               'Records_exported', 'Export_file', 'Restrictions_or_notes', 'Instructions']


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
                writer.writerow({'Search_ID': f'{source}-{count:04d}', 'Source': info['name'],
                    'Source_type': info['kind'], 'Portal': info['url'], 'Planned_query': query,
                    'Field_to_check': 'title / abstract / keywords (as available)',
                    'Status': 'pending_manual_search', 'Instructions': info['instruction']})
    return count


def header_key(value):
    # Turkish dotted/dotless I should not prevent explicit field mapping.
    text = unicodedata.normalize('NFKD', value.replace('ı', 'i').casefold())
    return re.sub(r'[^a-z0-9]', '', ''.join(c for c in text if not unicodedata.combining(c)))


ALIASES = {
    'Title': ['Title', 'Başlık', 'Makale Adı', 'Tez Adı', 'Tez Başlığı', 'Eser Adı'],
    'Authors': ['Authors', 'Author', 'Yazar', 'Yazarlar', 'Yazar Adı'],
    'Year': ['Year', 'Yıl', 'Yayın Yılı', 'Tarih'],
    'Journal': ['Journal', 'Journal Name', 'Dergi', 'Dergi Adı'],
    'DOI': ['DOI', 'Digital Object Identifier'],
    'Link': ['Link', 'URL', 'Bağlantı', 'Erişim Adresi'],
    'Institution': ['Institution', 'University', 'Üniversite', 'Üniversite Adı', 'Kurum'],
    'Language': ['Language', 'Dil', 'Tez Dili', 'Yayın Dili'],
    'Keywords': ['Keywords', 'Anahtar Kelimeler', 'Anahtar Sözcükler'],
    'Thesis_ID': ['Thesis_ID', 'Tez No', 'Tez Numarası', 'Thesis Number'],
    'Document_type': ['Document_type', 'Publication Type', 'Yayın Türü', 'Tez Türü'],
    'Volume': ['Volume', 'Cilt'], 'Issue': ['Issue', 'Sayı'], 'Pages': ['Pages', 'Sayfa'],
    'Record_IDs': ['Record_IDs', 'Record ID', 'Kayıt No', 'Kayıt Numarası'],
    'ORCIDs': ['ORCIDs', 'ORCID'],
}
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
    row['Abstract'] = abstracts[0][1] if len(abstracts) == 1 else '\n\n'.join(f'[{label}] {text}' for label, text in abstracts)
    return row


def read_ris(path):
    """Strict tagged RIS with multiline fields; refuses silently truncated records."""
    record, last = {}, None
    with Path(path).open(encoding='utf-8-sig') as handle:
        for number, line in enumerate(handle, 1):
            line = line.rstrip('\r\n')
            match = re.match(r'^([A-Z0-9]{2})  - ?(.*)$', line)
            if match:
                tag, value = match.groups()
                if tag == 'TY':
                    if record:
                        raise ValueError(f'RIS missing ER before line {number}')
                    record, last = {'TY': [value]}, 'TY'
                elif tag == 'ER':
                    if not record:
                        raise ValueError(f'RIS ER without TY at line {number}')
                    yield record
                    record, last = {}, None
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
    year = re.search(r'\b(?:18|19|20|21)\d{2}\b', first('PY', 'Y1', 'DA'))
    first_page, last_page = first('SP'), first('EP')
    return {'Title': first('TI', 'T1'), 'Authors': '; '.join(record.get('AU') or record.get('A1') or []),
            'Year': year.group() if year else '', 'Journal': first('JO', 'JF', 'JA', 'T2') if kind != 'THES' else '',
            'Abstract': first('AB', 'N2'), 'DOI': first('DO'), 'Link': first('UR'),
            'Record_IDs': first('ID'), 'Language': first('LA'), 'Keywords': '; '.join(record.get('KW', [])),
            'Document_type': {'THES': 'thesis', 'JOUR': 'article', 'CONF': 'conference', 'CPAPER': 'conference'}.get(kind, kind.lower()),
            'Volume': first('VL'), 'Issue': first('IS'),
            'Pages': first_page + ('-' + last_page if last_page else '')}


def norm_doi(value):
    from urllib.parse import unquote
    text = unquote((value or '').strip()).lower()
    return re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', text).strip()


def write_csv(path, rows):
    # Standalone exports must not depend on the main script's filename.
    fields = list(dict.fromkeys(list(ALIASES) + ['Abstract', 'Source', 'Abstract_source',
        'Provenance', 'Record_source_file', 'Query', 'Import_notes'] +
        [key for row in rows for key in row]))
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
            if not reader.fieldnames or any(not h for h in reader.fieldnames) or len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise ValueError('CSV requires distinct nonempty column headers')
            records = [(raw, map_csv_record(raw)) for raw in reader]
    else:
        raise ValueError('Supported import formats: .csv and .ris')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rows, raw_entries = [], []
    for index, (raw, row) in enumerate(records, 1):
        if not any(row.get(k) for k in ('Title', 'DOI', 'Record_IDs', 'Thesis_ID')):
            raise ValueError(f'Record {index} has no mapped title or identifier; check field mapping')
        row['DOI'] = norm_doi(row.get('DOI'))
        row['Source'] = info['name']
        row['Record_source_file'] = path.name
        row['Provenance'] = f'{info["name"]}; sha256:{digest}; record:{index}'
        row['Query'] = 'authorized export import (search expression recorded in search plan)'
        if source == 'yok_tez':
            row['Document_type'] = 'thesis'
        elif not row.get('Document_type'):
            row['Document_type'] = 'article'
        if row.get('Thesis_ID'):
            row['Record_IDs'] = info['name'] + ':thesis:' + row['Thesis_ID']
        elif row.get('Record_IDs'):
            # This importer owns the namespace; ID is an export identifier, not DOI evidence.
            row['Record_IDs'] = info['name'] + ':' + row['Record_IDs']
        if row.get('Abstract'):
            row['Abstract_source'] = info['name']
        row['Import_notes'] = 'Source label supplied by operator; native portal export schema and authenticity not independently verified.'
        raw_entries.append({'source': info['name'], 'input_sha256': digest, 'record': index, 'fields': raw,
                            'imported_at': datetime.now(timezone.utc).isoformat()})
        rows.append(row)
    return rows, raw_entries


def import_records(source, input_path, output_path, delimiter=','):
    output = Path(output_path)
    if output.exists() or Path(str(output) + '.import.jsonl').exists():
        raise FileExistsError('Choose a new output path to preserve existing imports')
    rows, raw_entries = load_records(source, input_path, delimiter)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(str(output), rows)
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
        if not separator or source not in allowed or not value.strip():
            raise ValueError('Expected SOURCE=VALUE with source yok_tez, tr_dizin or dergipark')
        if source in result:
            raise ValueError('Duplicate configuration for ' + source)
        result[source] = value.strip()
    return result


def search_terms(text):
    text = unicodedata.normalize('NFKD', text.replace('ı', 'i').casefold())
    return set(re.findall(r'[^\W_]+', ''.join(c for c in text if not unicodedata.combining(c))))


def parse_oai_xml(content):
    import xml.etree.ElementTree as ET
    if len(content) > 10 * 1024 * 1024 or re.search(br'<!\s*(?:DOCTYPE|ENTITY)', content, re.I):
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
    oai, dc = '{http://www.openarchives.org/OAI/2.0/}', '{http://purl.org/dc/elements/1.1/}'
    header = element.find(oai + 'header')
    if header is None or header.get('status') == 'deleted':
        return None
    metadata = element.find(oai + 'metadata')
    if metadata is None:
        return None
    def fields(name):
        return [' '.join(node.itertext()).strip() for node in metadata.iter(dc + name) if ''.join(node.itertext()).strip()]
    identifiers, dates = fields('identifier'), fields('date')
    doi = next((match.group(1) for value in identifiers
                if (match := re.fullmatch(r'(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/\S+)', value, re.I))), '')
    year = next((match.group() for value in dates if (match := re.search(r'\b(?:18|19|20|21)\d{2}\b', value))), '')
    types = fields('type')
    row = {'Title': ' / '.join(fields('title')), 'Authors': '; '.join(fields('creator')),
           'Abstract': '\n\n'.join(fields('description')), 'Year': year,
           'DOI': doi, 'Link': next((v for v in identifiers if v.startswith(('https://', 'http://'))), ''),
           'Record_IDs': SOURCES[source]['name'] + ':' + (header.findtext(oai + 'identifier') or ''),
           'Keywords': '; '.join(fields('subject')), 'Language': '; '.join(fields('language')),
           'Source': SOURCES[source]['name'],
           'Document_type': 'thesis' if source == 'yok_tez' else 'article' if source == 'dergipark' else '; '.join(types),
           'Abstract_source': SOURCES[source]['name'] + ':dc.description' if fields('description') else '',
           'Import_notes': 'OAI Dublin Core descriptions may include notes rather than abstracts. Publication year inferred from dc:date; verify its meaning.'}
    return row if row['Title'] or doi else None


class TurkishSearchClient:
    """Search configured exports or OAI-PMH metadata once per source per run.

    OAI-PMH provides harvesting, not keyword search; result coverage is bounded by
    the harvest budget, selected set and date range. No portal endpoint is assumed.
    """
    def __init__(self, exports=None, endpoints=None, delimiter=',', harvest_limit=1000,
                 sets=None, since=None, raw_log=None):
        from urllib.parse import urlsplit
        self.exports, self.endpoints = exports or {}, endpoints or {}
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
        self.delimiter, self.harvest_limit = delimiter, harvest_limit
        self.sets, self.since, self.raw_log = sets or {}, since, raw_log
        self.cache, self.details, self.failures = {}, {}, {}
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
        self._log({'source': source, 'endpoint': self.endpoints[source], 'params': params,
                   'retrieved_at': datetime.now(timezone.utc).isoformat(),
                   'xml': response.content.decode('utf-8', errors='replace')})
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
                self.details[source] = {'mode': 'export_search', 'inventory_records': len(rows),
                    'scope': 'supplied export only; catalogue coverage unknown', 'harvest_truncated': False}
            elif source in self.endpoints:
                self.details[source] = {'mode': 'oai_harvest_search', 'harvest_limit': self.harvest_limit,
                    'records_scanned': 0, 'harvest_truncated': False,
                    'scope': 'configured endpoint/set/date only; not complete catalogue coverage'}
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
        if re.search(r'\b(?:AND|OR|NOT)\b', query):
            raise ValueError('Turkish metadata search accepts plain words; use separate queries instead of Boolean operators')
        self._load(sess, source)
        terms = search_terms(query)
        matches = []
        for row in self.cache[source]:
            year = re.search(r'\b\d{4}\b', str(row.get('Year', '')))
            if year_from and year and int(year.group()) < year_from:
                continue
            text = ' '.join(row.get(field, '') for field in ('Title', 'Abstract', 'Keywords', 'Authors'))
            if terms <= search_terms(text):
                matches.append(dict(row, Query=query))
        details = dict(self.details[source], matching_records=len(matches), returned=min(limit, len(matches)),
                       match_rule='all normalized query tokens across title/abstract/keywords/authors; no stemming')
        details['status'] = 'possible_truncation' if len(matches) > limit or details['harvest_truncated'] else 'completed_local_scope'
        self.requests[(source, query)] = details
        return matches[:limit]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan', help='Write a pending search checklist; does not execute searches')
    plan.add_argument('--out', required=True)
    plan.add_argument('--query', action='append', help='Repeat to replace the default Turkish/English expressions')
    imp = commands.add_parser('import', help='Normalize CSV/RIS exports; optional direct Sci-Hub PDF lookup')
    imp.add_argument('--source', choices=SOURCES, required=True)
    imp.add_argument('--input', required=True)
    imp.add_argument('--out', required=True)
    imp.add_argument('--delimiter', default=',', help='CSV delimiter; use ; for semicolon exports')
    imp.add_argument('--download-pdfs', action='store_true',
                     help='Look up imported article DOIs directly on Sci-Hub; missing DOIs are reported')
    imp.add_argument('--pdf-dir', default='selected_pdfs')
    add_scihub_arguments(imp)
    args = parser.parse_args()
    try:
        if args.command == 'plan':
            count = write_plan(args.out, args.query)
            print(f'Saved {count} PENDING searches; no portal searches executed: {args.out}')
        else:
            if len(args.delimiter) != 1:
                parser.error('--delimiter must be one character')
            if args.download_pdfs:
                output = Path(args.out)
                raw_log = Path(str(output) + '.import.jsonl')
                if output.exists() or raw_log.exists():
                    raise FileExistsError('Choose a new output path to preserve existing imports')
                rows, raw_entries = load_records(args.source, args.input, args.delimiter)
                failed = download_rows(rows, args.pdf_dir, mirrors=args.scihub_mirror,
                                       delay=args.scihub_delay, max_mb=args.pdf_max_mb,
                                       timeout=args.pdf_timeout)
                output.parent.mkdir(parents=True, exist_ok=True)
                write_csv(output, rows)
                with raw_log.open('x', encoding='utf-8') as log:
                    for entry in raw_entries:
                        log.write(json.dumps(entry, ensure_ascii=False) + '\n')
                count = len(rows)
                print(f'Normalized {count} supplied records: {args.out}; {failed} PDFs unavailable')
                return 1 if failed else 0
            count = import_records(args.source, args.input, args.out, args.delimiter)
            print(f'Normalized {count} supplied records: {args.out}')
    except (ValueError, OSError) as exc:
        parser.exit(2, f'{type(exc).__name__}: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
