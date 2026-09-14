import contextlib
import importlib
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

ROOT=pathlib.Path(__file__).resolve().parents[3]
package=types.ModuleType('cloudbackupcleanup');package.__path__=[str(ROOT/'plugins.v3'/'cloudbackupcleanup')]
sys.modules.setdefault('cloudbackupcleanup',package)
hr=importlib.import_module('cloudbackupcleanup.hr')

HEAD='<tr><td class="colhead">HR编号</td><td class="colhead">种子名称</td><td class="colhead">还需做种时间</td></tr>'
NAV='<a href="myhr.php?status=done">已达标</a><a href="myhr.php?status=pending">未达标</a>'
EMPTY='<table>'+HEAD+'<tr><td>森马都没有找到</td></tr></table>'
ROW='<table>'+HEAD+'<tr><td>1</td><td><a href="details.php?id=123">电影</a></td><td>0</td></tr></table>'


class HrProviderTests(unittest.TestCase):
    def setUp(self):
        self.pages={
            'https://site.test/details.php?id=123':'<title>种子详情</title><a href="myhr.php">H&amp;R</a>',
            'https://site.test/myhr.php':EMPTY+NAV,
            'https://site.test/myhr.php?status=done':EMPTY,
            'https://site.test/myhr.php?status=pending':EMPTY,
        }
        self.requests=[]
        outer=self
        class Response:
            def __init__(self,raw):self.raw=raw
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return self.raw.encode()[:n]
        class Opener:
            def open(self,request,timeout):
                outer.requests.append(request.full_url)
                value=outer.pages[request.full_url]
                if isinstance(value,Exception):raise value
                return Response(value)
        site=types.SimpleNamespace(is_active=True,cookie='test-only-cookie',domain='site.test',url='https://site.test',proxy=False,ua='test')
        sites=types.ModuleType('app.db.oper.site');sites.SiteOper=lambda:types.SimpleNamespace(get_by_domain=lambda host:site)
        config=types.ModuleType('app.sdk.config');config.settings=types.SimpleNamespace(PROXY={})
        self.stack=contextlib.ExitStack()
        self.stack.enter_context(patch.dict(sys.modules,{'app.db.oper.site':sites,'app.sdk.config':config}))
        self.stack.enter_context(patch('urllib.request.build_opener',return_value=Opener()))
    def tearDown(self):self.stack.close()
    def check(self,allowed=(),limit=10):return hr.NexusHr(allowed,max_pages=limit).check('https://site.test/details.php?id=123')
    def test_absence_defaults_to_unknown(self):self.assertEqual(self.check().state,'unknown')
    def test_complete_page_confirms_clearance(self):
        self.pages['https://site.test/myhr.php?status=done']=ROW
        self.assertEqual(self.check().state,'complete')
    def test_pending_overrides_complete(self):
        self.pages['https://site.test/myhr.php?status=done']=ROW
        self.pages['https://site.test/myhr.php?status=pending']=ROW
        self.assertEqual(self.check().state,'incomplete')
    def test_missing_record_requires_explicit_site_policy(self):
        self.assertEqual(self.check(['site.test']).state,'no_hr')
    def test_incomplete_pagination_never_clears(self):
        self.pages['https://site.test/myhr.php?status=pending']=EMPTY+'<a href="myhr.php?page=2">下一页</a>'
        self.assertEqual(self.check(['site.test'],limit=3).state,'unknown')
    def test_numeric_page_link_is_read_before_clearance(self):
        self.pages['https://site.test/myhr.php?status=pending']=EMPTY+'<a href="myhr.php?status=pending&page=2">2</a>'
        self.pages['https://site.test/myhr.php?status=pending&page=2']=ROW
        self.assertEqual(self.check(['site.test']).state,'incomplete')
    def test_numeric_page_beyond_limit_is_unknown(self):
        self.pages['https://site.test/myhr.php?status=pending']=EMPTY+'<a href="myhr.php?status=pending&page=2">2</a>'
        self.pages['https://site.test/myhr.php?status=pending&page=2']=EMPTY
        self.assertEqual(self.check(['site.test'],limit=3).state,'unknown')
    def test_network_failure_is_not_no_hr(self):
        self.pages['https://site.test/myhr.php?status=done']=TimeoutError()
        self.assertEqual(self.check(['site.test']).state,'unknown')
    def test_foreign_origin_not_sent_cookie(self):
        result=hr.NexusHr().check('https://evil.test/details.php?id=123')
        self.assertEqual(result.state,'unknown');self.assertEqual(self.requests,[])
    def test_login_page_is_not_no_hr(self):
        self.pages['https://site.test/myhr.php']='<input type="password">'
        self.assertEqual(self.check(['site.test']).state,'unknown')
    def test_malformed_torrent_url_is_unknown(self):
        self.assertEqual(hr.NexusHr().check('https://[broken/details.php?id=123').state,'unknown')
        self.assertEqual(self.requests,[])
    def test_hr_record_id_is_not_torrent_id(self):
        page='<table>'+HEAD+'<tr><td><a href="myhr.php?id=123">123</a></td><td><a href="details.php?id=456">别的种子</a></td><td>0</td></tr></table>'
        self.assertFalse(hr.parse_page(page,'123')[0])
    def test_unrelated_all_navigation_is_not_an_hr_filter(self):
        self.pages['https://site.test/myhr.php'] += '<a href="https://other.test/all">全部</a>'
        self.assertEqual(self.check(['site.test']).state,'no_hr')
        self.assertFalse(any('other.test' in url for url in self.requests))


if __name__=='__main__':unittest.main()
