"""Station-confirmed HR results using the pinned CHDBits H&R Monitor parser.

The vendored parser owns table extraction. This module supplies column mappings,
read-only navigation and clearance policy; client timers never authorize deletion.
"""
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup
from .cloud import ProbeError
from .vendor.chd_hnr.config import ParserConfig
from .vendor.chd_hnr import parser as tables

PROOF_VERSION = 3
VIEWS = {'考核中', '已达标', '未达标', '全部', '已免罪'}


@dataclass(frozen=True)
class HrResult:
    state: str  # unknown, no_hr, incomplete, complete, overdue
    reason: str
    checked_at: float
    required_seed_seconds: int | None = None
    seeded_seconds: int | None = None
    remaining_seed_seconds: int | None = None
    deadline_at: float | None = None
    deadline_text: str = ''
    basis: str = ''
    proof_version: int = PROOF_VERSION
    remaining_seed_text: str = ''


def torrent_id(url):
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        value = (q.get('id') or q.get('torrentid') or [''])[0]
        return value if value.isdigit() else ''
    except (ValueError, TypeError):
        return ''


def duration_seconds(value):
    """Normalize station duration text; a two-part clock means minutes:seconds."""
    value = re.sub(r'\s+', '', value)
    if value == '0':
        return 0
    match = re.fullmatch(r'(?:(\d+)天)?(?:(\d+(?::\d{1,2}){1,2}))?', value)
    if match and any(match.groups()):
        days, clock = int(match[1] or 0), match[2]
        if not clock:
            return days * 86400
        parts = [int(x) for x in clock.split(':')]
        hours, minutes, seconds = (0, *parts) if len(parts) == 2 else parts
        if seconds >= 60 or (len(parts) == 3 and minutes >= 60) or (days and hours >= 24):
            raise ProbeError('站点 HR 时间字段无效')
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    units = re.fullmatch(r'(?:(\d+)天)?(?:(\d+)(?:小时|时))?(?:(\d+)(?:分钟|分))?(?:(\d+)秒)?', value)
    if units and any(units.groups()):
        return sum(int(v or 0) * mul for v, mul in zip(units.groups(), (86400, 3600, 60, 1)))
    raise ProbeError('站点 HR 时间格式无法确认')


def chd_duration(value):
    # Compatibility with existing callers and already verified CHD fixtures.
    return duration_seconds(value)


def _soup(html):
    soup = BeautifulSoup(html, 'html.parser')
    if soup.select('input[type=password]'):
        raise ProbeError('站点会话已失效')
    return soup


def _mapped_rows(html, required):
    """Use upstream table/record extraction with exact, validated column names.

    Avoid upstream's name-derived fallback IDs and first-record deduplication:
    destructive consumers must preserve conflicting records and real torrent IDs.
    """
    _soup(html)
    parser = tables.TableHTMLParser()
    parser.feed(html)
    parser.close()
    candidates = []
    for table in parser.tables:
        for index, row in enumerate(table.rows):
            headers = row.texts
            if all(name in headers for name in required):
                if len(set(headers)) != len(headers):
                    raise ProbeError('个人 HR 表头重复，无法完整核验')
                candidates.append((table, index, headers))
                break
    if len(candidates) != 1:
        raise ProbeError('个人 HR 页面结构尚未适配或表格不唯一')
    table, index, headers = candidates[0]
    config = ParserConfig(progress_columns=[required[-1]], name_columns=['标题', '种子名称'],
        progress_column_index=headers.index(required[-1]),
        name_column_index=next(headers.index(n) for n in ('标题', '种子名称') if n in headers),
        torrent_id_patterns=[r'(?:^|/)details\.php\?(?:[^#]*&)?id=(\d+)(?:&|$)'])
    pattern = re.compile(config.torrent_id_patterns[0])
    # Validate all data rows before consuming them: don't silently skip malformed
    # rows or accept the upstream display-only row-<digest> fallback as identity.
    for row in table.rows[index + 1:]:
        if any(pattern.search(link.href) for link in row.links):
            if len(row.cells) != len(headers) or not row.texts[config.progress_column_index]:
                raise ProbeError('个人 HR 记录列数或数据不完整')
    selected = tables.Table(rows=table.rows[index:])
    try:
        records = tables._records_from_table(selected, config, '', [pattern])
    except tables.ParseError:
        raise ProbeError('个人 HR 记录解析失败') from None
    if not records:
        empty_text=' '.join(' '.join(row.texts) for row in table.rows[index+1:])
        if not re.search(r'没有|暂无|森马|\bNA\b|N/A|no (?:records|torrents|results)',empty_text,re.I):
            raise ProbeError('HR 页面没有完整记录或明确空列表标志')
    return [(r.key, dict(zip(headers, r.raw_cells))) for r in records if r.key.isdigit()]


