"""Cloud/CMS-before-HR planning and durable, exact-file cleanup."""
import time
import urllib.parse
from pathlib import Path, PurePosixPath
from .cloud import ProbeError
from .files import identity, same_content_identity, sha1_file, unlink_verified

VIDEO = {'.mkv','.mp4','.m4v','.avi','.ts','.m2ts','.mov','.wmv','.mpg','.mpeg','.iso','.flv','.webm'}
ALLOWED_HR = {'complete','no_hr'}


def owner(task):
    return task.client + ':' + task.hash


def group_for(tasks, seed_hash):
    """Include cross-seeds sharing a concrete media pathname, transitively."""
    group = [t for t in tasks if t.hash == seed_hash]
    paths = {p for t in group for p in t.files if Path(p).suffix.lower() in VIDEO}
    changed = True
    while changed:
        changed = False
        members = {owner(t) for t in group}
        for task in tasks:
            media = {p for p in task.files if Path(p).suffix.lower() in VIDEO}
            if owner(task) not in members and paths & media:
                group.append(task); paths.update(media); changed = True
    return group


def cloud_path(local, mappings):
    candidates = []
    for mapping in mappings:
        base = Path(mapping['local'])
        if Path(local).is_relative_to(base):
            candidates.append(mapping)
    if not candidates:
        raise ProbeError('媒体文件没有配置115路径映射')
    mapping = max(candidates, key=lambda m: len(Path(m['local']).parts))
    relative = Path(local).relative_to(mapping['local'])
    return str(PurePosixPath(mapping['cloud']) / relative.as_posix()), str(PurePosixPath(mapping.get('cd2_source') or mapping['local']) / relative.as_posix())


def plan_group(seed_hash, tasks, clients, find_transfers, cloud_factory, cms, hr, config, cache, stop):
    group = group_for(tasks, seed_hash)
    if not group:
        raise ProbeError('下载器中没有该任务；不会依据旧历史删除文件')
    if not all(t.complete for t in group):
        raise ProbeError('等待下载：关联种子尚未全部完成')
    wanted = sorted({p for t in group for p in t.wanted if Path(p).suffix.lower() in VIDEO})
    if not wanted:
        raise ProbeError('没有可核验的视频文件')
    paths = {}; evidence = []; logical_paths = []
    roots = config['allowed_roots']
    cloud = cloud_factory()
    uploads = cloud.uploads()
    # No HR provider is touched anywhere before every file's cloud/CMS checks.
    for source in wanted:
        if stop.is_set():
            raise ProbeError('巡检已停止')
        source_stat = identity(source, roots)
        records = find_transfers(source)
        destinations = sorted({r.dest for r in records if r.status and r.src == source and r.dest and r.mode == 'link'})
        destinations = [p for p in destinations if Path(p).exists()]
        if not destinations:
            raise ProbeError('等待整理：缺少已完成的硬链接整理记录')
        for destination in destinations:
            if source == destination:
                raise ProbeError('整理源路径与目标路径相同，无法确认本地副本关系')
            target_stat = identity(destination, roots)
            if not same_content_identity(source_stat, target_stat):
                raise ProbeError('整理记录中的源文件与目标不是同一个硬链接')
            remote_path, logical_path = cloud_path(destination, config['mappings'])
            if remote_path in uploads:
                raise ProbeError('等待上传：该文件仍在 CD2 任务队列')
            remote = cloud.get_file(remote_path)
            if not remote or not remote.id or remote.size != source_stat['size'] or len(remote.sha1) != 40:
                raise ProbeError('等待上传：115文件尚未完整对应')
            if uploads and cache.get(source, {}).get('identity') != source_stat:
                raise ProbeError('等待校验：CD2 正在上传，暂缓读取磁盘计算哈希')
            sha1, source_stat = sha1_file(source, roots, cache, stop, config.get('hash_mib_s',16),pause_if=lambda:bool(cloud_factory().uploads()))
            if sha1 != remote.sha1:
                raise ProbeError('等待上传：本地与115的SHA1不一致')
            cms_result = cms.verify(remote, sha1)
            paths[source] = source_stat; paths[destination] = target_stat
            logical_paths.append(logical_path)
            evidence.append({'source':source,'destination':destination,'remote_path':remote.path,
                'remote_id':remote.id,'size':remote.size,'sha1':sha1,'cms':cms_result})
    # A large hash read may take time. Freshly re-query cloud and backup policies
    # before proceeding to HR, rather than trusting the start-of-cycle snapshot.
    fresh = cloud_factory(); blocked = fresh.uploads()
    fresh.assert_keep_cloud(logical_paths)
    for item in evidence:
        remote = fresh.get_file(item['remote_path'])
        if item['remote_path'] in blocked or not remote or (remote.id,remote.size,remote.sha1) != (item['remote_id'],item['size'],item['sha1']):
            raise ProbeError('等待上传：云端状态在校验期间发生变化')
        cms.verify(remote,item['sha1'])
    clearances = []
    verified_at = time.time()
    for task in group:
        if stop.is_set():
            raise ProbeError('巡检已停止')
        source_url=clients[task.client].source(task)
        result = hr.check(source_url)
        try:site=urllib.parse.urlparse(source_url).hostname or ''
        except ValueError:site=''
        clearances.append({'owner':owner(task),'site':site,'state':result.state,'reason':result.reason,'checked_at':result.checked_at})
    if any(c['state']=='incomplete' for c in clearances):
        return {'ready':False,'reason':'等待 HR：关联种子尚未全部达标','hr':clearances,'cloud_verified_at':verified_at,'evidence':evidence}
    if any(c['state'] not in ALLOWED_HR for c in clearances):
        return {'ready':False,'reason':'HR 无法确认：'+next(c['reason'] for c in clearances if c['state'] not in ALLOWED_HR),'hr':clearances,'cloud_verified_at':verified_at,'evidence':evidence}
    return {'ready':True,'reason':'备份、CMS与HR均已核验','owners':[{'client':t.client,'hash':t.hash,'generation':t.generation,'wanted':list(t.wanted)} for t in group],
        'paths':paths,'evidence':evidence,'logical_paths':logical_paths,'hr':clearances,
        'cloud_verified_at':verified_at,'created_at':time.time(),
        'video_bytes':sum(identity(p,roots)['size'] for p in wanted)}


