"""HDHome's observed columns/views mapped through the shared upstream parser."""
import unittest
import test_hr_provider as generic
hr=generic.hr
HEADERS=['种子编号','种子名称','上传量','下载量','分享率','还需做种时间','完成时间','剩余达标时间']
NAV='<a href="?hrtype=1">考核中</a><a href="?hrtype=0">已达标</a><a href="?hrtype=2">未达标</a>'

def page(ident=None,remaining='13天23:54:38',window='59天00:00:00'):
    head='<tr>'+''.join('<td class="colhead">'+h+'</td>' for h in HEADERS)+'</tr>'
    row='<tr><td colspan="8">NA</td></tr>' if ident is None else '<tr>'+''.join('<td>'+x+'</td>' for x in [ident,f'<a href="details.php?id={ident}">Test movie</a>','0','5 GB','0',remaining,'2030-01-01',window])+'</tr>'
    return '<table>'+head+row+'</table>'

class HdhomeTests(unittest.TestCase):
    setUp=generic.HrProviderTests.setUp
    tearDown=generic.HrProviderTests.tearDown
    def check_home(self,pending=None,done=None,failed=None,nav=NAV,extra=None):
        self.site.domain='hdhome.org';self.site.url='https://hdhome.org'
        self.pages={'https://hdhome.org/details.php?id=123':'<a href="myhr.php">我的HR</a>',
                    'https://hdhome.org/myhr.php':page()+nav,
                    'https://hdhome.org/myhr.php?hrtype=1':pending or page(),
                    'https://hdhome.org/myhr.php?hrtype=0':done or page(),
                    'https://hdhome.org/myhr.php?hrtype=2':failed or page()}
        self.pages.update(extra or {})
        return hr.NexusHr().check('https://hdhome.org/details.php?id=123')
    def test_pending_uses_station_time(self):
        result=self.check_home(pending=page('123'))
        self.assertEqual(result.state,'incomplete')
        self.assertEqual(result.remaining_seed_seconds,1209278)
        self.assertIn('站点倒计时',result.deadline_text)
    def test_complete_view_clears_even_with_no_duration(self):
        result=self.check_home(done=page('123',remaining='---',window='---'))
        self.assertEqual(result.state,'complete');self.assertEqual(result.basis,'site_personal_view')
    def test_unreached_view_is_overdue_not_complete(self):
        self.assertEqual(self.check_home(failed=page('123',window='-1天')).state,'overdue')
    def test_pending_conflict_overrides_completed_record(self):
        self.assertEqual(self.check_home(pending=page('123'),done=page('123')).state,'incomplete')
    def test_expired_pending_is_not_cleared(self):
        self.assertEqual(self.check_home(pending=page('123',window='0')).state,'overdue')
    def test_empty_personal_list_is_unknown(self):
        self.assertEqual(self.check_home().state,'unknown')
    def test_other_torrent_cannot_clear_this_one(self):
        self.assertEqual(self.check_home(done=page('456')).state,'unknown')
    def test_missing_view_is_unknown_even_with_complete_record(self):
        self.assertEqual(self.check_home(done=page('123'),nav='<a href="?hrtype=0">已达标</a>').state,'unknown')
    def test_missing_page_blocks_complete(self):
        result=self.check_home(done=page('123')+'<a href="?hrtype=0&amp;page=2">2</a>')
        self.assertEqual(result.state,'unknown')
    def test_pagination_preserves_pending_view(self):
        result=self.check_home(pending=page()+'<a href="?hrtype=1&amp;page=2">2</a>',done=page('123'),
            extra={'https://hdhome.org/myhr.php?hrtype=1&page=2':page('123')})
        self.assertEqual(result.state,'incomplete')
    def test_bad_row_shape_cannot_clear(self):
        bad=page('123').replace('<td>5 GB</td>','')
        self.assertEqual(self.check_home(done=bad).state,'unknown')
    def test_login_failure_keeps_unknown(self):
        self.assertEqual(self.check_home(done='<input type="password">').state,'unknown')
    def test_userid_pagination_is_bound_to_the_authenticated_page(self):
        result=self.check_home(done=page('123')+'<a href="?userid=99&amp;hrtype=0&amp;page=1">下一页</a>',
            extra={'https://hdhome.org/myhr.php?userid=99&hrtype=0&page=1':page()})
        self.assertEqual(result.state,'complete')
    def test_userid_cannot_change_on_later_pages(self):
        result=self.check_home(done=page('123')+'<a href="?userid=99&amp;hrtype=0&amp;page=1">下一页</a>',
            extra={'https://hdhome.org/myhr.php?userid=99&hrtype=0&page=1':page()+'<a href="?userid=100&amp;hrtype=0&amp;page=2">下一页</a>'})
        self.assertEqual(result.state,'unknown')
        self.assertFalse(any('userid=100' in url for url in self.requests))

if __name__=='__main__':unittest.main()
