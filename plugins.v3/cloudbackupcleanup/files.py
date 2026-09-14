"""Identity checks and exact-path unlinking; no recursive directory deletion."""
import hashlib
import os
import stat
import time
from pathlib import Path
from .cloud import ProbeError


def identity(path, roots):
    p = Path(path)
    if not p.is_absolute() or '..' in p.parts:
        raise ProbeError('本地路径无效')
    allowed = [Path(x).resolve() for x in roots]
    if not any(p.is_relative_to(root) for root in allowed):
        raise ProbeError('文件不在允许的本地目录内')
    # Refuse FUSE cloud mounts even if an operator accidentally includes one.
    mounts = Path('/proc/self/mountinfo')
    if mounts.is_file():
        matching = []
        for line in mounts.read_text().splitlines():
            before, sep, after = line.partition(' - ')
            fields = before.split()
            if sep and len(fields)>4:
                mount = Path(fields[4].replace('\\040',' ').replace('\\134','\\'))
                if p.is_relative_to(mount):
                    matching.append((len(mount.parts),after.split()[0]))
        if matching and max(matching)[1].startswith('fuse'):
            raise ProbeError('禁止清理 FUSE/网盘挂载路径')
    current = Path(p.anchor)
    for part in p.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ProbeError('本地路径包含符号链接')
    s = p.stat()
    if not stat.S_ISREG(s.st_mode):
        raise ProbeError('本地目标不是普通文件')
    return {'dev': s.st_dev, 'ino': s.st_ino, 'size': s.st_size, 'mtime_ns': s.st_mtime_ns, 'ctime_ns': s.st_ctime_ns}


def same_content_identity(a, b):
    return all(a[k] == b[k] for k in ('dev', 'ino', 'size', 'mtime_ns'))


def sha1_file(path, roots, cache, stop, max_mib_s=16, pause_if=None):
    before = identity(path, roots)
    key = str(path)
    cached = cache.get(key)
    if cached and cached.get('identity') == before:
        return cached['sha1'], before
    digest = hashlib.sha1()
    started = time.monotonic(); count = 0; last_busy_check = started
    with open(path, 'rb') as stream:
        s = os.fstat(stream.fileno())
        if (s.st_dev, s.st_ino) != (before['dev'], before['ino']):
            raise ProbeError('文件在校验前已被替换')
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            if stop.is_set():
                raise ProbeError('巡检已停止')
            if pause_if and time.monotonic()-last_busy_check >= 5:
                last_busy_check=time.monotonic()
                if pause_if():
                    raise ProbeError('等待校验：CD2 开始上传，暂缓读取磁盘')
            digest.update(block); count += len(block)
            delay = count / (max(1, max_mib_s) * 1024 * 1024) - (time.monotonic() - started)
            if delay > 0 and stop.wait(delay):
                raise ProbeError('巡检已停止')
    after = identity(path, roots)
    if after != before:
        raise ProbeError('文件在校验过程中发生变化')
    value = digest.hexdigest().upper()
    cache[key] = {'identity': before, 'sha1': value}
    return value, before


def unlink_verified(path, expected, roots):
    """Delete one previously verified name using a pinned directory fd.

    The downloader must already be absent and shared references checked by the
    coordinator. Missing files are idempotent; changed identities are refused.
    """
    p = Path(path)
    if not p.exists() and not p.is_symlink():
        return False
    actual = identity(p, roots)
    if not same_content_identity(actual, expected):
        raise ProbeError('清理前文件身份发生变化，已保留')
    # Pin every ancestor, not only the final directory: swapping an ancestor
    # for a symlink must not redirect unlink outside the configured roots.
    parent = os.open(p.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in p.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent); parent = child
    except Exception:
        os.close(parent)
        raise ProbeError('清理目录发生变化，已停止') from None
    try:
        s = os.stat(p.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(s.st_mode) or (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) != (
                expected['dev'], expected['ino'], expected['size'], expected['mtime_ns']):
            raise ProbeError('清理前文件被替换，已保留')
        os.unlink(p.name, dir_fd=parent)
    finally:
        os.close(parent)
    return True
