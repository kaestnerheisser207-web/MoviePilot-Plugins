import copy,importlib.util,importlib.machinery,pathlib,sys,threading,types,unittest
from datetime import timedelta
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[3]
package=types.ModuleType('cloudbackupcleanup');package.__path__=[str(ROOT/'plugins.v3'/'cloudbackupcleanup')]
sys.modules.setdefault('cloudbackupcleanup',package)


class Base:
    def __init__(self):self.saved={};self.saved_config={}
    def get_data(self,key):return copy.deepcopy(self.saved.get(key))
    def save_data(self,key,value):self.saved[key]=copy.deepcopy(value)
    def update_config(self,value):self.saved_config=copy.deepcopy(value);return True
    def get_config(self,key=None):return {}


def load_entry():
    modules={}
    for name in ('app.plugins','app.sdk.logging','app.sdk.queries','app.sdk.events','app.schemas.types','apscheduler.triggers.date','apscheduler.triggers.interval'):
        modules[name]=types.ModuleType(name)
    modules['app.plugins']._PluginBase=Base
    modules['app.sdk.logging'].logger=types.SimpleNamespace(info=lambda *a:None,warn=lambda *a:None)
    modules['app.sdk.events'].Event=types.SimpleNamespace
    modules['app.sdk.events'].eventmanager=types.SimpleNamespace(register=lambda event:lambda func:func)
    modules['app.schemas.types'].EventType=types.SimpleNamespace(DownloadAdded='DownloadAdded')
    modules['app.sdk.queries'].list_transfer_history=lambda **kw:None
    modules['apscheduler.triggers.date'].DateTrigger=lambda **kw:kw
    modules['apscheduler.triggers.interval'].IntervalTrigger=lambda **kw:types.SimpleNamespace(get_next_fire_time=lambda previous,now:now+timedelta(minutes=kw['minutes']))
    name='cloudbackupcleanup.lifecycle_fixture'
    loader=importlib.machinery.SourceFileLoader(name,str(ROOT/'plugins.v3'/'cloudbackupcleanup'/'__init__.py'))
    spec=importlib.util.spec_from_loader(name,loader,is_package=False)
    module=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,modules):spec.loader.exec_module(module)
    return module


