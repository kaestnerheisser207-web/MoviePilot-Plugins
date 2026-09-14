"""Native MoviePilot/Vuetify forms and readable inspection cards."""
from datetime import datetime


def node(component, text=None, content=None, **props):
    result={'component':component}
    if props:result['props']=props
    if text is not None:result['text']=str(text).replace('{{','｛｛').replace('}}','｝｝')
    if content is not None:result['content']=content
    return result


def field(model,label,hint='',component='VTextField',**props):
    return node(component,model=model,label=label,variant='outlined',density='comfortable',
                hint=hint,**{'persistent-hint':bool(hint)},**props)


def col(child,md=6):
    return node('VCol',content=[child],cols=12,md=md)


def section(title,description,columns):
    return node('VCard',variant='outlined',**{'class':'mb-5 rounded-lg'},content=[
        node('VCardText',content=[node('div',title,**{'class':'text-subtitle-1 font-weight-bold mb-1'}),
            node('div',description,**{'class':'text-body-2 text-medium-emphasis mb-6'}),
            node('VRow',content=columns)])])


def form(site_options):
    switches=[col(node('VSwitch',model=model,label=label,color='primary',
        hint=hint,**{'persistent-hint':True}),4) for model,label,hint in [
        ('enabled','启用周期巡检','按设定间隔检查备份与 HR'),
        ('auto_delete','自动清理本地视频','关闭时只核查并展示结果'),
        ('run_once','保存后巡检一次','执行一次后自动关闭')]]
    basic=section('巡检设置','下载、上传和 CMS 同步照常进行；备份核验通过后才查询个人 HR。',switches+[
        col(field('sites','参与 HR 核验的站点','读取 MP 已配置站点；未勾选站点的关联种子继续保留。',
            component='VSelect',items=site_options,multiple=True,chips=True,**{'closable-chips':True,'clearable':True}),12),
        col(field('interval','巡检间隔（分钟）','最少 5 分钟',type='number',min=5),4),
        col(field('batch_size','每轮核查组数','按共享文件归为一组',type='number',min=1),4),
        col(field('hash_mib_s','哈希读取上限（MiB/s）','限制本地文件读取速度',type='number',min=1),4)])
    storage=section('云端与媒体库','使用现有 CD2 和 CMS 连接，核验 115 文件与 STRM。',[
        col(field('cd2_url','CD2 地址',placeholder='http://192.168.x.x:19798')),
        col(field('cd2_token','CD2 只读 API 令牌',type='password',autocomplete='off')),
        col(field('cms_url','CMS 地址',placeholder='http://192.168.x.x:9527')),
        col(field('cms_database','CMS 只读索引路径')),
        col(field('cloud_root','115 根目录')),
        col(field('strm_root','CMS STRM 本地目录'))])
    paths=section('目录与核查范围','保留现有路径映射和限定种子范围。',[
        col(field('allowed_roots','允许清理的本地目录','每行一个绝对路径',component='VTextarea',rows=3)),
        col(field('hashes','限定种子 hash','每行一个；留空会检查下载器当前任务',component='VTextarea',rows=3)),
        col(field('mappings','本地 → 115 路径映射','JSON 数组，每项包含 local 和 cloud',component='VTextarea',rows=5),12)])
    advanced=node('VExpansionPanels',variant='accordion',**{'class':'mb-5'},content=[
        node('VExpansionPanel',content=[node('VExpansionPanelTitle','高级：旧任务来源补录'),
            node('VExpansionPanelText',content=[field('source_mappings','来源映射（JSON）',
                '仅用于缺少下载来源的旧任务，不可手工指定 HR 达标状态。',component='VTextarea',rows=5)])])])
    return [node('VForm',content=[basic,storage,paths,advanced,
        node('div','仅接受站点确认：无记录、查询失败或 HR 未达标时保留文件；共享文件的所有关联种子均须通过。',
             **{'class':'text-body-2 text-medium-emphasis mb-3'})])]


STATES={'unknown':('待核验','secondary'),'incomplete':('HR 未达标','warning'),
        'complete':('HR 已达标','success'),'no_hr':('无 HR 考核','success'),'overdue':('HR 逾期','error')}


def stamp(value):
    return datetime.fromtimestamp(value).strftime('%m-%d %H:%M') if value else '尚未检查'


def duration(seconds):
    hours,rest=divmod(max(0,int(seconds)),3600)
    return f'{hours} 小时 {rest//60} 分钟'


def chip(text,color='secondary'):
    return node('VChip',text,color=color,size='small',variant='tonal',**{'class':'mr-2 mb-2'})


