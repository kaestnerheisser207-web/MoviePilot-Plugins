"""qB/Transmission adapters using existing MP downloader configuration."""
import base64
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from .cloud import ProbeError


@dataclass
class Task:
    client: str
    hash: str
    complete: bool
    files: list[str]
    wanted: list[str]
    source_url: str = ''
    completed_at: int = 0
    seed_seconds: int = 0
    generation: int = 0
    labels: list[str] = field(default_factory=list)
    downloaded_bytes: int | None = None
    ratio: float | None = None


def safe_join(base, name):
    root, child = PurePosixPath(base), PurePosixPath(name)
    if not root.is_absolute() or child.is_absolute() or '..' in child.parts:
        raise ProbeError('下载器返回无效文件路径')
    return str(root / child)


def detail_url(comment):
    import re
    for value in re.findall(r'https?://[^\s<>"\']+', comment or ''):
        try:
            parsed = urllib.parse.urlparse(value)
            args = urllib.parse.parse_qs(parsed.query)
        except ValueError:
            continue
        if (args.get('id') or args.get('torrentid') or [''])[0].isdigit():
            return value
    return ''


def canonical_source_url(value):
    """Keep only a Nexus torrent identity, never download keys or user ids."""
    if not isinstance(value,str) or len(value)>4096:return ''
    try:
        p=urllib.parse.urlparse(value or '')
        q=urllib.parse.parse_qs(p.query);ids=q.get('id') or [];ident=ids[0] if len(ids)==1 else ''
        p.port  # Validate malformed ports before persisting a source identity.
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.path.rsplit('/',1)[-1]!='details.php' or not ident.isdigit():return ''
        return urllib.parse.urlunparse((p.scheme,p.netloc.lower(),p.path,'',urllib.parse.urlencode({'id':ident}),''))
    except (ValueError,TypeError):return ''


