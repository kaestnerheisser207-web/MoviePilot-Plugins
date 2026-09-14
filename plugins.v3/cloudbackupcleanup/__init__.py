"""MoviePilot V3 plugin: verify cloud backup, CMS and HR before local cleanup."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from app.plugins import _PluginBase
from app.sdk.logging import logger
from app.sdk.queries import list_transfer_history

from .cloud import CloudDrive, ProbeError
from .cms import CmsIndex
from .downloaders import from_mp
from .engine import VIDEO, check_delete_observer, check_scope, execute_plan, group_for, plan_group
from .hr import NexusHr


DEFAULTS = {
    'enabled':False, 'auto_delete':False, 'run_once':False, 'interval':30,
    'cd2_url':'', 'cd2_token':'', 'cms_url':'', 'cms_database':'/cms-index/cms-online.db',
    'strm_root':'/video/cloud-media', 'cloud_root':'/115open',
    'allowed_roots':'/video/btdownloads\n/video/movie\n/video/tv',
    'mappings':json.dumps([{'local':'/video/movie','cloud':'/115open/电影'},
                           {'local':'/video/tv','cloud':'/115open/电视剧'}],ensure_ascii=False,indent=2),
    'absence_confirmed_sites':'', 'hashes':'', 'batch_size':5, 'hash_mib_s':16,
}


class CloudBackupCleanup(_PluginBase):
    plugin_name = '云端备份后清理'
    plugin_desc = '定期核验115备份、CMS同步及HR状态，默认只读核查。'
    plugin_icon = 'CloudDrive_A.png'
    plugin_version = '0.1.2'
    plugin_author = 'kaestnerheisser207-web'
    author_url = 'https://github.com/kaestnerheisser207-web'
    plugin_config_prefix = 'cloudbackupcleanup_'
    plugin_order = 50
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
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
            for switch in ('enabled','auto_delete','run_once'):
                self._config[switch] = self._config.get(switch) is True
            self._once = self._config['run_once']
            if self._once:
                self._config['run_once'] = False
                self.update_config(self._config)
            self._state = self.get_data('state') or {'jobs':{},'hash_cache':{},'discovery_cursor':0}
            self._state.setdefault('jobs',{})
            self._state.setdefault('hash_cache',{})
            self._revision += 1
            self._stop = threading.Event()
            self._interval_trigger=IntervalTrigger(minutes=max(5,int(self._config['interval'])))
        finally:
            self._lock.release()

    def get_state(self):
        return bool(self._config.get('enabled'))

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
                self._state['jobs'][ident]={'hash':task.hash,'title':records[0].title or task.hash[:12],
                    'client':task.client,'generation':task.generation,'status':'等待巡检','next_check':0,'created_at':time.time()}
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
            hr=NexusHr(config['absence_confirmed_sites'])
            interval=max(5,int(config['interval']))*60
            allowed_hashes={x.strip().lower() for x in str(config.get('hashes','')).splitlines() if x.strip()}
            # The scheduler is the timing authority. A per-job "now+interval"
            # gate can fall a few seconds after the next tick and accidentally
            # skip a whole cycle; rotate oldest-checked pending jobs instead.
            due=sorted(((key,j) for key,j in self._state['jobs'].items() if not j.get('completed_at')),key=lambda x:x[1].get('checked_at',0))
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
                    if job.get('journal') and job['journal'].get('remove_requested'):
                        if not config.get('auto_delete'):
                            job['status']='清理暂停：当前为只读模式';continue
                        plan=job['plan']
                    else:
                        plan=plan_group(job['hash'],tasks,clients,self._find_transfers,cloud_factory,cms,hr,config,self._state['hash_cache'],run_stop)
                        job['status']=plan['reason'];job['inspection']=plan
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
                    execute_plan(plan,job['journal'],clients,load_tasks,verify_cloud,config['allowed_roots'],self._save,run_stop,allowed_hashes)
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

    def get_form(self):
        def item(component,model,label,**props):
            return {'component':component,'props':{'model':model,'label':label,**props}}
        controls=[
            item('VSwitch','enabled','启用周期巡检'),item('VSwitch','auto_delete','自动清理本地视频'),
            {'component':'VAlert','props':{'type':'warning','variant':'tonal','text':'默认只读。开启自动清理后，只有备份、CMS和关联种子的HR全部确认，才删除下载任务及核验过的视频硬链接。'}},
            item('VSwitch','run_once','保存后巡检一次'),item('VTextField','interval','巡检间隔（分钟）',type='number'),
            item('VTextField','cd2_url','CD2 地址'),item('VTextField','cd2_token','CD2 只读 API 令牌',type='password'),
            item('VTextField','cms_url','CMS 地址'),item('VTextField','cms_database','CMS 只读索引路径'),
            item('VTextField','strm_root','CMS STRM 本地目录'),item('VTextField','cloud_root','CD2 中的115根目录'),
            item('VTextarea','allowed_roots','允许清理的本地目录（每行一个）',rows=3),
            item('VTextarea','mappings','本地到115的路径映射（JSON）',rows=6),
            item('VTextarea','hashes','限定种子 hash（留空检查当前任务）',rows=2),
            item('VTextField','batch_size','每轮最多核查组数',type='number'),
            item('VTextField','hash_mib_s','哈希读取上限（MiB/s）',type='number'),
            item('VTextarea','absence_confirmed_sites','已核实“完整HR列表无记录即无HR”的站点域名',rows=2),
            {'component':'VAlert','props':{'type':'info','variant':'tonal','text':'未确认站点规则时请保留为空。页面无记录、查询失败、标签缺失都不会自动放行。'}}]
        return [{'component':'VForm','content':controls}],dict(DEFAULTS)

    def get_page(self):
        def label(value):
            return str(value).replace('{{','｛｛').replace('}}','｝｝')
        rows=[]
        snapshot=self.get_data('state') or self._state
        jobs=snapshot.get('jobs',{})
        for job in sorted(jobs.values(),key=lambda j:j.get('checked_at',0),reverse=True)[:200]:
            when=job.get('next_check')
            hr_items=job.get('inspection',{}).get('hr',[])
            cells=[{'component':'td','text':label(x)} for x in (
                job['title'],job['hash'][:12],job.get('group_count',len(hr_items) or '—'),job.get('status','等待巡检'),
                datetime.fromtimestamp(when).strftime('%m-%d %H:%M') if when and self.get_state() and not job.get('completed_at') else '—')]
            details=[]
            for item in hr_items:
                name=(item.get('site') or '来源未知')+' · '+item.get('owner','').rsplit(':',1)[-1][:8]
                stamp=datetime.fromtimestamp(item['checked_at']).strftime('%m-%d %H:%M') if item.get('checked_at') else ''
                details.append({'component':'li','text':label(name+'：'+item.get('reason','')+' '+stamp)})
            cells.append({'component':'td','content':[{'component':'ul','content':details}]} if details else {'component':'td','text':'尚未检查'})
            rows.append({'component':'tr','content':cells})
        mode='自动清理' if self._config.get('auto_delete') else '只读核查'
        result=[{'component':'VAlert','props':{'type':'info','variant':'tonal','text':f'{mode} · 共 {len(jobs)} 条记录'+(' · 巡检中' if snapshot.get('running') else '')}}]
        index_available=Path(self._config['cms_database']).is_file()
        result.append({'component':'VAlert','props':{'type':'info' if index_available else 'warning','variant':'tonal',
            'text':'CMS只读索引：'+('已接入' if index_available else '尚未接入，请检查只读挂载')}})
        if snapshot.get('last_error'):
            result.append({'component':'VAlert','props':{'type':'error','text':label(snapshot['last_error'])}})
        result.append({'component':'VTable','content':[
            {'component':'thead','content':[{'component':'tr','content':[{'component':'th','text':x} for x in ('资源','种子','关联数','当前状态','预计复查','最近HR核验')]}]},
            {'component':'tbody','content':rows}]})
        return result
