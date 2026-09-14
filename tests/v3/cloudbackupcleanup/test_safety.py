import hashlib
import importlib
import json
import os
import pathlib
import struct
import sys
import tempfile
import threading
import types
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[3]
PACKAGE=ROOT/'plugins.v3'/'cloudbackupcleanup'
module=types.ModuleType('cloudbackupcleanup');module.__path__=[str(PACKAGE)]
sys.modules.setdefault('cloudbackupcleanup',module)
cloud=importlib.import_module('cloudbackupcleanup.cloud')
files=importlib.import_module('cloudbackupcleanup.files')
engine=importlib.import_module('cloudbackupcleanup.engine')
downloaders=importlib.import_module('cloudbackupcleanup.downloaders')
hr=importlib.import_module('cloudbackupcleanup.hr')
cmsmod=importlib.import_module('cloudbackupcleanup.cms')


class WireTests(unittest.TestCase):
    def test_link_observer_blocks_even_with_passive_directory_exclusion(self):
        config={'enabled':True,'monitor_dirs':'/video/movie','exclude_dirs':'/video/movie'}
        with self.assertRaisesRegex(cloud.ProbeError,'监控重叠'):
            engine.check_delete_observer(['/video/movie/test/a.mkv'],config)
    def test_link_observer_allows_unmonitored_or_explicit_keyword_exclusion(self):
        config={'enabled':True,'monitor_dirs':'/video/movie','exclude_keywords':'CodexCleanupTest'}
        engine.check_delete_observer(['/video/test/a.mkv'],config)
        engine.check_delete_observer(['/video/movie/CodexCleanupTest/a.mkv'],config)
        engine.check_delete_observer(['/video/movie/a.mkv'],{'enabled':False})
    def test_truncated_frames_are_not_success(self):
        for value in (b'',b'\x00',b'\x00'+struct.pack('>I',4)+b'x'):
            with self.subTest(value=value),self.assertRaises(cloud.ProbeError):cloud.frames(value)
    def test_error_trailer_rejected_even_with_message(self):
        trailer=b'grpc-status: 7\r\n'
        raw=b'\0'+struct.pack('>I',0)+b'\x80'+struct.pack('>I',len(trailer))+trailer
        with self.assertRaises(cloud.ProbeError):cloud.frames(raw)
    def test_success_and_unicode_fields(self):
        payload=cloud.field(1,'电影')+cloud.field(2,1392653391)
        trailer=b'grpc-status: 0\r\n'
        result=cloud.frames(b'\0'+struct.pack('>I',len(payload))+payload+b'\x80'+struct.pack('>I',len(trailer))+trailer)
        self.assertEqual(cloud.text(result[0],1),'电影');self.assertEqual(cloud.first(result[0],2),1392653391)
    def test_mutation_rpc_not_available(self):
        with self.assertRaises(cloud.ProbeError):cloud.CloudDrive('http://example','secret').rpc('DeleteFile')
    def test_stopped_cloud_probe_does_not_start_network(self):
        stop=threading.Event();stop.set()
        client=cloud.CloudDrive('http://example','secret',stop=stop)
        with self.assertRaisesRegex(cloud.ProbeError,'巡检已停止'):client.uploads()
    def test_decoder_rejects_bad_lengths(self):
        with self.assertRaises(cloud.ProbeError):cloud.decode(b'\x0a\xff')
    def test_backup_policy_requires_keep(self):
        client=cloud.CloudDrive('http://example','secret')
        def reply(delete_rule=2,sync=0,completion=0):
            backup=cloud.field(1,'/video/tv')+cloud.field(5,delete_rule)+cloud.field(14,sync)+cloud.field(13,completion)
            return [{1:[cloud.field(1,backup)]}]
        for rule in [(0,0,0),(2,1,0),(2,0,1)]:
            client.rpc=lambda *a,r=rule:reply(*r)
            with self.assertRaises(cloud.ProbeError):client.assert_keep_cloud(['/video/tv/a.mkv'])
        client.rpc=lambda *a:reply();client.assert_keep_cloud(['/video/tv/a.mkv'])
        with self.assertRaises(cloud.ProbeError):client.assert_keep_cloud(['/video/other/a.mkv'])


class MediaFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name).resolve()
        self.src=self.root/'download'/'a.mkv';self.dst=self.root/'library'/'a.mkv'
        self.src.parent.mkdir();self.dst.parent.mkdir();self.src.write_bytes(b'verified video bytes');os.link(self.src,self.dst)
        self.task=downloaders.Task('tr','hash',True,[str(self.src)],[str(self.src)],generation=123)
        self.tasks=[self.task];self.clients={'tr':types.SimpleNamespace(source=lambda t:'https://example/details.php?id=1')}
        self.sha=hashlib.sha1(self.src.read_bytes()).hexdigest().upper()
        self.remote=cloud.RemoteFile('id','/115open/a.mkv',self.src.stat().st_size,self.sha)
        self.hr_calls=0;self.hr_state='complete';self.cloud_error=False;self.cms_error=False;self.uploads=set()
        outer=self
        class Cloud:
            def uploads(self):return outer.uploads
            def get_file(self,path):return outer.remote
            def assert_keep_cloud(self,paths):
                if outer.cloud_error:raise cloud.ProbeError('unsafe backup policy')
        self.cloud_factory=Cloud
        class Cms:
            def verify(self,*args):
                if outer.cms_error:raise cloud.ProbeError('CMS not ready')
                return {'synced_at':1,'strm':'test'}
        self.cms=Cms()
        class Hr:
            def check(self,url):
                outer.hr_calls+=1
                return hr.HrResult(outer.hr_state,'checked',1)
        self.hr=Hr()
        self.config={'allowed_roots':[str(self.root)],'mappings':[{'local':str(self.dst.parent),'cloud':'/115open'}],'hash_mib_s':100000}
        self.cache={}
        self.find=lambda p:[types.SimpleNamespace(src=str(self.src),dest=str(self.dst),mode='link',status=True)]
    def tearDown(self):self.tmp.cleanup()
    def plan(self):return engine.plan_group('hash',self.tasks,self.clients,self.find,self.cloud_factory,self.cms,self.hr,self.config,self.cache,threading.Event())


class PlanningTests(MediaFixture,unittest.TestCase):
    def test_valid_plan_does_not_delete_files(self):
        result=self.plan();self.assertTrue(result['ready']);self.assertEqual(self.hr_calls,1)
        self.assertTrue(self.src.exists());self.assertTrue(self.dst.exists())
    def test_not_uploaded_never_queries_hr(self):
        for remote in (None,cloud.RemoteFile('id','/115open/a.mkv',1,self.sha),cloud.RemoteFile('id','/115open/a.mkv',self.remote.size,'F'*40)):
            self.remote=remote
            with self.assertRaisesRegex(cloud.ProbeError,'等待上传'):self.plan()
        self.assertEqual(self.hr_calls,0)
    def test_active_upload_never_queries_hr(self):
        self.uploads={'/115open/a.mkv'}
        with self.assertRaisesRegex(cloud.ProbeError,'等待上传'):self.plan()
        self.assertEqual(self.hr_calls,0)
    def test_cms_not_ready_never_queries_hr(self):
        self.cms_error=True
        with self.assertRaisesRegex(cloud.ProbeError,'CMS not ready'):self.plan()
        self.assertEqual(self.hr_calls,0)
    def test_unsafe_cd2_policy_never_queries_hr(self):
        self.cloud_error=True
        with self.assertRaisesRegex(cloud.ProbeError,'unsafe backup policy'):self.plan()
        self.assertEqual(self.hr_calls,0)
    def test_one_missing_episode_blocks_whole_torrent(self):
        other=self.src.parent/'b.mkv';other.write_bytes(b'not uploaded')
        self.task.wanted.append(str(other));self.task.files.append(str(other))
        with self.assertRaisesRegex(cloud.ProbeError,'缺少'):self.plan()
        self.assertEqual(self.hr_calls,0)
    def test_cross_seed_incomplete_blocks_group(self):
        self.tasks.append(downloaders.Task('tr','cross',False,[str(self.src)],[str(self.src)],generation=124))
        with self.assertRaisesRegex(cloud.ProbeError,'尚未全部完成'):self.plan()
        self.assertEqual(self.hr_calls,0)
    def test_unknown_hr_retained_then_rechecked(self):
        self.hr_state='unknown';self.assertFalse(self.plan()['ready'])
        self.hr_state='incomplete';self.assertFalse(self.plan()['ready'])
        self.hr_state='complete';self.assertTrue(self.plan()['ready'])
    def test_every_cross_seed_requires_hr_clearance(self):
        self.tasks.append(downloaders.Task('tr','cross',True,[str(self.src)],[str(self.src)],generation=124))
        states=iter(['complete','incomplete'])
        self.hr=types.SimpleNamespace(check=lambda url:hr.HrResult(next(states),'checked',1))
        result=self.plan();self.assertFalse(result['ready']);self.assertEqual(len(result['hr']),2)
        self.assertTrue(self.src.exists())
    def test_replaced_library_copy_refused(self):
        self.dst.unlink();self.dst.write_bytes(self.src.read_bytes())
        with self.assertRaisesRegex(cloud.ProbeError,'不是同一个硬链接'):self.plan()
        self.assertEqual(self.hr_calls,0)
    def test_hash_cache_invalidated_by_content_change(self):
        self.plan();self.src.write_bytes(b'x'*self.src.stat().st_size)
        with self.assertRaisesRegex(cloud.ProbeError,'SHA1不一致'):self.plan()