class LifecycleTests(unittest.TestCase):
    def setUp(self):self.entry=load_entry();self.plugin=self.entry.CloudBackupCleanup()
    def test_upgrade_snapshots_mp_site_ids_and_preserves_existing_config(self):
        config={'enabled':True,'auto_delete':False,'hashes':'a'*40,'cd2_token':'private-test'}
        with patch.object(self.plugin,'_site_options',return_value=[{'title':'家园','value':5,'domain':'hdhome.org'}]):
            self.plugin.init_plugin(config)
        self.assertEqual(self.plugin.saved_config,{**self.entry.DEFAULTS,**config,'sites':[5]})
        self.plugin.init_plugin({**self.plugin.saved_config,'sites':[]})
        self.assertEqual(self.plugin._config['sites'],[])
    def test_form_uses_mp_site_ids_without_credentials_and_keeps_removed_selection(self):
        import json
        self.plugin.init_plugin({'sites':[4,99]})
        with patch.object(self.plugin,'_site_options',return_value=[{'title':'彩虹岛','value':4,'domain':'ptchdbits.co'}]):
            form,_=self.plugin.get_form()
        def walk(nodes):
            for node in nodes:
                yield node
                yield from walk(node.get('content',[]))
        select=next(x for x in walk(form) if x.get('props',{}).get('model')=='sites')
        self.assertEqual(select['props']['items'],[{'title':'彩虹岛','value':4},{'title':'站点 99（MP 中已移除）','value':99}])
        self.assertTrue(select['props']['multiple']);self.assertNotIn('domain',json.dumps(form))
        self.assertIs(select['props']['mobileLayout'],False)
        self.assertTrue(all(x['props'].get('mobileLayout') is False for x in walk(form) if x['component'] in ('VTextField','VTextarea')))
    def test_fresh_revalidation_honors_current_site_selection(self):
        from cloudbackupcleanup.hr import HrResult
        h='a'*40;url='https://site.test/details.php?id=123'
        plan={'owners':[{'client':'tr','hash':h}],'hr':[{'owner':'tr:'+h,'source_url':url}]}
        self.plugin.init_plugin({'sites':[]})
        with patch.object(self.entry,'NexusHr',return_value=types.SimpleNamespace(check=lambda u:HrResult('unknown','unselected',10))) as provider:
            with self.assertRaises(self.entry.ProbeError):self.plugin._refresh_hr(plan,{},threading.Event())
        self.assertEqual(provider.call_args.kwargs['selected_sites'],[])
    def test_download_event_captures_only_source_without_hash_lock_or_network(self):
        self.plugin.init_plugin({'enabled':True});h='a'*40
        event=types.SimpleNamespace(event_data={'hash':h,'downloader':'tr','context':types.SimpleNamespace(torrent_info=types.SimpleNamespace(page_url='https://site.test/details.php?id=123&hit=1&passkey=discard'))})
        self.plugin._lock.acquire()
        try:
            with patch.object(self.entry,'NexusHr') as hr,patch.object(self.entry,'CloudDrive') as cloud:
                self.plugin.remember_download_source(event);hr.assert_not_called();cloud.assert_not_called()
        finally:self.plugin._lock.release()
        self.assertEqual(self.plugin.get_data('source:tr:'+h)['urls'],['https://site.test/details.php?id=123'])
        task=types.SimpleNamespace(client='tr',hash=h)
        self.assertEqual(self.plugin._source_for(task,types.SimpleNamespace(source=lambda t:'')),'https://site.test/details.php?id=123')
    def test_source_conflict_cannot_choose_a_site_silently(self):
        self.plugin.init_plugin({'enabled':True});h='a'*40
        self.plugin.save_data('source:tr:'+h,{'urls':['https://site.test/details.php?id=123']})
        with self.assertRaisesRegex(self.entry.ProbeError,'来源记录存在冲突'):
            self.plugin._source_for(types.SimpleNamespace(client='tr',hash=h),types.SimpleNamespace(source=lambda t:'https://another.test/details.php?id=456'))
    def test_readded_download_keeps_original_hr_marker_and_capture_time(self):
        self.plugin.init_plugin({'enabled':True});h='a'*40
        data={'hash':h,'downloader':'tr','context':{'torrent_info':{'site':4,'site_name':'Test','page_url':'https://site.test/details.php?id=123','hit_and_run':True}}}
        with patch.object(self.entry.time,'time',return_value=100):self.plugin.remember_download_source(types.SimpleNamespace(event_data=data))
        data['context']['torrent_info']['hit_and_run']=False
        with patch.object(self.entry.time,'time',return_value=200):self.plugin.remember_download_source(types.SimpleNamespace(event_data=data))
        source=self.plugin.get_data('source:tr:'+h)
        record=source['histories']['https://site.test/details.php?id=123']
        self.assertEqual(record['time'],100);self.assertIs(record['hit_and_run'],True)
        self.assertTrue(source['ever_had_hr'])
    def test_backfill_cannot_set_personal_clearance(self):
        import json
        self.plugin.init_plugin({'source_mappings':json.dumps({'tr:'+'a'*40:{'url':'https://site.test/details.php?id=123','state':'complete'}})})
        with self.assertRaisesRegex(self.entry.ProbeError,'不能手工设置'):self.plugin._options()
    def test_migration_keeps_cache_journal_and_retires_old_inspection(self):
        old={'jobs':{'job':{'hash':'a'*40,'title':'test','inspection':{'ready':True,'hr':[{'state':'no_hr'}]},'plan':{'ready':True},'journal':{'remove_requested':['tr:a']}}},'hash_cache':{'file':{'sha1':'digest'}}}
        self.plugin.saved['state']=old;self.plugin.init_plugin({'enabled':True})
        state=self.plugin.get_data('state');job=state['jobs']['job']
        self.assertEqual(state['hash_cache'],old['hash_cache'])
        self.assertEqual(job['journal'],old['jobs']['job']['journal'])
        self.assertEqual(job['inspection'],{});self.assertTrue(job['previous_inspection']['ready'])
        self.assertEqual(job['personal_hr_state'],'unknown')
    def test_fresh_site_clearance_is_independent_of_original_hr_marker(self):
        from cloudbackupcleanup.hr import HrResult
        h='a'*40;url='https://site.test/details.php?id=123'
        plan={'ready':True,'owners':[{'client':'tr','hash':h}],'hr':[{'owner':'tr:'+h,'state':'complete','source_url':url}]}
        job={'original_hit_and_run':True};self.plugin.init_plugin({})
        with patch.object(self.entry,'NexusHr',return_value=types.SimpleNamespace(check=lambda u:HrResult('complete','site confirmed',10,basis='site_personal_view'))):
            self.plugin._refresh_hr(plan,job,threading.Event())
        self.assertTrue(job['original_hit_and_run']);self.assertEqual(plan['hr'][0]['state'],'complete')
    def test_stale_clearance_and_later_recovery_preserve_journal(self):
        from cloudbackupcleanup.hr import HrResult
        h='a'*40;url='https://site.test/details.php?id=123'
        plan={'ready':True,'owners':[{'client':'tr','hash':h}],'hr':[{'owner':'tr:'+h,'state':'complete','source_url':url}]}
        job={'journal':{'remove_requested':['tr:'+h]}};self.plugin.init_plugin({})
        with patch.object(self.entry,'NexusHr',return_value=types.SimpleNamespace(check=lambda u:HrResult('unknown','login expired',10))):
            with self.assertRaisesRegex(self.entry.ProbeError,'复核未通过'):self.plugin._refresh_hr(plan,job,threading.Event())
        self.assertEqual(plan['hr'][0]['state'],'complete')
        self.assertEqual(job['inspection']['hr'][0]['state'],'unknown')
        self.assertEqual(job['journal']['remove_requested'],['tr:'+h])
        with patch.object(self.entry,'NexusHr',return_value=types.SimpleNamespace(check=lambda u:HrResult('complete','site confirmed',20,basis='site_personal_view'))):
            self.plugin._refresh_hr(plan,job,threading.Event())
        self.assertEqual(plan['hr'][0]['checked_at'],20)
    def test_legacy_source_mapping_is_scoped_to_client_and_hash(self):
        import json
        h='a'*40;self.plugin.init_plugin({'source_mappings':json.dumps({'tr:'+h.upper():'https://site.test/details.php?id=123'})})
        client=types.SimpleNamespace(source=lambda t:'')
        self.assertEqual(self.plugin._source_for(types.SimpleNamespace(client='tr',hash=h),client),'https://site.test/details.php?id=123')
        self.assertEqual(self.plugin._source_for(types.SimpleNamespace(client='qb',hash=h),client),'')
    def test_disabled_plugin_does_not_capture_new_downloads(self):
        self.plugin.init_plugin({'enabled':False})
        self.plugin.remember_download_source(types.SimpleNamespace(event_data={'hash':'a'*40,'downloader':'tr','context':{'torrent_info':{'page_url':'https://site.test/details.php?id=123'}}}))
        self.assertEqual(self.plugin.saved,{})
    def test_malformed_download_source_does_not_interrupt_download_event(self):
        self.plugin.init_plugin({'enabled':True})
        self.plugin.remember_download_source(types.SimpleNamespace(event_data={'hash':'a'*40,'downloader':'tr','context':{'torrent_info':{'page_url':123}}}))
        self.assertEqual(self.plugin.saved,{})
    def test_legacy_source_rejects_download_endpoint(self):
        import json
        self.plugin.init_plugin({'source_mappings':json.dumps({'tr:'+'a'*40:'https://site.test/download.php?id=123&passkey=secret'})})
        with self.assertRaisesRegex(self.entry.ProbeError,'旧任务来源补录'):self.plugin._options()
    def test_manual_transfer_without_hash_discovered_by_current_source(self):
        self.plugin.init_plugin({'enabled':True})
        task=types.SimpleNamespace(client='tr',hash='test',generation=123,completed_at=123,wanted=['/video/test/a.mp4'])
        def history(**kw):
            return types.SimpleNamespace(items=[types.SimpleNamespace(title='manual test')] if kw['filters'].get('src')=='/video/test/a.mp4' else [])
        with patch.object(self.entry,'list_transfer_history',side_effect=history):self.plugin._discover([task])
        self.assertEqual(self.plugin._state['jobs']['tr:test:123']['title'],'manual test')
    def test_page_uses_current_schedule_instead_of_saved_estimate(self):
        import json
        self.plugin.saved['state']={'jobs':{'test':{'hash':'test','title':'test','next_check':1}}}
        self.plugin.init_plugin({'enabled':True})
        expected=self.entry.datetime(2030,1,2,3,4)
        self.plugin._interval_trigger=types.SimpleNamespace(get_next_fire_time=lambda *args:expected)
        page=json.dumps(self.plugin.get_page(),ensure_ascii=False)
        self.assertIn(expected.strftime('%m-%d %H:%M'),page)
        self.assertNotIn('01-01 08:00',page)
    def test_non_boolean_value_cannot_enable_deletion(self):
        self.plugin.init_plugin({'auto_delete':'true','enabled':'true'})
        self.assertFalse(self.plugin.get_state());self.assertIs(self.plugin._config['auto_delete'],False)
    def test_one_shot_is_consumed(self):
        self.plugin.init_plugin({'run_once':True})
        job=self.plugin.get_service()[0]
        self.assertTrue(job['func_kwargs']['force']);self.assertEqual(job['kwargs'],{})
        with patch.object(self.entry,'from_mp',return_value={}):job['func'](**job['func_kwargs'])
        self.assertEqual(self.plugin.get_service(),[])
        self.assertIs(self.plugin.saved_config['run_once'],False)
    def test_stale_scheduled_job_cannot_use_new_configuration(self):
        self.plugin.init_plugin({'enabled':True})
        job=self.plugin.get_service()[0]
        self.plugin.init_plugin({'enabled':True,'auto_delete':True})
        with patch.object(self.entry,'from_mp') as clients:
            job['func'](**job['func_kwargs']);clients.assert_not_called()
    def test_config_reload_waits_for_old_worker_to_stop(self):
        self.plugin.init_plugin({'enabled':True});job=self.plugin.get_service()[0]
        entered=threading.Event()
        def clients(stop):
            def tasks():
                entered.set();stop.wait(5);return []
            return {'test':types.SimpleNamespace(tasks=tasks)}
        with patch.object(self.entry,'from_mp',side_effect=clients):
            worker=threading.Thread(target=lambda:job['func'](**job['func_kwargs']))
            worker.start();self.assertTrue(entered.wait(2))
            self.plugin.init_plugin({'enabled':False})
            worker.join(2)
            self.assertFalse(worker.is_alive());self.assertFalse(self.plugin.get_state())
    def test_stop_prevents_queued_job_from_starting(self):
        self.plugin.init_plugin({'enabled':True});job=self.plugin.get_service()[0]
        self.plugin.stop_service()
        with patch.object(self.entry,'from_mp') as clients:
            job['func'](**job['func_kwargs']);clients.assert_not_called()
    def test_periodic_tick_is_not_skipped_by_job_deadline_jitter(self):
        import time
        self.plugin.saved['state']={'jobs':{'test':{'hash':'hash','title':'test','next_check':time.time()+1800}},'hash_cache':{}}
        self.plugin.init_plugin({'enabled':True})
        clients={'test':types.SimpleNamespace(tasks=lambda:[])}
        with patch.object(self.entry,'from_mp',return_value=clients),patch.object(self.entry,'plan_group',return_value={'ready':False,'reason':'等待上传'}) as planner:
            self.plugin.run(generation=self.plugin._revision)
            planner.assert_called_once()
        self.assertEqual(self.plugin._state['last_checked_count'],1)


if __name__=='__main__':unittest.main()
