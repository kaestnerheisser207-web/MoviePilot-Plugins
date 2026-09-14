"""Small read-only gRPC-web client for CloudDrive2; no mutation RPC exists here."""
import collections
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass


class ProbeError(RuntimeError):
    """Safe error suitable for display; never include credentials or signed URLs."""


def vint(n):
    result = bytearray()
    while n > 127:
        result.append((n & 127) | 128)
        n >>= 7
    result.append(n)
    return bytes(result)


def field(n, value):
    if isinstance(value, int):
        return vint(n << 3) + vint(value)
    if isinstance(value, str):
        value = value.encode()
    return vint((n << 3) | 2) + vint(len(value)) + value


def decode(raw):
    result = collections.defaultdict(list)
    i = 0
    def number():
        nonlocal i
        value = shift = 0
        while i < len(raw) and shift < 70:
            b = raw[i]; i += 1
            value |= (b & 127) << shift
            if b < 128:
                return value
            shift += 7
        raise ProbeError('CD2 protobuf 数据不完整')
    while i < len(raw):
        tag = number(); wire = tag & 7
        if tag >> 3 == 0:
            raise ProbeError('CD2 protobuf 字段无效')
        if wire == 0:
            value = number()
        elif wire in (1, 2, 5):
            size = number() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(raw) - i:
                raise ProbeError('CD2 protobuf 长度无效')
            value = raw[i:i+size]; i += size
        else:
            raise ProbeError('CD2 protobuf 类型不支持')
        result[tag >> 3].append(value)
    return result


def first(d, n, default=b''):
    return d.get(n, [default])[0]


def text(d, n):
    return first(d, n).decode('utf-8')


def frames(raw):
    messages = []; status = None
    while raw:
        if len(raw) < 5:
            raise ProbeError('CD2 响应帧不完整')
        flag, size = raw[0], struct.unpack('>I', raw[1:5])[0]
        if size > len(raw) - 5:
            raise ProbeError('CD2 响应长度不完整')
        payload, raw = raw[5:5+size], raw[5+size:]
        if flag == 128:
            for line in payload.decode('ascii').splitlines():
                if line.lower().startswith('grpc-status:'):
                    status = line.split(':', 1)[1].strip()
        elif flag == 0:
            messages.append(decode(payload))
        else:
            raise ProbeError('CD2 响应编码不支持')
    if status != '0':
        raise ProbeError('CD2 未返回成功状态' + (f'（{status}）' if status and status.isdigit() else ''))
    return messages


@dataclass(frozen=True)
class RemoteFile:
    id: str
    path: str
    size: int
    sha1: str


class CloudDrive:
    def __init__(self, url, token, timeout=20):
        self.url = url.rstrip('/')
        self.token = token
        self.timeout = timeout
        self.directories = {}

    def rpc(self, method, payload=b''):
        if method not in ('GetUploadFileList', 'GetSubFiles', 'GetRuntimeInfo', 'BackupGetAll'):
            raise ProbeError('禁止调用 CD2 写入接口')
        if not self.token or not self.url.startswith(('http://', 'https://')):
            raise ProbeError('请配置 CD2 地址及只读 API 令牌')
        request = urllib.request.Request(self.url + '/clouddrive.CloudDriveFileSrv/' + method,
            data=b'\0' + struct.pack('>I', len(payload)) + payload,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/grpc-web+proto', 'X-Grpc-Web': '1'})
        try:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self,*args,**kwargs):
                    return None
            opener=urllib.request.build_opener(NoRedirect(),urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=self.timeout) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise ProbeError('CD2 响应超出单次读取上限')
            return frames(raw)
        except ProbeError:
            raise
        except Exception as e:
            raise ProbeError('CD2 查询失败：' + type(e).__name__) from None

    def uploads(self):
        blocked = set()
        for reply in self.rpc('GetUploadFileList', field(1, 1)):
            for raw in reply.get(2, []):
                task = decode(raw)
                if first(task, 8, 0) not in (2, 5, 6, 8):
                    blocked.add(text(task, 2))
        return blocked

    def assert_keep_cloud(self, local_paths):
        """Reject source-cleanup / destination-delete policies on matching tasks."""
        from pathlib import PurePosixPath
        backups = []
        for reply in self.rpc('BackupGetAll'):
            for raw in reply.get(1, []):
                status = decode(raw)
                backup = decode(first(status, 1))
                backups.append(backup)
        for path in local_paths:
            matches = [b for b in backups if text(b, 1) and PurePosixPath(path).is_relative_to(PurePosixPath(text(b, 1)))]
            if not matches:
                raise ProbeError('没有找到对应的 CD2 备份策略，无法确认删除不会同步到云端')
            for backup in matches:
                if first(backup, 5, 0) != 2 or first(backup, 13, 0) != 0 or first(backup, 14, 0):
                    raise ProbeError('CD2 对应备份启用了删源或目标同步删除，请先调整备份策略')

    def get_file(self, path):
        from pathlib import PurePosixPath
        parent = str(PurePosixPath(path).parent)
        if parent not in self.directories:
            files = {}
            for reply in self.rpc('GetSubFiles', field(1, parent) + field(2, 1)):
                for raw in reply.get(1, []):
                    item = decode(raw)
                    if first(item, 30, 0):
                        continue
                    hashes = {}
                    for raw_hash in item.get(70, []):
                        pair = decode(raw_hash)
                        hashes[first(pair, 1, 0)] = text(pair, 2)
                    name = text(item, 3)
                    if name in files:
                        raise ProbeError('115 同一路径存在多个文件，需人工核对')
                    files[name] = RemoteFile(text(item, 1), name, first(item, 4, 0), hashes.get(2, '').upper())
            self.directories[parent] = files
        return self.directories[parent].get(path)
