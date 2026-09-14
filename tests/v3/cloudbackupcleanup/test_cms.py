import importlib,pathlib,sqlite3,sys,tempfile,types,unittest
from contextlib import closing
ROOT=pathlib.Path(__file__).resolve().parents[3]
package=types.ModuleType('cloudbackupcleanup');package.__path__=[str(ROOT/'plugins.v3'/'cloudbackupcleanup')]
sys.modules.setdefault('cloudbackupcleanup',package)
cms=importlib.import_module('cloudbackupcleanup.cms');cloud=importlib.import_module('cloudbackupcleanup.cloud')


class CmsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name).resolve();self.db=self.root/'cms.db'
        self.strm=self.root/'strm'/'tv'/'a.strm';self.strm.parent.mkdir(parents=True)
        self.strm.write_text('http://cms.test:9527/d/test-pick-code.mkv?/a.mkv')
        self.sha='A'*40
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE cloud_data (fid TEXT,full_y_path TEXT,is_dir INT,sha1 TEXT,size INT,status INT,action TEXT,local_path TEXT,local_name TEXT,pick_code TEXT,sync_time INT)')
            db.execute('INSERT INTO cloud_data VALUES (?,?,?,?,?,?,?,?,?,?,?)',('id','/tv/a.mkv',0,self.sha,123,1,'STRM','/media/tv','a.strm','test-pick-code',10))
        self.client=cms.CmsIndex(str(self.db),str(self.root/'strm'),'http://cms.test:9527')
        self.remote=cloud.RemoteFile('id','/115open/tv/a.mkv',123,self.sha)
    def tearDown(self):self.tmp.cleanup()
    def test_complete_index_and_strm(self):self.assertEqual(self.client.verify(self.remote,self.sha)['synced_at'],10)
    def test_wrong_cloud_id_is_not_same_title_match(self):
        with self.assertRaises(cloud.ProbeError):self.client.verify(cloud.RemoteFile('other',self.remote.path,123,self.sha),self.sha)
    def test_wrong_strm_target_blocks(self):
        self.strm.write_text('http://cms.test:9527/d/other.mkv')
        with self.assertRaisesRegex(cloud.ProbeError,'不匹配'):self.client.verify(self.remote,self.sha)
    def test_external_strm_host_blocks(self):
        self.strm.write_text('http://evil.test/d/test-pick-code.mkv')
        with self.assertRaisesRegex(cloud.ProbeError,'配置的 CMS'):self.client.verify(self.remote,self.sha)
    def test_missing_strm_blocks(self):
        self.strm.unlink()
        with self.assertRaisesRegex(cloud.ProbeError,'尚未生成'):self.client.verify(self.remote,self.sha)
    def test_wal_requires_separate_consistency_adaptation(self):
        db=sqlite3.connect(self.db);db.execute('PRAGMA journal_mode=WAL')
        try:
            with self.assertRaisesRegex(cloud.ProbeError,'DELETE'):self.client.verify(self.remote,self.sha)
        finally:db.close()
    def test_wrong_index_hash_blocks(self):
        with closing(sqlite3.connect(self.db)) as db, db:db.execute('UPDATE cloud_data SET sha1=?',('B'*40,))
        with self.assertRaisesRegex(cloud.ProbeError,'索引尚未'):self.client.verify(self.remote,self.sha)


if __name__=='__main__':unittest.main()