def check_shared(plan, tasks):
    expected = {x['client']+':'+x['hash'] for x in plan['owners']}
    paths = set(plan['paths'])
    for task in tasks:
        if owner(task) not in expected and paths.intersection(task.files):
            raise ProbeError('出现新的共享文件种子，已保留本地数据')
        if owner(task) in expected:
            planned = next(x for x in plan['owners'] if x['client']+':'+x['hash'] == owner(task))
            if not planned.get('generation') or task.generation != planned['generation']:
                raise ProbeError('种子任务被重新添加或缺少创建时间，已保留')
            if not task.complete or set(task.wanted) != set(planned.get('wanted',[])):
                raise ProbeError('种子任务的完成度或文件选择发生变化')


def execute_plan(plan, journal, clients, load_tasks, verify_cloud, roots, save, stop=None):
    """Resume a persisted plan. No generic retry of an uncertain delete request."""
    if not plan.get('ready') or not plan.get('hr') or any(c.get('state') not in ALLOWED_HR for c in plan['hr']):
        raise ProbeError('没有完整的 HR 放行证据')
    if not plan.get('paths') or not plan.get('owners'):
        raise ProbeError('清理计划为空')
    def check_stop():
        if stop and stop.is_set():
            raise ProbeError('巡检已停止，未完成的清理将保留记录')
    check_stop();verify_cloud(plan)
    tasks = load_tasks()
    check_shared(plan,tasks)
    for path, expected in plan['paths'].items():
        if Path(path).exists() or Path(path).is_symlink():
            if not same_content_identity(identity(path,roots),expected):
                raise ProbeError('文件在清理计划生成后已变化')
    requested = journal.setdefault('remove_requested',[])
    removed = journal.setdefault('tasks_removed',[])
    for item in plan['owners']:
        check_stop()
        key = item['client']+':'+item['hash']
        present = any(owner(t)==key for t in tasks)
        if not present:
            if key not in requested:
                raise ProbeError('任务在清理开始前已被外部删除，需要重新核对')
            if key not in removed:
                removed.append(key);save()
            continue
        if key in requested:
            raise ProbeError('上次删除结果未确认且任务仍存在，需要检查后人工重试')
        requested.append(key);save()
        clients[item['client']].remove_task_only(item['hash'])
        tasks = load_tasks()
        check_shared(plan,tasks)
        if any(owner(t)==key for t in tasks):
            raise ProbeError('下载器尚未确认任务移除，文件继续保留')
        removed.append(key);save()
    tasks = load_tasks();check_shared(plan,tasks)
    if any(owner(t) in {x['client']+':'+x['hash'] for x in plan['owners']} for t in tasks):
        raise ProbeError('关联任务仍然存在，文件继续保留')
    finished = journal.setdefault('files_removed',[])
    for path, expected in plan['paths'].items():
        check_stop()
        # Recheck shared references immediately before each unlink. Directory
        # pinning and file identity validation happen in unlink_verified().
        check_shared(plan,load_tasks())
        if path not in finished:
            unlink_verified(path,expected,roots)
            finished.append(path);save()
    journal['completed_at']=time.time();save()
