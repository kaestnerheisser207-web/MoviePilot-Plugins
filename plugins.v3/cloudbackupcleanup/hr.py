"""Conservative NexusPHP personal-HR reader. Missing records default to unknown."""
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from bs4 import BeautifulSoup
from .cloud import ProbeError


@dataclass(frozen=True)
class HrResult:
    state: str  # complete, no_hr, incomplete, unknown
    reason: str
    checked_at: float


def torrent_id(url):
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except ValueError:
        return ''
    value = (q.get('id') or q.get('torrentid') or [''])[0]
    return value if value.isdigit() else ''


def parse_page(html, wanted_id):
    """Require a recognizable HR table; a login/error page is never empty HR."""
    soup = BeautifulSoup(html, 'html.parser')
    if soup.select('input[type=password]'):
        raise ProbeError('站点会话已失效')
    tables = []
    for table in soup.find_all('table'):
        headers = [x.get_text(' ', strip=True) for x in table.select('th, td.colhead')]
        if 'HR编号' in headers and '种子名称' in headers and '还需做种时间' in headers:
            tables.append(table)
    if not tables:
        raise ProbeError('站点 HR 页面结构尚未适配')
    table = min(tables, key=lambda t: len(str(t)))
    def is_torrent_link(a):
        return urllib.parse.urlparse(a.get('href','')).path.rsplit('/',1)[-1]=='details.php'
    rows = []
    for row in table.find_all('tr'):
        if any(is_torrent_link(a) and torrent_id(a.get('href', '')) == wanted_id for a in row.find_all('a')):
            rows.append(row.get_text(' ', strip=True))
    links = []
    for a in soup.find_all('a'):
        label = a.get_text(' ', strip=True)
        href=a.get('href','')
        path=urllib.parse.urlparse(href).path
        hr_navigation=path.rsplit('/',1)[-1]=='myhr.php' or (not path and href.startswith('?'))
        if hr_navigation and (label in ('已达标', '未达标', '全部') or label.lower() in ('next', 'next >') or '下一页' in label):
            links.append((label,href))
    has_other_rows = any(is_torrent_link(a) and torrent_id(a.get('href','')) for a in table.find_all('a'))
    empty = bool(re.search(r'没有|暂无|森马|no (?:records|torrents|results)', table.get_text(' ', strip=True), re.I))
    if not rows and not has_other_rows and not empty:
        # Some Nexus forks put the empty marker next to the table.
        empty = bool(re.search(r'森马都没有找到|没有找到记录|暂无记录|no torrents found', soup.get_text(' ', strip=True), re.I))
        if not empty:
            raise ProbeError('HR 页面没有完整记录或明确空列表标志')
    return bool(rows), links


class NexusHr:
    def __init__(self, absence_confirmed_sites=(), timeout=20, max_pages=10):
        self.absence_confirmed_sites = set(absence_confirmed_sites)
        self.timeout = timeout
        self.max_pages = max_pages
        self.cache = {}

    def check(self, source_url):
        now = time.time()
        if not source_url or not torrent_id(source_url):
            return HrResult('unknown', '缺少站点种子详情页定位', now)
        parsed = urllib.parse.urlparse(source_url)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            return HrResult('unknown', '种子详情页地址无效', now)
        host = parsed.hostname or ''
        try:
            from app.db.oper.site import SiteOper
            from app.sdk.config import settings
            site = SiteOper().get_by_domain(host)
            if not site or not site.is_active or not site.cookie:
                raise ProbeError('站点未配置或会话不可用')
            configured = urllib.parse.urlparse(site.url or ('https://' + site.domain))
            if (configured.hostname or '').lower() != host.lower():
                raise ProbeError('种子来源与已配置站点不一致')
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            expected_port = configured.port or (443 if configured.scheme == 'https' else 80)
            if port != expected_port:
                raise ProbeError('种子来源端口与站点配置不一致')
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            proxy = settings.PROXY if site.proxy else {}
            opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler(proxy or {}))
            headers = {'Cookie': site.cookie, 'User-Agent': site.ua or 'Mozilla/5.0'}
            def fetch(url):
                target = urllib.parse.urlparse(url)
                origin=lambda u:(u.scheme,(u.hostname or '').lower(),u.port or (443 if u.scheme=='https' else 80))
                if target.username or target.password or origin(target)!=origin(parsed):
                    raise ProbeError('HR 页面跳转到其他站点，已停止')
                if url not in self.cache:
                    req = urllib.request.Request(url, headers=headers)
                    with opener.open(req, timeout=self.timeout) as response:
                        raw = response.read(4*1024*1024+1)
                    if len(raw) > 4*1024*1024:
                        raise ProbeError('HR 页面过大，无法确认完整记录')
                    self.cache[url] = raw.decode('utf-8', errors='replace')
                return self.cache[url]
            detail = BeautifulSoup(fetch(source_url), 'html.parser')
            if detail.select('input[type=password]'):
                raise ProbeError('站点会话已失效')
            link = next((a.get('href') for a in detail.find_all('a')
                         if urllib.parse.urlparse(a.get('href','')).path.rstrip('/').endswith('myhr.php')), None)
            if not link:
                raise ProbeError('站点未提供已适配的个人 HR 入口')
            start = urllib.parse.urljoin(source_url, link)
            queue = [(start, '全部')]; visited = set(); found = set(); views = set()
            wanted = torrent_id(source_url)
            while queue:
                url, view = queue.pop(0)
                marker = (url, view)
                if marker in visited:
                    continue
                if len(visited) >= self.max_pages:
                    raise ProbeError('HR 分页超过巡检上限，未完成完整核对')
                visited.add(marker)
                matched, links = parse_page(fetch(url), wanted)
                views.add(view)
                if matched:
                    found.add(view)
                for label, href in links:
                    if not href:
                        continue
                    next_view = label if label in ('已达标','未达标','全部') else view
                    queue.append((urllib.parse.urljoin(url, href), next_view))
            if '未达标' in found:
                return HrResult('incomplete', '站点个人 HR 列表显示未达标', now)
            if '已达标' in found:
                return HrResult('complete', '站点个人 HR 列表显示已达标', now)
            if found:
                return HrResult('unknown', '存在 HR 记录，但未获得明确达标状态', now)
            if host in self.absence_confirmed_sites and {'已达标','未达标'} <= views:
                return HrResult('no_hr', '按已核实的站点规则，完整 HR 列表中无记录', now)
            return HrResult('unknown', 'HR 列表无记录；尚未确认该站无记录即无 HR', now)
        except ProbeError as e:
            return HrResult('unknown', str(e), now)
        except Exception as e:
            return HrResult('unknown', 'HR 查询失败：' + type(e).__name__, now)
