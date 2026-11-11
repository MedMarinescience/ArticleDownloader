import importlib.util
from pathlib import Path
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import turkish_literature as a

CARD = '''<div class="card article-card dp-card-outline"><h5 class="card-title"><a href="/tr/pub/test/article/123">Yeraltı Suyu</a></h5></div>'''
META = '''<meta name="citation_title" content="Yeraltı Suyu"><meta name="citation_author" content="Çağrı Şahin"><meta name="citation_author" content="Ali İnce"><meta name="citation_publication_date" content="2024-01-01"><meta name="citation_doi" content="https://doi.org/10.1234/test"><meta name="citation_journal_title" content="Dergi"><meta name="citation_abstract" content="Özet">'''

class Response:
    def __init__(self, url, text, status_code=200):
        self.url=url; self.text=text; self.status_code=status_code
    def raise_for_status(self):
        if self.status_code != 200: raise RuntimeError(self.status_code)
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self,*args): self.close()

class Session:
    def __init__(self, respond): self.respond=respond; self.urls=[]
    def get(self,url,**kwargs):
        self.urls.append(url)
        return self.respond(url)

class Tests(unittest.TestCase):
    def test_verification_response(self):
        html='<form name="search_verification"><div class="cf-turnstile"></div></form>'
        self.assertTrue(a.dergipark_gate(html, 'https://dergipark.org.tr/tr/search/verification'))
        s=Session(lambda url: Response(url, html))
        r=a.search_dergipark(s, 'yeraltı suyu',max_results=3)
        self.assertEqual(r['status'],'blocked')
        self.assertEqual(r['rows'],[])
        self.assertEqual(len(s.urls),1)
    def test_detail_metadata(self):
        html='<meta name="citation_title" content="Fixture article"><meta name="citation_author" content="Selim Armut"><meta name="citation_publication_date" content="2024">'
        r=a.dergipark_parse_detail(html,'https://dergipark.org.tr/tr/pub/itucis/article/1567744','su')
        self.assertEqual(r['Authors'],'Selim Armut')
        self.assertEqual(r['Year'],'2024')
        self.assertEqual(r['Source'],'DergiPark')
    def test_parse_card_and_pagination(self):
        html=CARD+'<ul class="pagination"><li><a href="?q=su&section=article&page=2">2</a></li></ul>'
        r=a.dergipark_parse_search(html,a.dergipark_search_url('su'),'su')
        self.assertEqual(len(r['rows']),1)
        self.assertEqual(r['rows'][0]['Record_IDs'],'DergiPark:123')
        self.assertIn('page=2',r['next_url'])
    def test_empty_vs_unrecognized(self):
        for html,expected in [('<p>Sonuç bulunamadı</p>','completed'),('<html>Something changed</html>','failed')]:
            with self.subTest(html=html):
                s=Session(lambda url:Response(url,html))
                self.assertEqual(a.search_dergipark(s,'su')['status'],expected)
    def test_caps_search_and_enriches(self):
        s=Session(lambda url:Response(url,META if '/article/' in url else CARD))
        r=a.search_dergipark(s,'su',max_results=1)
        self.assertEqual(r['status'],'completed')
        self.assertEqual(r['rows'][0]['Authors'],'Çağrı Şahin; Ali İnce')
        self.assertEqual(r['rows'][0]['DOI'],'10.1234/test')
    def test_detail_failure_keeps_hit(self):
        s=Session(lambda url:Response(url,'Oops' if '/article/' in url else CARD))
        r=a.search_dergipark(s,'su')
        self.assertEqual(r['status'],'completed')
        self.assertIn('Metadata_error',r['rows'][0])
    def test_browser_challenge_without_waiting(self):
        class Page:
            url='https://dergipark.org.tr/tr/search/verification'
            def goto(self,*args,**kwargs): return type('R',(),{'status':200})()
            def content(self):return '<form name="search_verification"></form>'
        r=a.search_dergipark_browser(Page(),None,'su',verification_timeout=0)
        self.assertEqual(r['status'],'blocked')

if __name__=='__main__': unittest.main()
