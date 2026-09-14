"""CMS index and STRM verification. SQL access is read-only and parameterized."""
import os
import sqlite3
import urllib.parse
from contextlib import closing
from pathlib import Path, PurePosixPath
from .cloud import ProbeError


class CmsIndex:
    def __init__(self, database, strm_root, cms_url, cloud_root='/115open'):
        self.database = database
        self.strm_root = Path(strm_root)
        self.host = urllib.parse.urlparse(cms_url).netloc
        self.cloud_root = PurePosixPath(cloud_root)

    def verify(self, remote, local_sha1):
        if not Path(self.database).is_file():
            raise ProbeError('CMS 索引未接入：请挂载只读数据库')
        try:
            expected_path = '/' + str(PurePosixPath(remote.path).relative_to(self.cloud_root))
            with closing(sqlite3.connect('file:' + urllib.parse.quote(self.database, safe='/') + '?mode=ro', uri=True, timeout=2)) as db:
                db.execute('PRAGMA query_only=ON')
                if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':
                    raise ProbeError('CMS 数据库不是 DELETE 模式，需先验证一致性接入')
                rows = db.execute('SELECT sha1,size,status,action,local_path,local_name,pick_code,sync_time '
                    'FROM cloud_data WHERE fid=? AND full_y_path=? AND is_dir=0', (remote.id, expected_path)).fetchall()
            if len(rows) != 1:
                raise ProbeError('CMS 尚未索引对应的115文件')
            sha1, size, status, action, folder, name, pick, sync_time = rows[0]
            if status != 1 or action != 'STRM' or size != remote.size or str(sha1).upper() != local_sha1:
                raise ProbeError('CMS 索引尚未与文件校验结果一致')
            relative = PurePosixPath(folder).relative_to('/media')
            if '..' in relative.parts or Path(name).name != name:
                raise ProbeError('CMS STRM 路径无效')
            target = self.strm_root / str(relative) / name
            if not target.resolve().is_relative_to(self.strm_root.resolve()) or not target.is_file():
                raise ProbeError('CMS STRM 尚未生成')
            if target.stat().st_size > 16384:
                raise ProbeError('CMS STRM 内容异常')
            url = urllib.parse.urlparse(target.read_text().strip())
            if url.scheme not in ('http', 'https') or url.netloc != self.host or not url.path.startswith('/d/'):
                raise ProbeError('STRM 未指向配置的 CMS 服务')
            if not pick or os.path.basename(url.path).rsplit('.', 1)[0] != pick:
                raise ProbeError('STRM 与 CMS 云端记录不匹配')
            return {'synced_at': sync_time, 'strm': str(target)}
        except ProbeError:
            raise
        except Exception as e:
            raise ProbeError('CMS 核验失败：' + type(e).__name__) from None
