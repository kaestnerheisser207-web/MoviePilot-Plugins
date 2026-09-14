"""Separate MP download provenance from explicitly labelled cross-seeds."""
from .cloud import ProbeError

ROLE_POLICY = 'original_then_auxiliary_personal_hr_v1'


def classify(task, histories, captured=False):
    if any(str(getattr(r,'download_hash','')).lower()==task.hash.lower() for r in histories):
        return {'role':'original','basis':'mp_download_history'}
    if captured:
        return {'role':'original','basis':'download_source_record'}
    # Zero downloaded bytes alone is insufficient: IYUU moves reset that counter.
    if 'IYUU自动辅种' in getattr(task,'labels',()) and getattr(task,'downloaded_bytes',None)==0:
        return {'role':'auxiliary','basis':'iyuu_cross_seed_label_and_zero_download'}
    return {'role':'unknown','basis':'缺少 MP 下载来源或明确辅种证据'}


def partition(group, lookup):
    result={'original':[],'auxiliary':[],'unknown':[]}
    for task in group:
        proof=lookup(task)
        if proof.get('role') not in result:raise ProbeError('任务角色证据无效')
        result[proof['role']].append({'client':task.client,'hash':task.hash,'generation':task.generation,
            'wanted':list(task.wanted),**proof})
    return result
