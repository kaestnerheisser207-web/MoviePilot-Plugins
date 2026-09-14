"""H&R Assistant rule calculation using actual downloader counters.

Adapted comparison semantics: seeding time OR ratio must exceed the configured
threshold. Unlike the upstream TR helper, wall-clock time is never seeding time.
"""
import math,time
from .hr import HrResult


def number(value,minimum=0):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<minimum:
        return None
    return float(value)


def calculate(task,rule,site_name,original_reason):
    now=time.time();duration=number(rule.get('hr_duration'));extra=number(rule.get('additional_seed_time'))
    ratio_limit=number(rule.get('hr_ratio'));seeded=number(getattr(task,'seed_seconds',None))
    generation=number(getattr(task,'generation',None),1)
    if duration is None or duration<=0 or extra is None or ratio_limit is None or ratio_limit<=0 or generation is None:
        return HrResult('unknown','计算规则或任务身份不完整，继续保留',now)
    ratio=number(getattr(task,'ratio',None)) if number(getattr(task,'downloaded_bytes',None),1) is not None else None
    required=(duration+extra)*3600
    time_met=seeded is not None and seeded>required
    ratio_met=ratio is not None and ratio>ratio_limit
    if seeded is None and ratio is None:
        return HrResult('unknown','下载器未提供实际做种时长或有效分享率',now)
    complete=time_met or ratio_met
    reason=f'{site_name}：按 H&R 助手规则计算'+('达标' if complete else '未达标')+f'（做种 > {duration+extra:g} 小时，或分享率 > {ratio_limit:g}）'
    result=HrResult('complete' if complete else 'incomplete',reason,now,
        required_seed_seconds=int(required),seeded_seconds=int(seeded) if seeded is not None else None,
        remaining_seed_seconds=max(0,int(required-seeded)+1) if seeded is not None and not complete else (0 if complete else None),
        basis='downloader_rule_calculation',station_reason=original_reason,
        calculation={'rule':dict(rule),'site_name':site_name,'seed_seconds':seeded,'ratio':ratio,
                     'task_generation':int(generation),'time_met':time_met,'ratio_met':ratio_met})
    return result
