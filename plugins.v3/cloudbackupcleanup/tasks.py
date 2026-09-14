"""Adapted H&R task/history design from InfinityPacer's H&R assistant.

Upstream: a009a8c7031fe989cd4d3c8c421fe6f7396ca3de, GPL-3.0.
See THIRD_PARTY_NOTICES.md and the repository LICENSE. The reduced dataclasses
retain source identity separately from personal clearance; no elapsed-time rule
from the upstream downloader helper authorizes cleanup here.
"""
from dataclasses import asdict, dataclass
from enum import Enum


class HNRStatus(str, Enum):
    PENDING = 'unknown'
    IN_PROGRESS = 'incomplete'
    COMPLIANT = 'complete'
    UNRESTRICTED = 'no_hr'
    OVERDUE = 'overdue'


@dataclass(frozen=True)
class TorrentHistory:
    hash: str
    downloader: str
    page_url: str
    torrent_id: str
    site: int | None = None
    site_name: str = ''
    hit_and_run: bool | None = None
    time: float = 0

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class TorrentTask(TorrentHistory):
    generation: int = 0
    title: str = ''
    hr_status: HNRStatus = HNRStatus.PENDING

    def to_job(self):
        return {'hash': self.hash, 'client': self.downloader,
                'generation': self.generation, 'title': self.title,
                'source_url': self.page_url, 'original_hit_and_run': self.hit_and_run,
                'personal_hr_state': self.hr_status.value, 'status': '等待巡检',
                'next_check': 0, 'created_at': self.time}


def migrate_state(state):
    """Preserve audit/journal/hash data while retiring pre-v3 HR clearances."""
    if state.get('hr_schema_version', 0) >= 3:
        return state
    for job in state.get('jobs', {}).values():
        if job.get('completed_at'):
            continue
        if job.get('inspection'):
            job['previous_inspection'] = job['inspection']
            job['inspection'] = {}
        job['personal_hr_state'] = HNRStatus.PENDING.value
        job['status'] = '等待 HR 重新核验'
        job['requires_hr_refresh'] = True
    state['hr_schema_version'] = 3
    return state