class CleanupTests(MediaFixture,unittest.TestCase):
    def prepare(self):
        plan=self.plan();journal={};self.saved=0
        def remove(h):self.tasks[:]=[t for t in self.tasks if t.hash!=h]
        self.clients['tr'].remove_task_only=remove
        return plan,journal
    def save(self):self.saved+=1
    def execute(self,plan,journal,save=None):
        engine.execute_plan(plan,journal,self.clients,lambda:list(self.tasks),lambda p:None,[str(self.root)],save or self.save)
    def test_exact_cleanup_leaves_unrelated_files(self):
        extra=self.src.parent/'keep.txt';extra.write_text('original ancillary data')
        plan,journal=self.prepare();self.execute(plan,journal)
        self.assertFalse(self.src.exists());self.assertFalse(self.dst.exists());self.assertTrue(extra.exists())
        self.assertIn('completed_at',journal)
    def test_unknown_clearance_cannot_execute(self):
        plan,journal=self.prepare();plan['hr'][0]['state']='unknown'
        with self.assertRaises(cloud.ProbeError):self.execute(plan,journal)
        self.assertTrue(self.src.exists());self.assertEqual(len(self.tasks),1)
    def test_cross_seed_outside_hash_scope_is_not_deleted(self):
        self.tasks.append(downloaders.Task('tr','cross',True,[str(self.src)],[str(self.src)],generation=124))
        plan,journal=self.prepare()
        with self.assertRaisesRegex(cloud.ProbeError,'限定 hash'):
            engine.execute_plan(plan,journal,self.clients,lambda:list(self.tasks),lambda p:None,[str(self.root)],self.save,allowed_hashes={'hash'})
        self.assertEqual(len(self.tasks),2);self.assertTrue(self.src.exists())
    def test_new_shared_task_prevents_delete(self):
        plan,journal=self.prepare();self.tasks.append(downloaders.Task('tr','new',True,[str(self.src)],[str(self.src)],generation=55))
        with self.assertRaises(cloud.ProbeError):self.execute(plan,journal)
        self.assertTrue(self.src.exists())
    def test_new_share_during_task_removal_keeps_payload(self):
        plan,journal=self.prepare()
        def remove_and_add(h):
            self.tasks[:]=[downloaders.Task('tr','new',True,[str(self.src)],[str(self.src)],generation=999)]
        self.clients['tr'].remove_task_only=remove_and_add
        with self.assertRaisesRegex(cloud.ProbeError,'新的共享'):self.execute(plan,journal)
        self.assertTrue(self.src.exists());self.assertTrue(self.dst.exists())
    def test_readded_same_hash_prevents_delete(self):
        plan,journal=self.prepare();self.task.generation=999
        with self.assertRaises(cloud.ProbeError):self.execute(plan,journal)
        self.assertTrue(self.src.exists())
    def test_changed_file_selection_prevents_delete(self):
        plan,journal=self.prepare();self.task.wanted=[]
        with self.assertRaises(cloud.ProbeError):self.execute(plan,journal)
        self.assertTrue(self.src.exists())
    def test_inplace_file_selection_change_cannot_change_saved_plan(self):
        plan,journal=self.prepare();self.task.wanted.append(str(self.root/'new.mkv'))
        with self.assertRaises(cloud.ProbeError):self.execute(plan,journal)
        self.assertTrue(self.src.exists())
    def test_disabled_worker_stops_before_mutation(self):
        plan,journal=self.prepare();stop=threading.Event();stop.set()
        with self.assertRaisesRegex(cloud.ProbeError,'巡检已停止'):
            engine.execute_plan(plan,journal,self.clients,lambda:list(self.tasks),lambda p:None,[str(self.root)],self.save,stop)
        self.assertEqual(len(self.tasks),1);self.assertTrue(self.src.exists())
    def test_ambiguous_delete_is_not_retried(self):
        plan,journal=self.prepare();calls=[]
        def uncertain(h):calls.append(h);raise TimeoutError()
        self.clients['tr'].remove_task_only=uncertain
        with self.assertRaises(TimeoutError):self.execute(plan,journal)
        with self.assertRaises(cloud.ProbeError):self.execute(plan,journal)
        self.assertEqual(len(calls),1);self.assertTrue(self.src.exists())
        self.tasks.clear();self.execute(plan,journal);self.assertFalse(self.src.exists())
    def test_interrupted_unlinks_resume(self):
        plan,journal=self.prepare()
        def crash():
            if len(journal.get('files_removed',[]))==1:raise RuntimeError('crash')
        with self.assertRaises(RuntimeError):self.execute(plan,journal,crash)
        self.execute(plan,journal);self.assertFalse(self.src.exists());self.assertFalse(self.dst.exists())
    def test_external_task_removal_before_own_request_is_not_clearance(self):
        plan,journal=self.prepare();self.tasks.clear()
        with self.assertRaises(cloud.ProbeError):self.execute(plan,journal)
        self.assertTrue(self.src.exists())
    def test_replaced_file_after_plan_is_preserved(self):
        plan,journal=self.prepare();self.dst.unlink();self.dst.write_bytes(b'new user file')
        with self.assertRaises(cloud.ProbeError):self.execute(plan,journal)
        self.assertEqual(self.dst.read_bytes(),b'new user file')
    def test_symlink_rejected(self):
        plan,journal=self.prepare();self.dst.unlink();self.dst.symlink_to(self.src)
        with self.assertRaises(cloud.ProbeError):self.execute(plan,journal)
        self.assertTrue(self.src.exists())