class Client:
    def __init__(self, name, kind, config, path_mapping=None, timeout=15, stop=None):
        self.name, self.kind, self.config = name, kind, config
        self.base = str(config.get('host') or '').rstrip('/')
        if '://' not in self.base:
            self.base = 'http://' + self.base
        self.timeout = timeout
        self.mapping = path_mapping
        self.stop = stop
        # Do not apply the external-site proxy to local downloader traffic.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):
                return None
        self.opener = urllib.request.build_opener(NoRedirect(),urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.session_id = None
        self.logged_in = False

    def path(self, value):
        # MP 3.0.1: each pair is (storage_path, downloader_path).
        for storage, download in self.mapping or []:
            if PurePosixPath(value).is_relative_to(PurePosixPath(download)):
                value = str(PurePosixPath(storage) / PurePosixPath(value).relative_to(download))
                break
        if value.startswith('local:'):
            value = value[len('local:'):]
        if not PurePosixPath(value).is_absolute():
            raise ProbeError(f'下载器 {self.name} 映射目标不是本地绝对路径')
        return value

    def request(self, url, *, data=None, headers=None):
        if self.stop and self.stop.is_set():
            raise ProbeError('巡检已停止')
        try:
            with self.opener.open(urllib.request.Request(url, data=data, headers=headers or {}), timeout=self.timeout) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise ProbeError('下载器返回数据过大')
                return raw
        except urllib.error.HTTPError:
            raise
        except ProbeError:
            raise
        except Exception as e:
            raise ProbeError(f'下载器 {self.name} 请求失败：{type(e).__name__}') from None

    def qb(self, path, data=None):
        if not self.logged_in:
            body = urllib.parse.urlencode({'username': self.config.get('username', ''), 'password': self.config.get('password', '')}).encode()
            if self.request(self.base + '/api/v2/auth/login', data=body).strip() != b'Ok.':
                raise ProbeError(f'下载器 {self.name} 登录失败')
            self.logged_in = True
        return self.request(self.base + '/api/v2/' + path,
                            data=urllib.parse.urlencode(data).encode() if data is not None else None)

    def tr(self, method, args):
        auth = (self.config.get('username', '') + ':' + self.config.get('password', '')).encode()
        headers = {'Authorization': 'Basic ' + base64.b64encode(auth).decode(), 'Content-Type': 'application/json'}
        if self.session_id:
            headers['X-Transmission-Session-Id'] = self.session_id
        endpoint = self.base if self.base.endswith('/rpc') else self.base + '/transmission/rpc'
        body = json.dumps({'method': method, 'arguments': args}).encode()
        try:
            raw = self.request(endpoint, data=body, headers=headers)
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise ProbeError(f'下载器 {self.name} HTTP {e.code}') from None
            # 409 explicitly rejects execution and supplies a session challenge;
            # replaying this rejected RPC does not retry an ambiguous mutation.
            self.session_id = e.headers.get('X-Transmission-Session-Id')
            if not self.session_id:
                raise ProbeError('Transmission 会话校验失败') from None
            headers['X-Transmission-Session-Id'] = self.session_id
            raw = self.request(endpoint, data=body, headers=headers)
        result = json.loads(raw)
        if result.get('result') != 'success':
            raise ProbeError(f'下载器 {self.name} 未确认请求成功')
        return result.get('arguments', {})

    def tasks(self):
        try:
            if self.kind == 'qbittorrent':
                result = []
                for row in json.loads(self.qb('torrents/info')):
                    h = row['hash']
                    files = json.loads(self.qb('torrents/files?' + urllib.parse.urlencode({'hash': h})))
                    base = self.path(row['save_path'])
                    all_paths = [safe_join(base, f['name']) for f in files]
                    selected = [safe_join(base, f['name']) for f in files if f.get('priority', 0) > 0]
                    complete = bool(selected) and row.get('progress') == 1 and all(f.get('progress') == 1 for f in files if f.get('priority', 0) > 0)
                    result.append(Task(self.name, h, complete, all_paths, selected, completed_at=row.get('completion_on', 0), seed_seconds=row.get('seeding_time', 0), generation=row.get('added_on',0), labels=[x.strip() for x in str(row.get('tags') or '').split(',') if x.strip()], downloaded_bytes=row.get('downloaded'),ratio=row.get('ratio')))
                return result
            if self.kind == 'transmission':
                rows = self.tr('torrent-get', {'fields': ['hashString', 'percentDone', 'downloadDir', 'files', 'fileStats', 'comment', 'doneDate', 'secondsSeeding', 'addedDate', 'labels', 'downloadedEver', 'uploadRatio']})['torrents']
                result = []
                for row in rows:
                    files, stats = row['files'], row['fileStats']
                    if len(files) != len(stats):
                        raise ProbeError('Transmission 文件清单不完整')
                    base = self.path(row['downloadDir'])
                    all_paths = [safe_join(base, f['name']) for f in files]
                    selected = [p for p, s in zip(all_paths, stats) if s.get('wanted')]
                    complete = bool(selected) and row.get('percentDone') == 1 and all(f.get('bytesCompleted', -1) == f['length'] for f,s in zip(files,stats) if s.get('wanted'))
                    result.append(Task(self.name, row['hashString'], complete, all_paths, selected,
                        detail_url(row.get('comment')), row.get('doneDate',0), row.get('secondsSeeding',0), row.get('addedDate',0), list(row.get('labels') or []), row.get('downloadedEver'),row.get('uploadRatio')))
                return result
            raise ProbeError(f'下载器 {self.kind} 尚未适配，无法排除共享文件')
        except ProbeError:
            raise
        except Exception as e:
            raise ProbeError(f'下载器 {self.name} 清单读取失败：{type(e).__name__}') from None

    def source(self, task):
        if task.source_url:
            return task.source_url
        if self.kind == 'qbittorrent':
            row = json.loads(self.qb('torrents/properties?' + urllib.parse.urlencode({'hash': task.hash})))
            return detail_url(row.get('comment'))
        return ''

    def remove_task_only(self, task_hash):
        """Remove the task, preserving payload files for the checked unlink step."""
        if self.kind == 'qbittorrent':
            self.qb('torrents/delete', {'hashes': task_hash, 'deleteFiles': 'false'})
        elif self.kind == 'transmission':
            self.tr('torrent-remove', {'ids': [task_hash], 'delete-local-data': False})
        else:
            raise ProbeError('下载器删除接口尚未适配')


def from_mp(stop=None):
    from app.sdk.services import DownloaderHelper
    configurations = DownloaderHelper().get_configs()
    return {name: Client(name, item.type, item.config, item.path_mapping,stop=stop)
            for name, item in configurations.items()}
