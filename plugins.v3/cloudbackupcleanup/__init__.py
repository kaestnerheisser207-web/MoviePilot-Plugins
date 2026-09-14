"""MoviePilot V3 plugin: verify cloud backup, CMS and HR before local cleanup."""
import json
import re
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from app.plugins import _PluginBase
from app.sdk.logging import logger
from app.sdk.queries import list_transfer_history, list_download_history
from app.sdk.events import Event, eventmanager
from app.schemas.types import EventType

from .cloud import CloudDrive, ProbeError
from .cms import CmsIndex
from .downloaders import canonical_source_url, from_mp
from .engine import VIDEO, ALLOWED_HR, check_delete_observer, check_scope, execute_plan, group_for, plan_group, hr_clearance
from .hr import NexusHr, PROOF_VERSION, torrent_id
from .tasks import TorrentHistory, TorrentTask, HNRStatus, migrate_state
from . import ui
from .roles import ROLE_POLICY, classify


DEFAULTS = {
    'sites':[], 'enabled':False, 'auto_delete':False, 'run_once':False, 'interval':30,
    'cd2_url':'', 'cd2_token':'', 'cms_url':'', 'cms_database':'/cms-index/cms-online.db',
    'strm_root':'/video/cloud-media', 'cloud_root':'/115open',
    'allowed_roots':'/video/btdownloads\n/video/movie\n/video/tv',
    'mappings':json.dumps([{'local':'/video/movie','cloud':'/115open/电影'},
                           {'local':'/video/tv','cloud':'/115open/电视剧'}],ensure_ascii=False,indent=2),
    'absence_confirmed_sites':'', 'hashes':'', 'batch_size':2, 'hash_mib_s':16,'source_mappings':'{}',
}