class HrAndMappingTests(unittest.TestCase):
    HEAD='<tr><td class="colhead">HR编号</td><td class="colhead">种子名称</td><td class="colhead">还需做种时间</td></tr>'
    def test_hr_login_and_error_pages_are_not_empty(self):
        for html in ('<input type="password">','<h1>数据库错误</h1>','<table>'+self.HEAD+'</table>'):
            with self.assertRaises(cloud.ProbeError):hr.parse_page(html,'123')
    def test_hr_exact_torrent_identity(self):
        page='<table>'+self.HEAD+'<tr><td>1</td><td><a href="details.php?id=123">电影</a></td><td>24小时</td></tr></table>'
        self.assertTrue(hr.parse_page(page,'123')[0]);self.assertFalse(hr.parse_page(page,'12')[0])
    def test_empty_hr_page_is_only_a_fact(self):
        page='<table>'+self.HEAD+'<tr><td>森马都没有找到</td></tr></table>'
        self.assertEqual(hr.parse_page(page,'123')[0],False)
    def test_mapping_uses_path_boundaries_and_specific_prefix(self):
        mappings=[{'local':'/video/tv','cloud':'/115open/电视剧'},{'local':'/video/tv/special','cloud':'/115open/特别'}]
        self.assertEqual(engine.cloud_path('/video/tv/special/a.mkv',mappings)[0],'/115open/特别/a.mkv')
        with self.assertRaises(cloud.ProbeError):engine.cloud_path('/video/tv-other/a.mkv',mappings)
    def test_download_mapping_direction(self):
        client=downloaders.Client('test','qbittorrent',{'host':'http://localhost'},[('/video/btdownloads','/downloads')])
        self.assertEqual(client.path('/downloads/show'),'/video/btdownloads/show')
        self.assertEqual(client.path('/downloads-other/show'),'/downloads-other/show')
    def test_parent_traversal_refused(self):
        with self.assertRaises(cloud.ProbeError):downloaders.safe_join('/video','../other.mkv')
    def test_metadata_version_and_delete_default(self):
        import ast
        tree=ast.parse((PACKAGE/'__init__.py').read_text())
        defaults=next(n.value for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='DEFAULTS' for t in n.targets))
        deletion=next(v for k,v in zip(defaults.keys,defaults.values) if isinstance(k,ast.Constant) and k.value=='auto_delete')
        self.assertIs(ast.literal_eval(deletion),False)
        meta=json.loads((ROOT/'package.v3.json').read_text())['CloudBackupCleanup']
        klass=next(n for n in tree.body if isinstance(n,ast.ClassDef))
        version=next(n.value.value for n in klass.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='plugin_version' for t in n.targets))
        self.assertEqual(meta['version'],version)


if __name__=='__main__':unittest.main()