def fact(label,value):
    return col(node('div',content=[node('div',label,**{'class':'text-caption text-medium-emphasis mb-1'}),
        node('div',value,**{'class':'text-body-2','style':'overflow-wrap:anywhere'})]),4)


def page(snapshot,config,next_check,index_available,site_names):
    jobs=snapshot.get('jobs',{})
    summary=[chip('自动清理' if config.get('auto_delete') else '只读核查','warning' if config.get('auto_delete') else 'primary'),
        chip(f'{len(jobs)} 个资源'),chip('CMS 已接入' if index_available else 'CMS 未接入','success' if index_available else 'warning'),
        chip('巡检中' if snapshot.get('running') else ('周期巡检已启用' if config.get('enabled') else '周期巡检已关闭'))]
    result=[node('div',content=summary,**{'class':'d-flex flex-wrap mb-4'})]
    if snapshot.get('last_error'):
        result.append(node('VAlert',snapshot['last_error'],type='error',variant='tonal',**{'class':'mb-4'}))
    if not jobs:result.append(node('div','尚无核查记录。配置完成后，可保存并巡检一次。',**{'class':'py-8 text-center text-medium-emphasis'}))
    for job in sorted(jobs.values(),key=lambda j:j.get('checked_at',0),reverse=True)[:200]:
        inspection=job.get('inspection') or {};items=inspection.get('hr',[])
        state,color=STATES.get(job.get('personal_hr_state'),STATES['unknown'])
        original='有 HR' if job.get('original_hit_and_run') is True else ('标记为无 HR' if job.get('original_hit_and_run') is False else '未知')
        remaining=[x['remaining_seed_seconds'] for x in items if x.get('remaining_seed_seconds') is not None]
        facts=[fact('关联种子',str(job.get('group_count',len(items) or 1))+' 个 · 原始标记 '+original),
            fact('最近检查',stamp(job.get('checked_at'))),
            fact('下次复查',stamp(next_check) if next_check and not job.get('completed_at') else '—')]
        if remaining:facts.append(fact('还需做种',duration(max(remaining))))
        deadlines=[x['deadline_at'] for x in items if x.get('deadline_at')]
        if deadlines:facts.append(fact('预计截止（站点倒计时）',stamp(min(deadlines))))
        details=[]
        for item in items:
            host=item.get('site') or '来源未知';name=site_names.get(host,host)
            label,c=STATES.get(item.get('state'),STATES['unknown'])
            body=[node('div',content=[chip(name,'primary'),chip(label,c)]),
                node('div',item.get('reason') or '尚无判断依据',**{'class':'text-body-2 mb-2','style':'overflow-wrap:anywhere'})]
            timing=[]
            if item.get('remaining_seed_seconds') is not None:timing.append('还需做种 '+duration(item['remaining_seed_seconds']))
            elif item.get('remaining_seed_text'):timing.append('站点剩余做种 '+item['remaining_seed_text'])
            if item.get('deadline_at'):timing.append('预计截止 '+stamp(item['deadline_at'])+'（站点倒计时估算）')
            if timing:body.append(node('div',' · '.join(timing),**{'class':'text-body-2 mb-2'}))
            body.append(node('div','核验 '+stamp(item.get('checked_at'))+' · 种子 '+item.get('owner','').rsplit(':',1)[-1][:12],**{'class':'text-caption text-medium-emphasis'}))
            details.append(node('div',content=body,**{'class':'py-3','style':'border-top:1px solid rgba(var(--v-border-color),var(--v-border-opacity))'}))
        content=[node('div',content=[node('div',job.get('title') or job.get('hash','')[:12],
                    **{'class':'text-h6 mr-4','style':'overflow-wrap:anywhere;white-space:normal;flex:1;min-width:0'}),chip(state,color)],
                    **{'class':'d-flex align-start mb-3'}),
            node('div',job.get('status','等待巡检'),**{'class':'text-body-2 text-medium-emphasis mb-5','style':'overflow-wrap:anywhere'}),
            node('VRow',content=facts)]
        if details:
            content.append(node('VExpansionPanels',variant='accordion',**{'class':'mt-5'},content=[
                node('VExpansionPanel',elevation=0,content=[node('VExpansionPanelTitle',f'站点核验详情 · {len(items)} 个种子'),
                    node('VExpansionPanelText',content=details)])]))
        else:content.append(node('div','备份核验通过后显示个人 HR 结果',**{'class':'text-caption text-medium-emphasis mt-4'}))
        result.append(node('VCard',variant='outlined',**{'class':'mb-4 rounded-lg'},content=[node('VCardText',content=content)]))
    return result
