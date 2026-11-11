import importlib.util
from pathlib import Path
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import turkish_literature as tr


def hit(identifier, **overrides):
    source = {'id': identifier, 'docType': 'PAPER', 'language': 'TUR',
              'publicationYear': 2024, 'doi': 'https://doi.org/10.1234/TEST',
              'abstracts': [{'title': 'An English title', 'abstract': 'English', 'language': 'ENG'},
                            {'title': 'Türkçe yeraltı suyu', 'abstract': '<p>Özet &amp; bilgi</p>', 'language': 'TUR'}],
              'authors': [{'name': 'İkinci', 'order': 2}, {'name': 'Çağrı Şahin', 'order': 1}],
              'journal': {'name': 'Dergi'}, 'issue': {'year': '2024', 'number': '2', 'volume': '4'}}
    source.update(overrides)
    return {'_id': str(identifier), '_source': source}


class Response:
    def __init__(self, data=None, status=200, text=''):
        self.data, self.status_code, self.text = data, status, text
        self.url = 'https://search.trdizin.gov.tr/api/defaultSearch/publication/'
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def json(self):
        if self.data is None: raise ValueError('non JSON')
        return self.data


class Session:
    def __init__(self, pages): self.pages, self.calls = list(pages), []
    def get(self, url, **kw):
        self.calls.append((url, kw))
        if url.endswith('env.json'): return Response({'apiUrl': 'https://search.trdizin.gov.tr'})
        return self.pages.pop(0)


def page(hits, total, **kwargs):
    result = {'hits': {'total': {'value': total, 'relation': 'eq'}, 'hits': hits}}
    result.update(kwargs)
    return Response(result)


class Tests(unittest.TestCase):
    def test_metadata_schema_and_turkish_fields(self):
        row = tr.trdizin_row(hit(1), 'yeraltı suyu')
        self.assertEqual(row['Title'], 'Türkçe yeraltı suyu')
        self.assertEqual(row['Authors'], 'Çağrı Şahin; İkinci')
        self.assertEqual(row['Abstract'], 'Özet & bilgi')
        self.assertEqual(row['DOI'], '10.1234/test')
        self.assertEqual(row['Document_type'], 'article')
        self.assertEqual(tr.trdizin_row(hit(2, docType='PROJECT'), 'q')['Document_type'], 'project')

    def test_paginated_query_keeps_size_and_changes_page(self):
        session = Session([page([hit(i) for i in range(1, 21)], 25), page([hit(i) for i in range(21, 26)], 25)])
        result = tr.search_tr_dizin(session, 'yeraltı suyu', max_results=50)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['rows']), 25)
        self.assertEqual(result['pages_fetched'], 2)
        self.assertEqual([call[1]['params']['page'] for call in session.calls[1:]], [1, 2])
        self.assertEqual([call[1]['params']['limit'] for call in session.calls[1:]], [20, 20])
        self.assertEqual(session.calls[1][1]['params']['q'], 'yeraltı suyu')

    def test_result_and_page_caps(self):
        session = Session([page([hit(i) for i in range(20)], 50)])
        result = tr.search_tr_dizin(session, 'q', max_results=5)
        self.assertEqual(result['status'], 'capped')
        self.assertEqual(len(result['rows']), 5)
        session = Session([page([hit(i) for i in range(20)], 50)])
        self.assertEqual(tr.search_tr_dizin(session, 'q', max_pages=1)['status'], 'capped')

    def test_true_zero_is_distinct_from_malformed_or_access_failure(self):
        result = tr.search_tr_dizin(Session([page([], 0)]), 'q')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['total_reported'], 0)
        for response, expected in [(Response({}, 403), 'blocked'), (Response({}, 401), 'blocked'),
                                   (Response({}, 429), 'blocked'), (Response({'error': 'oops'}), 'failed'),
                                   (Response(None, text='<div>captcha</div>'), 'blocked')]:
            with self.subTest(expected=expected):
                result = tr.search_tr_dizin(Session([response]), 'q')
                self.assertEqual(result['status'], expected)
                self.assertTrue(result['error'])

    def test_partial_search_failure_keeps_earlier_rows(self):
        session = Session([page([hit(i) for i in range(20)], 25), Response({}, 500)])
        result = tr.search_tr_dizin(session, 'q')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['rows']), 20)
        self.assertEqual(result['pages_fetched'], 1)

    def test_repeated_page_and_empty_before_total_are_failures(self):
        session = Session([page([hit(i) for i in range(20)], 25), page([hit(i) for i in range(20)], 25)])
        self.assertEqual(tr.search_tr_dizin(session, 'q')['status'], 'failed')
        session = Session([page([], 25)])
        self.assertEqual(tr.search_tr_dizin(session, 'q')['status'], 'failed')

    def test_partial_shards_or_timeout_not_success(self):
        for extra in ({'_shards': {'failed': 1}}, {'timed_out': True}):
            self.assertEqual(tr.search_tr_dizin(Session([page([hit(1)], 1, **extra)]), 'q')['status'], 'failed')


if __name__ == '__main__': unittest.main()
