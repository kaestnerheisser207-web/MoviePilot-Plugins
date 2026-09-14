"""Sanitized CHDBits table structure observed during a real H3 download."""
import unittest
import test_hr_provider as generic
hr=generic.hr

HEADERS=['类型','标题','H&R百分比','剩余时间','H&R周期','做种时间','下载时间','上传','下载','分享率','完成时间']
def table(percent='0%',seeded='0:03',remaining='19天23:54:38',tid='123'):
    cells=['Movies',f'<a href="details.php?id={tid}&amp;hit=1">test movie</a>',percent,remaining,'3天',seeded,'4:56','0 KB','5 GB','0.000','<span title="2030-01-01 00:00:00">5分</span>']
    return '<table><tr>'+''.join(f'<td class="colhead">{h}</td>' for h in HEADERS)+'</tr><tr>'+''.join(f'<td>{c}</td>' for c in cells)+'</tr></table>'

class ChdParserTests(unittest.TestCase):
    def test_duration_short_clock_is_minutes_and_seconds(self):
        self.assertEqual(hr.chd_duration('0:03'),3)
        self.assertEqual(hr.chd_duration('4:56'),296)
        self.assertEqual(hr.chd_duration('19天23:54:38'),1727678)
        self.assertEqual(hr.chd_duration('3天'),259200)
    def test_bad_duration_is_rejected(self):
        for text in ('','unknown','1:99','1天24:00:00','-1天','12未知'):
            with self.subTest(text=text),self.assertRaises(hr.ProbeError):hr.chd_duration(text)
    def test_live_shape_pending_and_deadline(self):
        rows,_=hr.parse_chd_page(table(),'123',1000)
        r=rows[0]
        self.assertEqual(r.state,'incomplete');self.assertEqual(r.seeded_seconds,3)
        self.assertEqual(r.remaining_seed_seconds,259197)
        self.assertEqual(r.deadline_at,1728678)
    def test_expired_window_is_not_clearance(self):
        self.assertEqual(hr.parse_chd_page(table(remaining='0'),'123',1000)[0][0].state,'overdue')
    def test_rounded_percentage_does_not_clear_short_seed_time(self):
        self.assertEqual(hr.parse_chd_page(table(percent='100%',seeded='2天23:59:59'),'123',1000)[0][0].state,'incomplete')
    def test_complete_needs_both_site_percentage_and_site_seed_time(self):
        self.assertEqual(hr.parse_chd_page(table(percent='100%',seeded='3天'),'123',1000)[0][0].state,'complete')
        self.assertEqual(hr.parse_chd_page(table(percent='99%',seeded='3天'),'123',1000)[0][0].state,'incomplete')
    def test_wrong_torrent_and_login_are_not_clearance(self):
        self.assertEqual(hr.parse_chd_page(table(tid='456'),'123',1000)[0],[])
        with self.assertRaises(hr.ProbeError):hr.parse_chd_page('<input type="password">No Hit And Runs','123',1000)
    def test_invalid_percentage_is_rejected(self):
        for p in ('','100','101%','NaN%'):
            with self.subTest(p=p),self.assertRaises(hr.ProbeError):hr.parse_chd_page(table(percent=p),'123',1000)
    def test_h5_uses_the_record_requirement_not_site_default(self):
        html=table(seeded='3天').replace('<td>3天</td>','<td>5天</td>',1)
        result=hr.parse_chd_page(html,'123',1000)[0][0]
        self.assertEqual(result.required_seed_seconds,432000)
        self.assertEqual(result.state,'incomplete')
    def test_duplicate_conflicting_records_are_not_silently_deduplicated(self):
        done=table(percent='100%',seeded='3天')
        pending=table().split('</tr>',1)[1].removesuffix('</table>')
        result=hr.parse_chd_page(done.replace('</table>',pending+'</table>'),'123',1000)[0]
        self.assertEqual([x.state for x in result],['complete','incomplete'])
    def test_elapsed_calendar_time_does_not_clear_pending_record(self):
        result=hr.parse_chd_page(table(seeded='1:00:00'),'123',1000+5*86400)[0][0]
        self.assertEqual(result.state,'incomplete')
        self.assertEqual(result.remaining_seed_seconds,71*3600)

class ChdProviderTests(unittest.TestCase):
    setUp=generic.HrProviderTests.setUp
    tearDown=generic.HrProviderTests.tearDown
    def chd(self,html=None,extra=None,allowed=()):
        self.site.domain='ptchdbits.co';self.site.url='https://ptchdbits.co'
        self.pages={'https://ptchdbits.co/details.php?id=123':'<a href="hnr.php?id=99">H&amp;R:</a>',
                    'https://ptchdbits.co/hnr.php?id=99':html if html is not None else table()}
        self.pages.update(extra or {})
        return hr.NexusHr(allowed).check('https://ptchdbits.co/details.php?id=123')
    def test_chd_real_route_returns_pending(self):
        result=self.chd();self.assertEqual(result.state,'incomplete')
        self.assertEqual(result.remaining_seed_seconds,259197)
    def test_chd_absence_is_unknown_even_with_legacy_absence_allowlist(self):
        self.assertEqual(self.chd('No Hit And Runs',allowed=['ptchdbits.co']).state,'unknown')
    def test_chd_pagination_pending_overrides_complete(self):
        first=table(percent='100%',seeded='3天')+'<a href="?id=99&amp;page=2">2</a>'
        result=self.chd(first,{'https://ptchdbits.co/hnr.php?id=99&page=2':table()})
        self.assertEqual(result.state,'incomplete')
    def test_chd_different_profile_pagination_is_rejected(self):
        result=self.chd(table()+'<a href="?id=100&amp;page=2">2</a>')
        self.assertEqual(result.state,'unknown')
        self.assertFalse(any('id=100' in x for x in self.requests))
    def test_chd_missing_page_is_not_clearance(self):
        result=self.chd(table(percent='100%',seeded='3天')+'<a href="?id=99&amp;page=2">2</a>')
        self.assertEqual(result.state,'unknown')
    def test_hr_action_link_is_never_executed(self):
        result=self.chd(table(),{'https://ptchdbits.co/details.php?id=123':'<a href="hnr.php?id=99&amp;action=pardon">H&amp;R</a>'})
        self.assertEqual(result.state,'unknown')
        self.assertFalse(any('action=' in url for url in self.requests))

if __name__=='__main__':unittest.main()
