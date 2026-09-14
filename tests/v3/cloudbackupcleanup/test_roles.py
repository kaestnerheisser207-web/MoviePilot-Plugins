import types,threading,unittest
from test_safety import MediaFixture,engine,downloaders,cloud
from cloudbackupcleanup.roles import classify,ROLE_POLICY

class RoleTests(unittest.TestCase):
    def task(self,labels=(),downloaded=0):
        return types.SimpleNamespace(hash='abc',labels=list(labels),downloaded_bytes=downloaded)
    def test_moved_original_with_zero_download_bytes_stays_original(self):
        task=self.task(['IYUU自动转移'])
        self.assertEqual(classify(task,[types.SimpleNamespace(download_hash='abc')])['role'],'original')
    def test_download_history_overrides_auxiliary_label(self):
        self.assertEqual(classify(self.task(['IYUU自动辅种']),[types.SimpleNamespace(download_hash='abc')])['role'],'original')
    def test_zero_download_bytes_alone_is_not_auxiliary_proof(self):
        self.assertEqual(classify(self.task(),[])['role'],'unknown')
    def test_explicit_auxiliary_label_and_zero_bytes(self):
        self.assertEqual(classify(self.task(['IYUU自动辅种']),[])['role'],'auxiliary')
    def test_auxiliary_that_downloaded_payload_needs_review(self):
        self.assertEqual(classify(self.task(['IYUU自动辅种'],123),[])['role'],'unknown')

class OriginalScopeTests(MediaFixture,unittest.TestCase):
    def roles(self,t):return {'role':'original' if t.hash=='hash' else 'auxiliary','basis':'test'}
    def scoped_plan(self):
        return engine.plan_group('hash',self.tasks,self.clients,self.find,self.cloud_factory,self.cms,self.hr,self.config,self.cache,threading.Event(),role_lookup=self.roles)
    def add_aux(self):
        self.tasks.append(downloaders.Task('tr','aux',True,[str(self.src)],[str(self.src)],generation=124))
    def test_cleared_original_then_auxiliary_are_both_checked_before_removal(self):
        self.add_aux();p=self.scoped_plan()
        self.assertEqual(self.hr_calls,2);self.assertEqual(len(p['hr']),2)
        self.assertEqual([x['role'] for x in p['hr']],['original','auxiliary'])
        self.assertEqual(len(p['owners']),2);self.assertEqual(p['auxiliaries'][0]['hash'],'aux')
        engine.check_scope(p,{'hash'})
    def test_auxiliary_cannot_expand_payload_scope(self):
        self.add_aux();self.tasks[-1].wanted.append('/another/video.mkv')
        with self.assertRaisesRegex(cloud.ProbeError,'范围之外'):self.scoped_plan()
        self.assertEqual(self.hr_calls,0)
    def test_unknown_role_retains_group_without_auxiliary_hr_queries(self):
        self.add_aux();self.roles=lambda t:{'role':'original' if t.hash=='hash' else 'unknown'}
        p=self.scoped_plan();self.assertFalse(p['ready']);self.assertEqual(self.hr_calls,1)
    def test_role_change_before_delete_is_blocked(self):
        self.add_aux();p=self.scoped_plan()
        with self.assertRaisesRegex(cloud.ProbeError,'角色发生变化'):
            engine.check_shared(p,self.tasks,lambda t:{'role':'original'})
    def test_unrelated_original_outside_hash_scope_still_blocks(self):
        self.add_aux();self.roles=lambda t:{'role':'original'};p=self.scoped_plan()
        with self.assertRaisesRegex(cloud.ProbeError,'限定 hash'):engine.check_scope(p,{'hash'})
    def test_missing_role_revalidation_cannot_use_saved_plan(self):
        self.add_aux();p=self.scoped_plan()
        with self.assertRaisesRegex(cloud.ProbeError,'角色复核'):engine.check_shared(p,self.tasks)

    def test_original_not_clear_never_queries_auxiliary_hr(self):
        self.add_aux();self.hr_state='incomplete';p=self.scoped_plan()
        self.assertEqual(self.hr_calls,1);self.assertFalse(p['ready'])
        self.assertEqual([x['role'] for x in p['hr']],['original'])
    def test_auxiliary_personal_hr_blocks_removal(self):
        from cloudbackupcleanup.hr import HrResult
        self.add_aux();answers=iter(['complete','incomplete'])
        self.hr=types.SimpleNamespace(check=lambda url:HrResult(next(answers),'site result',1))
        p=self.scoped_plan();self.assertFalse(p['ready']);self.assertIn('辅种个人 HR 未达标',p['reason'])
        self.assertTrue(self.src.exists())
    def test_unknown_auxiliary_hr_is_not_an_exemption(self):
        from cloudbackupcleanup.hr import HrResult
        self.add_aux();answers=iter(['complete','unknown'])
        self.hr=types.SimpleNamespace(check=lambda url:HrResult(next(answers),'no record',1))
        self.assertFalse(self.scoped_plan()['ready'])
    def test_cleared_original_and_auxiliary_removed_before_verified_files(self):
        self.add_aux();p=self.scoped_plan();calls=[]
        def remove(h):
            self.assertTrue(self.src.exists());calls.append(h)
            self.tasks[:]=[t for t in self.tasks if t.hash!=h]
        self.clients['tr'].remove_task_only=remove
        engine.execute_plan(p,{},self.clients,lambda:list(self.tasks),lambda p:None,
            [str(self.root)],lambda:None,allowed_hashes={'hash'},verify_hr=lambda p:None,role_lookup=self.roles)
        self.assertEqual(set(calls),{'hash','aux'});self.assertFalse(self.src.exists());self.assertFalse(self.dst.exists())

    def test_original_sidecars_shared_by_auxiliary_do_not_expand_video_deletion(self):
        note=self.src.with_suffix('.nfo');note.write_text('keep original metadata')
        self.task.files.append(str(note));self.task.wanted.append(str(note));self.add_aux()
        self.tasks[-1].files.append(str(note));self.tasks[-1].wanted.append(str(note))
        p=self.scoped_plan();self.assertTrue(p['ready']);self.assertNotIn(str(note),p['paths'])
        self.assertEqual(note.read_text(),'keep original metadata')