def _links(soup, endpoint):
    links = []
    for a in soup.find_all('a', href=True):
        target = urllib.parse.urlparse(a['href'])
        if target.path and target.path.rsplit('/', 1)[-1] != endpoint:
            continue
        if not target.path and not a['href'].startswith('?'):
            continue
        label = a.get_text(' ', strip=True)
        query = urllib.parse.parse_qs(target.query)
        pagination = 'page' in query
        if pagination and not all(x.isdigit() for x in query['page']):
            raise ProbeError('HR 分页参数无效')
        if pagination or label in VIEWS or '下一页' in label or '下页' in label or label.lower().startswith('next'):
            links.append((label, a['href']))
    return links


def _clock(value, optional=False):
    if optional and value in ('', '---', '--', 'NA', 'N/A'):
        return None
    return duration_seconds(value)


def parse_chd_page(html, wanted_id, observed_at):
    soup = _soup(html)
    try:
        rows = _mapped_rows(html, ('标题', 'H&R百分比', '剩余时间', 'H&R周期', '做种时间'))
    except ProbeError:
        if 'No Hit And Runs' in soup.get_text(' ', strip=True) and not soup.select('a[href*="details.php"]'):
            rows = []
        else:
            raise
    results = []
    for ident, values in rows:
        if ident != wanted_id:
            continue
        pct = values['H&R百分比']
        try:
            if not re.fullmatch(r'\d+(?:\.\d+)?%', pct):
                raise ValueError()
            percent = Decimal(pct[:-1])
            if not 0 <= percent <= 100:
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise ProbeError('彩虹岛 HR 百分比无效') from None
        required = duration_seconds(values['H&R周期'])
        seeded = duration_seconds(values['做种时间'])
        window_text = values['剩余时间']
        expired = bool(re.fullmatch(r'已(?:过期|到期)|逾期', window_text)) or window_text.startswith('-')
        window = 0 if expired else duration_seconds(window_text)
        if required <= 0:
            raise ProbeError('彩虹岛 HR 做种要求无效')
        complete = percent == 100 and seeded >= required
        state = 'complete' if complete else ('overdue' if window <= 0 else 'incomplete')
        reason = '彩虹岛个人 HR 表显示已达标' if complete else ('彩虹岛 HR 已到期但未达标' if state == 'overdue' else f'彩虹岛个人 HR 表显示未达标（{pct}）')
        results.append(HrResult(state, reason, observed_at, required, seeded,
            max(0, required - seeded), observed_at + window,
            '站点剩余 ' + window_text + '；截止时间按站点倒计时估算',
            'site_percentage_and_seed_time', remaining_seed_text=values['做种时间'] + ' / ' + values['H&R周期']))
    return results, [href for _, href in _links(soup, 'hnr.php')]


def parse_page(html, wanted_id):
    """Compatibility entrypoint for the existing generic Nexus personal table."""
    soup = _soup(html)
    rows = _mapped_rows(html, ('HR编号', '种子名称', '还需做种时间'))
    return any(ident == wanted_id for ident, _ in rows), _links(soup, 'myhr.php')


