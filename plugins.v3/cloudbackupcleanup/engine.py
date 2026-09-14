"""Cloud/CMS-before-HR planning and durable, exact-file cleanup."""
import time
import urllib.parse
from pathlib import Path, PurePosixPath
from .cloud import ProbeError
from .roles import ROLE_POLICY, partition
from .files import identity, same_content_identity, sha1_file, unlink_verified

VIDEO = {'.mkv','.mp4','.m4v','.avi','.ts','.m2ts','.mov','.wmv','.mpg','.mpeg','.iso','.flv','.webm'}
ALLOWED_HR = {'complete','no_hr'}


def hr_clearance(task_owner, source_url, result):
    try:site=urllib.parse.urlparse(source_url).hostname or ''
    except ValueError:site=''
    data={'owner':task_owner,'site':site,'source_url':source_url,'state':result.state,'reason':result.reason,'checked_at':result.checked_at}
    for key in ('required_seed_seconds','seeded_seconds','remaining_seed_seconds','deadline_at','deadline_text','remaining_seed_text','basis','proof_version','station_reason','calculation'):
        value=getattr(result,key,None)
        if value is not None and value!='':data[key]=value
    return data


def check_delete_observer(paths, config):
    """An external unlink watcher can delete beyond this verified plan."""
    if not config or not config.get('enabled'):
        return
    monitored = [Path(p.strip()) for p in str(config.get('monitor_dirs') or '').splitlines() if p.strip()]
    excluded = [p for p in str(config.get('exclude_keywords') or '').splitlines() if p]
    # RemoveLink's exclude_dirs protects passive targets only; it does not
    # suppress a source deletion event or its torrent/history side effects.
    for value in paths:
        if any(keyword in str(value) for keyword in excluded):
            continue
        if any(Path(value).is_relative_to(root) for root in monitored):
            raise ProbeError('清理暂停：清理硬链接插件正在监控待删路径，可能触发额外删除；请先解决监控重叠')


def check_scope(plan, allowed_hashes):
    targets=plan.get('originals',[]) if plan.get('role_policy')==ROLE_POLICY else plan.get('owners',[])
    if allowed_hashes and any(x['hash'].lower() not in allowed_hashes for x in targets):
        raise ProbeError('关联种子超出限定 hash 范围，已保留；需明确扩大范围后再清理')


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


def plan_group(seed_hash, tasks, clients, find_transfers, cloud_factory, cms, hr, config, cache, stop, source_lookup=None, role_lookup=None, hr_check=None):
    group = group_for(tasks, seed_hash)
    if not group:
        raise ProbeError('下载器中没有该任务；不会依据旧历史删除文件')
    if not all(t.complete for t in group):
        raise ProbeError('等待下载：关联种子尚未全部完成')
    roles=partition(group,role_lookup) if role_lookup else None
    originals={x['client']+':'+x['hash'] for x in roles['original']} if roles else {owner(t) for t in group}
    if roles and not any(t.hash==seed_hash and owner(t) in originals for t in group):
        raise ProbeError('当前任务不是已确认的原始下载种子，保留并等待来源核验')
    wanted = sorted({p for t in group if owner(t) in originals for p in t.wanted if Path(p).suffix.lower() in VIDEO})
    if roles and any(set(t.wanted)-set(wanted) for t in group if owner(t) not in originals):
        raise ProbeError('辅种包含原始下载范围之外的文件，不能联动移除')
    role_data={'role_policy':ROLE_POLICY,'originals':roles['original'],'auxiliaries':roles['auxiliary'],
               'unknown_owners':roles['unknown']} if roles else {}
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
        if owner(task) not in originals:continue
        if stop.is_set():
            raise ProbeError('巡检已停止')
        source_url=source_lookup(task) if source_lookup else clients[task.client].source(task)
        result = hr_check(task,source_url,hr) if hr_check else hr.check(source_url)
        clearances.append({**hr_clearance(owner(task),source_url,result),'role':'original'})
    if any(c['state'] in ('incomplete','overdue') for c in clearances):
        reason='HR 已逾期但未达标，继续保留' if any(c['state']=='overdue' for c in clearances) else '等待 HR：原始下载种子尚未达标'
        return {**role_data,'ready':False,'reason':reason,'hr':clearances,'cloud_verified_at':verified_at,'evidence':evidence}
    if any(c['state'] not in ALLOWED_HR for c in clearances):
        return {**role_data,'ready':False,'reason':'HR 无法确认：'+next(c['reason'] for c in clearances if c['state'] not in ALLOWED_HR),'hr':clearances,'cloud_verified_at':verified_at,'evidence':evidence}
    if roles and roles['unknown']:
        return {**role_data,'ready':False,'reason':'原始下载 HR 已通过，但关联任务身份未知，暂不清理',
                'hr':clearances,'cloud_verified_at':verified_at,'evidence':evidence}
    # Auxiliary checks are deferred until the original download clears HR.
    # A cross-seed label is provenance, never a personal HR exemption.
    if roles:
        for task in group:
            if owner(task) in originals:continue
            if stop.is_set():raise ProbeError('巡检已停止')
            source_url=source_lookup(task) if source_lookup else clients[task.client].source(task)
            result=hr_check(task,source_url,hr) if hr_check else hr.check(source_url)
            clearances.append({**hr_clearance(owner(task),source_url,result),'role':'auxiliary'})
        blocked=next((c for c in clearances if c['state'] not in ALLOWED_HR),None)
        if blocked:
            return {**role_data,'ready':False,'reason':'原始下载 HR 已通过；辅种个人 HR '+
                    ('未达标，继续保留' if blocked['state'] in ('incomplete','overdue') else '尚未确认，继续保留'),
                    'hr':clearances,'cloud_verified_at':verified_at,'evidence':evidence}
    return {**role_data,'ready':True,'reason':'备份、CMS与原始下载HR均已核验','owners':[{'client':t.client,'hash':t.hash,'generation':t.generation,'wanted':list(t.wanted)} for t in group],
        'paths':paths,'evidence':evidence,'logical_paths':logical_paths,'hr':clearances,
        'cloud_verified_at':verified_at,'created_at':time.time(),
        'video_bytes':sum(identity(p,roots)['size'] for p in wanted)}