class CloudBackupCleanup(_PluginBase):
    plugin_name = '云端备份后清理'
    plugin_desc = '定期核验115备份、CMS同步及HR状态，默认只读核查。'
    plugin_icon = 'CloudDrive_A.png'
    plugin_version = '0.5.0'
    plugin_author = 'kaestnerheisser207-web'
    author_url = 'https://github.com/kaestnerheisser207-web'
    plugin_config_prefix = 'cloudbackupcleanup_'
    plugin_order = 50
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._source_lock = threading.Lock()
        self._stop = threading.Event()
        self._config = dict(DEFAULTS)
        self._state = {}
        self._once = False
        self._revision = 0
        self._interval_trigger = None

    def init_plugin(self, config=None):
        self._stop.set()
        if not self._lock.acquire(timeout=30):
            raise RuntimeError('旧巡检尚未退出，请稍后保存配置')
        try:
            self._config = {**DEFAULTS, **(config or {})}
            # Snapshot current MP IDs for upgrades. Later new sites are opt-in.
            if config and 'sites' not in config:
                self._config['sites']=[site['value'] for site in self._site_options()]
                self.update_config(self._config)
            if not isinstance(self._config.get('sites'),list):
                self._config['sites']=[]
            for switch in ('enabled','auto_delete','run_once'):
                self._config[switch] = self._config.get(switch) is True
            self._once = self._config['run_once']
            if self._once:
                self._config['run_once'] = False
                self.update_config(self._config)
            old_state=self.get_data('state')
            self._state = old_state or {'jobs':{},'hash_cache':{},'discovery_cursor':0,'hr_schema_version':3}
            self._state.setdefault('jobs',{})
            self._state.setdefault('hash_cache',{})
            if old_state and old_state.get('hr_schema_version',0)<3:
                migrate_state(self._state);self._save()
            if self._state.get('role_policy')!=ROLE_POLICY:
                for job in self._state['jobs'].values():
                    if not job.get('completed_at'):
                        if job.get('inspection'):job['previous_inspection']=job['inspection']
                        job['inspection']={};job['personal_hr_state']='unknown'
                        job['status']='等待核验原始下载来源';job['requires_hr_refresh']=True
                self._state['role_policy']=ROLE_POLICY
                if old_state:self._save()
            self._revision += 1
            self._stop = threading.Event()
            self._interval_trigger=IntervalTrigger(minutes=max(5,int(self._config['interval'])))
        finally:
            self._lock.release()

    def get_state(self):
        return bool(self._config.get('enabled'))

    @eventmanager.register(EventType.DownloadAdded)
    def remember_download_source(self,event:Event=None):
        # Capture only the download identity. Do not query HR/cloud or acquire
        # the long-running hash lock in the download event path.
        if not event or not self.get_state() or self._stop.is_set():return
        data=event.event_data or {}
        if not hasattr(data,'get'):return
        h=str(data.get('hash') or '').lower();client=data.get('downloader')
        context=data.get('context')
        torrent=context.get('torrent_info') if isinstance(context,dict) else getattr(context,'torrent_info',None)
        raw=torrent.get('page_url') if isinstance(torrent,dict) else getattr(torrent,'page_url',None)
        url=canonical_source_url(raw)
        if not url or not isinstance(client,str) or not client or len(client)>100 or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',h):return
        key='source:'+client+':'+h
        try:
            get=lambda name: torrent.get(name) if isinstance(torrent,dict) else getattr(torrent,name,None)
            flag=get('hit_and_run');site=get('site');now=time.time()
            history=TorrentHistory(h,client,url,torrent_id(url),
                site if isinstance(site,int) and not isinstance(site,bool) else None,
                str(get('site_name') or ''),flag if isinstance(flag,bool) else None,now).to_dict()
            with self._source_lock:
                before=self.get_data(key) or {};urls=set(before.get('urls',[]));urls.add(url)
                histories=dict(before.get('histories',{}));prior=histories.get(url)
                if prior:
                    history['time']=prior.get('time',now)
                    if prior.get('hit_and_run') is not None:history['hit_and_run']=prior['hit_and_run']
                histories[url]=history
                self.save_data(key,{'urls':sorted(urls),'histories':histories,
                    'captured_at':before.get('captured_at',now),'last_seen_at':now,
                    'ever_had_hr':before.get('ever_had_hr',False) or flag is True,'basis':'DownloadAdded'})
        except Exception:
            logger.warn('下载来源记录失败；后续无法确认来源时将保留文件')

    def _role_for(self,task):
        page=list_download_history(filters={'download_hash':task.hash},page={'page':1,'count':200})
        if page is None or page.has_next:
            return {'role':'unknown','basis':'下载历史读取不完整'}
        saved,records=self._source_records(task)
        proof=classify(task,page.items,captured=bool(records) or saved.get('basis')=='DownloadAdded')
        names={str(getattr(r,'torrent_site','') or '') for r in page.items};names.discard('')
        proof['site_name']=next(iter(names)) if len(names)==1 else next((x[3:] for x in getattr(task,'labels',()) if x.startswith('站点/')),'')
        return proof

    def _configured_sources(self):
        raw=json.loads(self._config.get('source_mappings') or '{}')
        if not isinstance(raw,dict):raise ProbeError('旧任务来源补录必须为 JSON 对象')
        output={}
        for key,value in raw.items():
            if not isinstance(key,str) or not re.fullmatch(r'.+:(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})',key):
                raise ProbeError('旧任务来源补录的键必须为 下载器名:hash')
            entry={'url':value} if isinstance(value,str) else value
            if not isinstance(entry,dict) or set(entry)-{'url','site','site_name','hit_and_run','first_seen_at'}:
                raise ProbeError('旧任务来源补录仅接受来源信息，不能手工设置 HR 达标状态')
            url=canonical_source_url(entry.get('url'))
            flag=entry.get('hit_and_run');site=entry.get('site');first=entry.get('first_seen_at',0)
            if not url or (flag is not None and not isinstance(flag,bool)) or (site is not None and (not isinstance(site,int) or isinstance(site,bool))):
                raise ProbeError('旧任务来源补录内容无效')
            if not isinstance(first,(float,int)) or isinstance(first,bool) or not math.isfinite(first) or first<0:
                raise ProbeError('旧任务来源补录时间无效')
            client,h=key.rsplit(':',1);key=client+':'+h.lower()
            item=TorrentHistory(h.lower(),client,url,torrent_id(url),site,str(entry.get('site_name') or ''),flag,float(first)).to_dict()
            output.setdefault(key,[]).append(item)
        return output

    def _source_records(self,task):
        key=task.client+':'+task.hash.lower()
        saved=self.get_data('source:'+key) or {}
        return saved,list(saved.get('histories',{}).values())+self._configured_sources().get(key,[])

    def _source_summary(self,task):
        _,records=self._source_records(task)
        flags={r.get('hit_and_run') for r in records}
        flag=True if True in flags else (False if flags=={False} else None)
        urls={r.get('page_url') for r in records if r.get('page_url')}
        return (next(iter(urls)) if len(urls)==1 else ''),flag

    def _source_for(self,task,client=None,fallback=''):
        key=task.client+':'+task.hash.lower()
        urls={canonical_source_url(client.source(task)) if client else '',canonical_source_url(fallback)}
        saved,records=self._source_records(task)
        urls.update(canonical_source_url(x) for x in saved.get('urls',[]))
        urls.update(canonical_source_url(x.get('page_url')) for x in records)
        urls.discard('')
        if len(urls)>1:raise ProbeError('种子来源记录存在冲突，无法确认 HR 对应关系')
        return next(iter(urls),'')

    def _refresh_hr(self,plan,job,stop):
        if plan.get('role_policy')!=ROLE_POLICY:
            raise ProbeError('旧清理计划需重新核验原始下载身份，不能自动恢复删除')
        expected={x['client']+':'+x['hash'] for x in plan['owners']}
        recorded={x.get('owner'):x for x in plan.get('hr',[])}
        if set(recorded)!=expected:raise ProbeError('清理计划缺少完整的关联种子 HR 证据')
        provider=NexusHr(stop=stop, selected_sites=self._config['sites']);fresh=[]
        for item in plan['originals']+plan.get('auxiliaries',[]):
            if stop.is_set():raise ProbeError('巡检已停止')
            key=item['client']+':'+item['hash']
            task=SimpleNamespace(client=item['client'],hash=item['hash'])
            url=self._source_for(task,fallback=recorded[key].get('source_url',''))
            role='original' if item in plan['originals'] else 'auxiliary'
            fresh.append({**hr_clearance(key,url,provider.check(url)),'role':role})
            if role=='original' and fresh[-1]['state'] not in ALLOWED_HR:break
        job['last_revalidation']={'checked_at':time.time(),'hr':fresh}
        valid=all(x['state'] in ALLOWED_HR and x.get('proof_version')==PROOF_VERSION and x.get('basis') in
            {'site_percentage_and_seed_time','site_personal_view','site_waiver_view'} for x in fresh)
        if not valid:
            job['inspection']={**plan,'ready':False,'hr':fresh,'reason':'清理前 HR 复核未通过'}
            job['personal_hr_state']=next((x['state'] for x in fresh if x['role']=='original' and x['state'] not in ALLOWED_HR),'complete')
            self._save()
            raise ProbeError('清理前站点 HR 复核未通过，继续保留')
        plan['hr']=fresh;job['inspection']=plan
        job['personal_hr_state']='no_hr' if all(x['state']=='no_hr' for x in fresh) else 'complete'
        self._save()

    @staticmethod
    def get_command():
        return []

    def get_api(self):
        # The standard plugin form provides the one-shot switch. No extra
        # destructive HTTP endpoint is exposed to ordinary logged-in users.
        return []

    def get_service(self):
        services = []
        if self.get_state():
            services.append({'id':self.__class__.__name__+'.Check','name':'云端备份后清理巡检',
                'trigger':self._interval_trigger,'func':self.run,'kwargs':{}})
        if self._once:
            services.append({'id':self.__class__.__name__+'.Once','name':'云端备份核查一次',
                'trigger':DateTrigger(run_date=datetime.now(timezone.utc)+timedelta(seconds=3)),
                'func':self.run,'kwargs':{},'func_kwargs':{'force':True,'generation':self._revision}})
        for service in services:
            service.setdefault('func_kwargs',{}).setdefault('generation',self._revision)
        return services

    def stop_service(self):
        self._stop.set()
        if not self._lock.acquire(timeout=30):
            raise RuntimeError('巡检尚未退出，已禁止后续清理动作')
        self._lock.release()

    def _save(self):
        self.save_data('state',self._state)

    def _options(self):
        result = dict(self._config)
        result['allowed_roots'] = [x.strip() for x in result['allowed_roots'].splitlines() if x.strip()]
        result['mappings'] = json.loads(result['mappings'])
        self._configured_sources()
        if not result['allowed_roots'] or not isinstance(result['mappings'],list) or not result['mappings']:
            raise ProbeError('请配置本地允许目录和115路径映射')
        for root in result['allowed_roots']:
            if not Path(root).is_absolute() or root=='/' or '..' in Path(root).parts:
                raise ProbeError('允许目录必须为明确的本地绝对路径，不能是根目录')
        for mapping in result['mappings']:
            if not isinstance(mapping,dict) or not all(isinstance(mapping.get(k),str) and Path(mapping[k]).is_absolute() for k in ('local','cloud')):
                raise ProbeError('路径映射格式无效')
            if not any(Path(mapping['local']).is_relative_to(Path(root)) for root in result['allowed_roots']):
                raise ProbeError('映射的本地目录不在允许目录中')
        result['absence_confirmed_sites'] = [x.strip().lower() for x in result['absence_confirmed_sites'].splitlines() if x.strip()]
        return result

    @staticmethod
    def _find_transfers(source):
        records = []
        for page in range(1,26):
            result = list_transfer_history(filters={'src':source,'status':True},
                page={'page':page,'count':200,'sort':{'field':'id','direction':'desc'}})
            records.extend(result.items)
            if not result.has_next:
                return records
        raise ProbeError('整理记录超过单次读取上限，无法确认完整关联')

    def _discover(self,tasks):
        wanted = {x.strip().lower() for x in str(self._config.get('hashes','')).splitlines() if x.strip()}
        candidates = [t for t in tasks if not wanted or t.hash.lower() in wanted]
        candidates.sort(key=lambda t:(t.completed_at,t.hash,t.client),reverse=True)
        # Probe a bounded rotating portion of current download tasks, never a
        # destructive scan of arbitrary filesystem or disappeared old tasks.
        cursor = self._state.get('discovery_cursor',0) % max(1,len(candidates))
        ordered = candidates[cursor:] + candidates[:cursor]
        for task in ordered[:100]:
            if self._role_for(task).get('role')!='original':continue
            ident = task.client+':'+task.hash+':'+str(task.generation)
            if ident in self._state['jobs']:
                continue
            result = list_transfer_history(filters={'download_hash':task.hash,'status':True},page={'page':1,'count':1})
            records=result.items
            if not records:
                # MP manual transfers can have no download_hash. Discovery may
                # use the current torrent's exact source path; plan_group still
                # verifies every selected file and its hardlink identity.
                source=next((p for p in task.wanted if Path(p).suffix.lower() in VIDEO),None)
                if source:
                    records=list_transfer_history(filters={'src':source,'status':True},page={'page':1,'count':1}).items
            if records:
                url,flag=self._source_summary(task)
                self._state['jobs'][ident]=TorrentTask(task.hash,task.client,url,torrent_id(url),
                    hit_and_run=flag,time=time.time(),generation=task.generation,
                    title=records[0].title or task.hash[:12]).to_job()
        self._state['discovery_cursor']=(cursor+min(100,len(candidates))) % max(1,len(candidates))

    def run(self,force=False,generation=None):
        if self._stop.is_set() or (generation is not None and generation!=self._revision):
            return
        if not force and not self.get_state():
            return
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._stop.is_set() or (generation is not None and generation!=self._revision):
                return
            run_stop=self._stop
            if force:
                self._once=False
            self._state['last_started']=time.time();self._state['running']=True;self._save()
            config=self._options()
            clients=from_mp(stop=run_stop)
            if not clients:
                raise ProbeError('MP 没有启用的下载器')
            def load_tasks():
                values=[]
                for client in clients.values():
                    values.extend(client.tasks())
                return values
            tasks=load_tasks()
            self._discover(tasks)
            self._save()
            cloud_factory=lambda:CloudDrive(config['cd2_url'],config['cd2_token'],stop=run_stop)
            cms=CmsIndex(config['cms_database'],config['strm_root'],config['cms_url'],config['cloud_root'])
            hr=NexusHr(stop=run_stop, selected_sites=config['sites'])
            interval=max(5,int(config['interval']))*60
            allowed_hashes={x.strip().lower() for x in str(config.get('hashes','')).splitlines() if x.strip()}
            # The scheduler is the timing authority. A per-job "now+interval"
            # gate can fall a few seconds after the next tick and accidentally
            # skip a whole cycle; rotate oldest-checked pending jobs instead.
            due=sorted(((key,j) for key,j in self._state['jobs'].items() if not j.get('completed_at') and
                (not allowed_hashes or j['hash'].lower() in allowed_hashes)),key=lambda x:x[1].get('checked_at') or 0)
            checked=0;handled=set()
            for key,job in due:
                if run_stop.is_set() or checked>=max(1,min(50,int(config['batch_size']))):
                    break
                if job['hash'] in handled:
                    continue
                group=group_for(tasks,job['hash']);handled.update(t.hash for t in group)
                job['group_count']=len(group)
                checked+=1;job['checked_at']=time.time();job['next_check']=time.time()+interval
                try:
                    if job.get('client'):
                        job['source_url'],job['original_hit_and_run']=self._source_summary(SimpleNamespace(client=job['client'],hash=job['hash']))
                    if job.get('inspection'):job['previous_inspection']=job['inspection']
                    job['inspection']={};job['personal_hr_state']='unknown'
                    if job.get('journal') and job['journal'].get('remove_requested'):
                        if not config.get('auto_delete'):
                            job['status']='清理暂停：当前为只读模式';continue
                        plan=job['plan']
                    else:
                        plan=plan_group(job['hash'],tasks,clients,self._find_transfers,cloud_factory,cms,hr,config,self._state['hash_cache'],run_stop,source_lookup=lambda task:self._source_for(task,clients[task.client]),role_lookup=self._role_for)
                        job['status']=plan['reason'];job['inspection']=plan
                        states=[x['state'] for x in plan.get('hr',[]) if x.get('role','original')=='original']
                        cleared_state='no_hr' if states and all(state=='no_hr' for state in states) else 'complete'
                        job['personal_hr_state']=next((state for state in ('overdue','incomplete','unknown') if state in states),cleared_state if states and all(state in ALLOWED_HR for state in states) else 'unknown')
                        job['requires_hr_refresh']=False
                        if not plan['ready']:
                            continue
                        check_scope(plan,allowed_hashes)
                        if not config.get('auto_delete'):
                            job['status']='可清理（只读核查）';continue
                        job['plan']=plan;job['journal']={};self._save()
                    def verify_cloud(p):
                        check_delete_observer(p['paths'],self.get_config('RemoveLink'))
                        cloud=cloud_factory();blocked=cloud.uploads();cloud.assert_keep_cloud(p['logical_paths'])
                        for e in p['evidence']:
                            remote=cloud.get_file(e['remote_path'])
                            if e['remote_path'] in blocked or not remote or (remote.id,remote.size,remote.sha1)!=(e['remote_id'],e['size'],e['sha1']):
                                raise ProbeError('清理前云端文件状态变化')
                            cms.verify(remote,e['sha1'])
                    execute_plan(plan,job['journal'],clients,load_tasks,verify_cloud,config['allowed_roots'],self._save,run_stop,allowed_hashes,
                        verify_hr=lambda p:self._refresh_hr(p,job,run_stop),role_lookup=self._role_for)
                    job['status']='已清理核验通过的视频';job['completed_at']=time.time()
                    for other in self._state['jobs'].values():
                        if other is not job and any(other.get('client')==x['client'] and other['hash']==x['hash'] and other.get('generation')==x['generation'] for x in plan['owners']):
                            other['status']='已随关联种子清理';other['completed_at']=job['completed_at']
                    tasks=load_tasks()
                except ProbeError as e:
                    job['status']=str(e)
                except Exception as e:
                    job['status']='巡检失败：'+type(e).__name__
                finally:
                    next_fire=self._interval_trigger.get_next_fire_time(None,datetime.now(timezone.utc)+timedelta(milliseconds=1))
                    if next_fire and self.get_state() and not job.get('completed_at'):
                        job['next_check']=next_fire.timestamp()
                    elif not self.get_state():
                        job['next_check']=None
                    self._save()
            self._state['last_error']=''
            self._state['last_checked_count']=checked
            logger.info(f'云端备份后清理：本轮核查 {checked} 组，模式：'+('自动清理' if config.get('auto_delete') else '只读核查'))
        except ProbeError as e:
            self._state['last_error']=str(e)
        except Exception as e:
            self._state['last_error']='巡检初始化失败：'+type(e).__name__
        finally:
            try:
                self._state['running']=False;self._state['last_finished']=time.time();self._save()
            finally:
                self._lock.release()

    @staticmethod
    def _site_options():
        try:
            from app.db.oper.site import SiteOper
            return [{'title':str(site.name),'value':site.id,'domain':str(site.domain)}
                    for site in SiteOper().list()]
        except Exception:
            logger.warn('读取 MP 站点列表失败；站点未选择时保留文件')
            return []

    def get_form(self):
        options=self._site_options()
        known={x['value'] for x in options}
        # Keep removed IDs visible so a save cannot silently discard selection.
        options.extend({'title':f'站点 {sid}（MP 中已移除）','value':sid}
                       for sid in self._config.get('sites',[]) if sid not in known)
        return ui.form([{'title':x['title'],'value':x['value']} for x in options]),dict(DEFAULTS)

    def get_page(self):
        snapshot=self.get_data('state') or self._state
        next_fire=self._interval_trigger.get_next_fire_time(None,datetime.now(timezone.utc)) if self.get_state() and self._interval_trigger else None
        names={x['domain']:x['title'] for x in self._site_options()}
        return ui.page(snapshot,self._config,next_fire.timestamp() if next_fire else None,
                       Path(self._config['cms_database']).is_file(),names)
