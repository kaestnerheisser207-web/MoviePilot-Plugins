import types,unittest,threading
from unittest.mock import patch
from test_lifecycle import load_entry
from cloudbackupcleanup.calculation import calculate
from cloudbackupcleanup.hr import HrResult

RULE={'site_name':'观众','hr_duration':48,'additional_seed_time':24,'hr_ratio':99}
class CalculationTests(unittest.TestCase):
    def task(self,seconds=3600,ratio=1,downloaded=100):return types.SimpleNamespace(seed_seconds=seconds,ratio=ratio,downloaded_bytes=downloaded,generation=123)
    def test_uses_actual_counter_not_wall_clock(self):
        r=calculate(self.task(),RULE,'观众','no record')
        self.assertEqual(r.state,'incomplete');self.assertEqual(r.seeded_seconds,3600)
    def test_paused_time_does_not_reduce_remaining(self):
        with patch('cloudbackupcleanup.calculation.time.time',return_value=1000):a=calculate(self.task(),RULE,'观众','')
        with patch('cloudbackupcleanup.calculation.time.time',return_value=999999):b=calculate(self.task(),RULE,'观众','')
        self.assertEqual(a.remaining_seed_seconds,b.remaining_seed_seconds)
    def test_counter_or_valid_ratio_can_clear(self):
        self.assertEqual(calculate(self.task(72*3600+1),RULE,'观众','').state,'complete')
        self.assertEqual(calculate(self.task(ratio=100),RULE,'观众','').state,'complete')
        self.assertEqual(calculate(self.task(72*3600,99),RULE,'观众','').state,'incomplete')
    def test_zero_download_auxiliary_cannot_clear_by_ratio(self):
        self.assertEqual(calculate(self.task(ratio=1000,downloaded=0),RULE,'观众','').state,'incomplete')
    def test_invalid_rules_and_counters_do_not_clear(self):
        for value in (float('nan'),float('inf'),-1,True):
            self.assertEqual(calculate(self.task(),{**RULE,'hr_duration':value},'观众','').state,'unknown')
        self.assertEqual(calculate(self.task(None,None),RULE,'观众','').state,'unknown')
    def test_counter_reset_never_reuses_old_high_value(self):
        self.assertEqual(calculate(self.task(300000),RULE,'观众','').state,'complete')
        self.assertEqual(calculate(self.task(0),RULE,'观众','').state,'incomplete')

class CalculationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.entry=load_entry();self.p=self.entry.CloudBackupCleanup();self.p.init_plugin({'hr_calculation':True,'sites':[2]})
        self.task=types.SimpleNamespace(client='tr',hash='a'*40,generation=123,seed_seconds=300000,ratio=1,downloaded_bytes=100)
    def test_explicit_station_status_is_not_overridden(self):
        for state in ('incomplete','overdue','complete','no_hr'):
            r=HrResult(state,'site',1)
            with patch.object(self.p,'_calculation_rule') as rules:
                self.assertIs(self.p._hr_check(self.task,'url',types.SimpleNamespace(check=lambda u:r)),r);rules.assert_not_called()
    def test_unknown_station_uses_rules_only_when_enabled(self):
        with patch.object(self.p,'_calculation_rule',return_value=(RULE,'观众')):
            r=self.p._hr_check(self.task,'url',types.SimpleNamespace(check=lambda u:HrResult('unknown','no record',1)))
        self.assertEqual(r.state,'complete');self.assertEqual(r.basis,'downloader_rule_calculation');self.assertEqual(r.station_reason,'no record')
        self.p._config['hr_calculation']=False
        self.assertEqual(self.p._hr_check(self.task,'url',types.SimpleNamespace(check=lambda u:HrResult('unknown','no record',1))).state,'unknown')
    def test_unselected_and_disabled_sites_cannot_calculate(self):
        for site in ({'value':4,'title':'观众','domain':'audiences.me','active':True},{'value':2,'title':'观众','domain':'audiences.me','active':False}):
            with patch.object(self.p,'_site_options',return_value=[site]):self.assertIsNone(self.p._calculation_rule('https://audiences.me/details.php?id=1'))
    def plan(self):
        from cloudbackupcleanup.engine import hr_clearance
        url='https://audiences.me/details.php?id=1';o={'client':'tr','hash':'a'*40,'generation':123}
        record=hr_clearance('tr:'+'a'*40,url,calculate(self.task,RULE,'观众','no record'))
        return {'role_policy':self.entry.ROLE_POLICY,'originals':[o],'auxiliaries':[],'owners':[o],'hr':[record]}
    def test_live_counter_reset_prevents_cached_calculated_clearance(self):
        p=self.plan();self.task.seed_seconds=0
        with patch.object(self.entry,'from_mp',return_value={'tr':types.SimpleNamespace(tasks=lambda:[self.task])}),patch.object(self.entry,'NexusHr',return_value=types.SimpleNamespace(check=lambda u:HrResult('unknown','no record',2))),patch.object(self.p,'_calculation_rule',return_value=(RULE,'观众')):
            with self.assertRaises(self.entry.ProbeError):self.p._refresh_hr(p,{},threading.Event())
    def test_removed_task_requires_confirmed_journal_and_same_rule(self):
        p=self.plan();job={'journal':{'tasks_removed':['tr:'+'a'*40]}}
        with patch.object(self.entry,'from_mp',return_value={'tr':types.SimpleNamespace(tasks=lambda:[])}),patch.object(self.entry,'NexusHr',return_value=types.SimpleNamespace(check=lambda u:HrResult('unknown','no record',2))),patch.object(self.p,'_calculation_rule',return_value=(RULE,'观众')):
            self.p._refresh_hr(p,job,threading.Event())
            with self.assertRaises(self.entry.ProbeError):self.p._refresh_hr(p,{},threading.Event())
        with patch.object(self.entry,'from_mp',return_value={}),patch.object(self.entry,'NexusHr',return_value=types.SimpleNamespace(check=lambda u:HrResult('unknown','no record',2))),patch.object(self.p,'_calculation_rule',return_value=({**RULE,'hr_duration':999},'观众')):
            with self.assertRaises(self.entry.ProbeError):self.p._refresh_hr(p,job,threading.Event())
    def test_station_incomplete_still_blocks_after_confirmed_task_removal(self):
        p=self.plan();job={'journal':{'tasks_removed':['tr:'+'a'*40]}}
        with patch.object(self.entry,'from_mp',return_value={}),patch.object(self.entry,'NexusHr',return_value=types.SimpleNamespace(check=lambda u:HrResult('incomplete','site pending',2))),patch.object(self.p,'_calculation_rule',return_value=(RULE,'观众')):
            with self.assertRaises(self.entry.ProbeError):self.p._refresh_hr(p,job,threading.Event())