def check_shared(plan, tasks, role_lookup=None):
    expected = {x['client']+':'+x['hash'] for x in plan['owners']}
    if plan.get('role_policy')==ROLE_POLICY:
        if role_lookup is None:raise ProbeError('缺少任务角色复核，继续保留')
        proofs={x['client']+':'+x['hash']:x for x in plan['originals']+plan['auxiliaries']}
        if set(proofs)!=expected:raise ProbeError('原始下载与辅种身份不完整')
        for task in tasks:
            if owner(task) in expected and role_lookup(task).get('role')!=proofs[owner(task)]['role']:
                raise ProbeError('关联任务角色发生变化，继续保留')
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


def execute_plan(plan, journal, clients, load_tasks, verify_cloud, roots, save, stop=None, allowed_hashes=None, verify_hr=None, role_lookup=None):
    """Resume a persisted plan. No generic retry of an uncertain delete request."""
    if not plan.get('ready') or not plan.get('hr') or any(c.get('state') not in ALLOWED_HR for c in plan['hr']):
        raise ProbeError('没有完整的 HR 放行证据')
    if not plan.get('paths') or not plan.get('owners'):
        raise ProbeError('清理计划为空')
    if verify_hr is None:
        raise ProbeError('缺少清理前站点 HR 复核，不允许使用缓存放行')
    check_scope(plan,allowed_hashes)
    def check_stop():
        if stop and stop.is_set():
            raise ProbeError('巡检已停止，未完成的清理将保留记录')
    check_stop();verify_cloud(plan);check_stop();verify_hr(plan);check_stop()
    tasks = load_tasks()
    check_shared(plan,tasks,role_lookup)
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
        # Site evidence is refreshed for every mutation, including resumed runs.
        verify_cloud(plan);check_stop();verify_hr(plan);check_stop()
        tasks=load_tasks();check_shared(plan,tasks,role_lookup)
        if not any(owner(t)==key for t in tasks):
            raise ProbeError('任务在复核期间被外部移除，需要重新核对')
        requested.append(key);save()
        clients[item['client']].remove_task_only(item['hash'])
        tasks = load_tasks()
        check_shared(plan,tasks,role_lookup)
        if any(owner(t)==key for t in tasks):
            raise ProbeError('下载器尚未确认任务移除，文件继续保留')
        removed.append(key);save()
    tasks = load_tasks();check_shared(plan,tasks,role_lookup)
    if any(owner(t) in {x['client']+':'+x['hash'] for x in plan['owners']} for t in tasks):
        raise ProbeError('关联任务仍然存在，文件继续保留')
    finished = journal.setdefault('files_removed',[])
    for path, expected in plan['paths'].items():
        check_stop()
        # Recheck shared references immediately before each unlink. Directory
        # pinning and file identity validation happen in unlink_verified().
        check_shared(plan,load_tasks(),role_lookup)
        if path not in finished:
            verify_cloud(plan);check_stop();verify_hr(plan);check_stop()
            check_shared(plan,load_tasks(),role_lookup)
            unlink_verified(path,expected,roots)
            finished.append(path);save()
    journal['completed_at']=time.time();save()
