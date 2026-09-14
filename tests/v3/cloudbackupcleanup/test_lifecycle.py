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


def load_entry():
    modules={}
    for name in ('app.plugins','app.sdk.logging','app.sdk.queries','apscheduler.triggers.date','apscheduler.triggers.interval'):
        modules[name]=types.ModuleType(name)
    modules['app.plugins']._PluginBase=Base
    modules['app.sdk.logging'].logger=types.SimpleNamespace(info=lambda *a:None)
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