class NexusHr:
    def __init__(self, absence_confirmed_sites=(), timeout=20, max_pages=50, stop=None, selected_sites=None):
        # Legacy config is accepted for migration, but never authorizes absence.
        self.timeout, self.max_pages, self.stop = timeout, max_pages, stop
        self.selected_sites = selected_sites
        self.cache, self.response_times = {}, {}
        self.response_bytes = 0

    def check(self, source_url):
        now = time.time()
        if not source_url or not torrent_id(source_url):
            return HrResult('unknown', '缺少站点种子详情页定位', now)
        try:
            parsed = urllib.parse.urlparse(source_url)
            if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
                raise ProbeError('种子详情页地址无效')
            from app.db.oper.site import SiteOper
            from app.sdk.config import settings
            host = parsed.hostname or ''
            site = SiteOper().get_by_domain(host)
            if self.selected_sites is not None and (not site or getattr(site, 'id', None) not in self.selected_sites):
                raise ProbeError('站点未勾选，保留关联种子及共享文件')
            if not site or not site.is_active or not site.cookie:
                raise ProbeError('站点未配置或会话不可用')
            configured = urllib.parse.urlparse(site.url or ('https://' + site.domain))
            origin = lambda u: (u.scheme, (u.hostname or '').lower(), u.port or (443 if u.scheme == 'https' else 80))
            if origin(parsed) != origin(configured):
                raise ProbeError('种子来源与已配置站点不一致')
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler(settings.PROXY if site.proxy else {}))
            headers = {'Cookie': str(site.cookie).strip(), 'User-Agent': site.ua or 'Mozilla/5.0', 'Cache-Control': 'no-cache'}
            def fetch(url):
                if self.stop and self.stop.is_set():
                    raise ProbeError('巡检已停止')
                target = urllib.parse.urlparse(url)
                if target.username or target.password or origin(target) != origin(parsed):
                    raise ProbeError('HR 页面跳转到其他站点，已停止')
                if target.path.rsplit('/',1)[-1] in ('myhr.php','hnr.php'):
                    allowed={'id','page'} if target.path.rsplit('/',1)[-1]=='hnr.php' else {'id','page','hrtype','status','userid'}
                    if set(urllib.parse.parse_qs(target.query))-allowed:
                        raise ProbeError('个人 HR 链接包含非只读筛选参数，已停止')
                if url not in self.cache:
                    with opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout) as response:
                        raw = response.read(4 * 1024 * 1024 + 1)
                        stamp = time.time()
                        server_date = getattr(response, 'headers', {}).get('Date')
                        if server_date:
                            try: stamp = parsedate_to_datetime(server_date).timestamp()
                            except (TypeError, ValueError, OverflowError): pass
                    if len(raw) > 4 * 1024 * 1024:
                        raise ProbeError('HR 页面过大，无法确认完整记录')
                    self.response_bytes += len(raw)
                    if self.response_bytes > 32 * 1024 * 1024:
                        raise ProbeError('HR 响应总量超过巡检上限，未完成完整核对')
                    self.cache[url] = raw.decode('utf-8', errors='replace')
                    self.response_times[url] = stamp
                return self.cache[url]
            endpoint = 'hnr.php' if host == 'ptchdbits.co' else 'myhr.php'
            detail = _soup(fetch(source_url))
            link = next((a.get('href') for a in detail.find_all('a') if urllib.parse.urlparse(a.get('href', '')).path.rsplit('/', 1)[-1] == endpoint), None)
            if not link:
                raise ProbeError('站点未提供已适配的个人 HR 入口')
            start = urllib.parse.urljoin(source_url, link)
            account = urllib.parse.parse_qs(urllib.parse.urlparse(start).query).get('id') if endpoint == 'hnr.php' else None
            if endpoint == 'hnr.php' and (not account or len(account) != 1 or not account[0].isdigit()):
                raise ProbeError('彩虹岛 HR 账户定位无效')
            queue, visited, results, views = [(start, '全部')], set(), [], set()
            profile_ids=urllib.parse.parse_qs(urllib.parse.urlparse(start).query).get('userid',[])
            if profile_ids and (len(profile_ids)!=1 or not profile_ids[0].isdigit()):
                raise ProbeError('个人 HR 账户定位无效')
            profile_user = profile_ids[0] if profile_ids else None
            while queue:
                url, view = queue.pop(0)
                parsed_url = urllib.parse.urlparse(url)
                query = urllib.parse.parse_qs(parsed_url.query)
                if endpoint == 'myhr.php' and query.get('userid'):
                    uid=query['userid']
                    if len(uid)!=1 or not uid[0].isdigit() or (profile_user and uid[0]!=profile_user):
                        raise ProbeError('个人 HR 分页账户发生变化')
                    if profile_user is None:
                        raise ProbeError('个人 HR 分页账户尚未由当前账号页面确认')
                marker_query={k:v for k,v in query.items() if k!='userid' and not (k=='page' and v==['0'])}
                marker=(origin(parsed_url),parsed_url.path,urllib.parse.urlencode(sorted(marker_query.items()),doseq=True),view)
                if marker in visited:
                    continue
                if len(visited) >= self.max_pages:
                    raise ProbeError('HR 分页超过巡检上限，未完成完整核对')
                if account and urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get('id') != account:
                    raise ProbeError('彩虹岛 HR 分页账户发生变化')
                visited.add(marker)
                html = fetch(url)
                stamp = self.response_times[url]
                if endpoint == 'hnr.php':
                    found, links = parse_chd_page(html, torrent_id(source_url), stamp)
                    results.extend(found)
                    queue.extend((urllib.parse.urljoin(url, href), view) for href in links)
                else:
                    required = ('种子编号', '种子名称', '还需做种时间', '剩余达标时间') if host == 'hdhome.org' else ('HR编号', '种子名称', '还需做种时间')
                    rows = _mapped_rows(html, required)
                    views.add(view)
                    for ident, values in rows:
                        if ident != torrent_id(source_url):
                            continue
                        remaining = _clock(values['还需做种时间'], optional=True)
                        window_text = values.get('剩余达标时间', '')
                        window = 0 if window_text.startswith('-') or window_text in ('已过期','已到期','逾期') else _clock(window_text, optional=True)
                        state = {'已达标': 'complete', '未达标': 'overdue' if host == 'hdhome.org' else 'incomplete', '考核中': 'incomplete', '已免罪': 'no_hr'}.get(view, 'unknown')
                        if state == 'incomplete' and window is not None and window <= 0:
                            state = 'overdue'
                        reason = '站点个人 HR 列表显示' + view
                        if state == 'unknown': reason = '存在 HR 记录，但未获得明确达标状态'
                        results.append(HrResult(state, reason, now, remaining_seed_seconds=remaining,
                            deadline_at=stamp + window if window is not None else None,
                            deadline_text='站点剩余 ' + window_text + '；截止时间按站点倒计时估算' if window is not None else '',
                            basis='site_waiver_view' if state == 'no_hr' else 'site_personal_view',
                            remaining_seed_text=values['还需做种时间']))
                    links=_links(_soup(html),endpoint)
                    profile_ids=set()
                    for _,href in links:
                        ids=urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get('userid')
                        if ids:
                            if len(ids)!=1 or not ids[0].isdigit():raise ProbeError('个人 HR 分页账户无效')
                            profile_ids.add(ids[0])
                    if profile_ids:
                        if len(profile_ids)!=1 or (profile_user and profile_ids!={profile_user}):
                            raise ProbeError('个人 HR 分页账户发生变化')
                        profile_user=next(iter(profile_ids))
                    queue.extend((urllib.parse.urljoin(url, href), label if label in VIEWS else view) for label, href in links)
            if not results:
                return HrResult('unknown', '个人 HR 表未找到该种子，不能据此认定无 HR 或已达标', now)
            blocking = next((r for r in results if r.state in ('incomplete', 'overdue')), None)
            if blocking:
                return replace(blocking, checked_at=now)
            if endpoint == 'myhr.php':
                required_views = {'考核中', '已达标', '未达标'} if host == 'hdhome.org' else {'已达标', '未达标'}
                if not required_views <= views:
                    raise ProbeError('个人 HR 筛选页面不完整，无法确认达标')
            cleared = next((r for r in results if r.state in ('complete', 'no_hr')), None)
            if cleared:
                return replace(cleared, checked_at=now)
            return replace(results[0], checked_at=now)
        except ProbeError as e:
            return HrResult('unknown', str(e), now)
        except Exception as e:
            return HrResult('unknown', 'HR 查询失败：' + type(e).__name__, now)
