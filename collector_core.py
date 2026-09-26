#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collector_core.py — 短视频平台视频链接采集核心（零外部依赖，仅用标准库）

提供：
- HttpClient：统一 UA / 节流 / **按域名下发 Cookie** / GET + POST JSON，基于 urllib。
- BilibiliSearcher：关键词搜索（WBI 签名）+ 视频详情 enrich（时长/点赞/播放/UP主）。
- KuaishouSearcher：快手 GraphQL 搜索（带 Cookie 即可直出标题/时长/点赞/播放/作者）。
- ShareResolver：抖音/快手分享短链 -> 干净视频页 URL。
- BrowserSearcher：抖音/快手 Playwright 搜索，支持注入登录 Cookie / 持久化 profile，
  并拦截抖音搜索 XHR 直接提取完整元数据。
- VideoLink：统一数据模型，含元数据字段。
- build_markdown：把采集结果渲染为 MD 文档字符串。

本模块同时被 CLI（collect_links.py）与 Web 后端（app.py）复用，避免重复代码。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 注意：此前的 STEALTH_INIT（navigator.webdriver 覆写等反检测注入）与 --headless=new 启动参数
# 经实测会触发抖音 secsdk 的 verify_check 风控验证码墙，导致关键词搜索恒返回 0 条。
# 参考 video_link_collector-v13fixedmany（同一 Cookie 可正常采集关键词搜索结果）的实现已移除二者，
# 仅用普通 headless + 关闭 AutomationControlled。此处刻意对齐，不再做 stealth 注入。

# 浏览器启动参数：与已验证可用的 video_link_collector-v13fixedmany 实现保持一致。
# 仅关闭 AutomationControlled 并放宽音视频自动播放策略。刻意不启用 --headless=new、不做 stealth 注入，
# 否则抖音关键词搜索接口会被风控拦截（verify_check），返回空 data。
BROWSER_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--autoplay-policy=no-user-gesture-required",
    "--mute-audio",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

# Bilibili WBI 重排表（仅取 <32 的索引组成 32 位 mixin key）
WBI_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# 平台 -> 主域名（用于按域名下发 Cookie，严格隔离避免 A 站 Cookie 泄漏到 B 站）
PLATFORM_DOMAIN = {
    "bilibili": "bilibili.com",
    "douyin": "douyin.com",
    "kuaishou": "kuaishou.com",
    "xiaohongshu": "xiaohongshu.com",
    "weibo": "weibo.com",
    "youtube": "youtube.com",
}

# 浏览器搜索配置表：新增平台只改这里，BrowserSearcher 无需改代码。
#   url  —— 搜索页地址，{kw} 会被 URL-encode 后的关键词替换
#   pat  —— 从 href 中提取视频 ID 的正则（第 1 组为 ID）
#   host —— 拼接纯净链接的前缀，最终链接 = host + ID
#   sel  —— 用于收集 href 的 CSS 选择器
BROWSER_SEARCH: dict[str, dict] = {
    "douyin": {
        "url": "https://www.douyin.com/search/{kw}",
        "pat": r"/video/(\d+)", "host": "https://www.douyin.com/video/",
        "sel": "a[href*='/video/'], a[href*='v.douyin.com/']",
        # 结果卡片可能用 /video/{id} 或 v.douyin.com/{short} 两种链接形态
        "variants": [
            {"pat": r"/video/(\d+)", "host": "https://www.douyin.com/video/"},
            {"pat": r"v\.douyin\.com/([A-Za-z0-9_-]+)", "host": "https://v.douyin.com/"},
        ],
    },
    "kuaishou": {
        "url": "https://www.kuaishou.com/search/video?searchKey={kw}",
        "pat": r"/short-video/([A-Za-z0-9_-]+)", "host": "https://www.kuaishou.com/short-video/",
        "sel": "a[href*='/short-video/']",
    },
    "bilibili": {
        "url": "https://search.bilibili.com/video?keyword={kw}",
        "pat": r"/video/(BV[A-Za-z0-9]+)", "host": "https://www.bilibili.com/video/",
        "sel": "a[href*='/video/BV']",
    },
    "xiaohongshu": {
        "url": "https://www.xiaohongshu.com/search_result?keyword={kw}&type=video",
        # 小红书笔记必须带 xsec_source / xsec_token 才能打开，否则提示「你访问的页面不见了」。
        # 搜索结果卡片的 <a href> 往往只有 /explore/{id}，真正的 token 放在元素（或其祖先）上的
        # data-xsec-token / data-xsec-source 属性，由前端点击时才拼进 URL。
        # 因此这里标记 xsec=True，搜索循环会额外读取这两个属性并补回链接；href 里若已带 token 则不重复拼接。
        "pat": r"(/(?:explore|discovery/item)/[0-9a-f]{16,32}(?:[?#][^\s\"']*)?)",
        "host": "https://www.xiaohongshu.com",
        "sel": "a[href*='/explore/'], a[href*='/discovery/item/']",
        "xsec": True,
        "dom_meta": True,
    },
    "weibo": {
        "url": "https://s.weibo.com/video?q={kw}",
        # 微博视频结果卡片的链接是 sinaweibo:// 深链（内含 mid=状态ID），
        # 旧版 /tv/show/ 与 video.weibo.com/show?fid=1034:ID 也已少见。
        # 统一抽取 mid，拼成 yt-dlp 支持的 m.weibo.cn/status/{mid}（WeiboIE 直接可下）。
        "sel": "a[href*='sinaweibo://'], a[href*='video.weibo.com/show'], a[href*='/tv/show/']",
        "dom_meta": True,
        "variants": [
            {"pat": r"mid[=%3D]+([0-9]+)", "host": "https://m.weibo.cn/status/"},
            {"pat": r"video\.weibo\.com/show\?fid=1034:([0-9]+)", "host": "https://m.weibo.cn/status/"},
            {"pat": r"/tv/show/1034:([0-9]+)", "host": "https://m.weibo.cn/status/"},
        ],
    },
    "youtube": {
        "url": "https://www.youtube.com/results?search_query={kw}",
        "pat": r"[?&]v=([A-Za-z0-9_-]{11})", "host": "https://www.youtube.com/watch?v=",
        "sel": "a[href*='watch?v=']",
        "dom_meta": True,
    },
}

# 快手 Web 搜索 GraphQL 查询（与官网前端一致）。cookie_manager 探活时复用同一份定义。
KUAISHOU_GRAPHQL_QUERY = """
query visionSearchPhoto($keyword: String, $pcursor: String, $searchSessionId: String, $page: String, $webPageArea: String) {
  visionSearchPhoto(keyword: $keyword, pcursor: $pcursor, searchSessionId: $searchSessionId, page: $page, webPageArea: $webPageArea) {
    result
    llsid
    webPageArea
    feeds {
      type
      author { id name headerUrl }
      tags { type name }
      photo {
        id
        duration
        caption
        likeCount
        realLikeCount
        viewCount
        coverUrl
        timestamp
      }
      canAddComment
      currentPcursor
    }
    searchSessionId
    pcursor
  }
}
""".strip()


@dataclass
class VideoLink:
    platform: str
    keyword: str
    title: str
    url: str
    author: str = ""
    duration: int = 0          # 秒
    likes: int = 0
    views: int = 0
    media_url: str = ""        # 小红书真实视频直链（浏览器解析得到）
    cover: str = ""            # 封面图
    is_video: bool = False     # 是否为视频笔记（图文笔记无需视频下载）
    fetched_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))


def fmt_num(n: int) -> str:
    """把数字格式化为中文习惯：12345 -> 1.2万。"""
    if n is None or n == 0:
        return "0"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def fmt_duration(sec: int) -> str:
    if not sec:
        return "—"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _to_int(v) -> int:
    """把 '1.2万' / '12,345' / 12345 统一转 int。"""
    if isinstance(v, (int, float)):
        return int(v)
    if not v:
        return 0
    s = str(v).strip().replace(",", "")
    try:
        if s.endswith("万"):
            return int(float(s[:-1]) * 10_000)
        if s.endswith("亿"):
            return int(float(s[:-1]) * 100_000_000)
        return int(float(s))
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# 浏览器 DOM 元数据提取：对用真实浏览器搜索但拿不到接口 JSON 的平台
# （小红书 / 微博 / YouTube），直接从卡片 DOM 文本里解析
# 标题 / 点赞 / 播放 / 作者 / 封图，避免依赖 yt-dlp（对这类强反爬站基本失效）。
# 这是「尽力而为」的启发式：抓不到就留空，绝不影响链接本身。
# ---------------------------------------------------------------------------

def _match_metric(hay: str, keys: tuple[str, ...]) -> int:
    """在文本里找 '<指标词> <数字>'（优先）或 '<数字>紧接指标词>'，返回 int。

    优先用「指标词 数字」顺序（如「点赞 8901」「❤ 1.2万」）。
    关键：'<指标词> 数字' 的数字后必须紧跟空白或结尾（(?=\\s|$)），否则会误吃
    下一对的指标词或时长——例如「播放 3201赞」会把 3201 误当播放、「赞 05:10」
    会把 05 误当点赞。该断言确保只吃「属于本指标词」的数字。
    """
    for pat in (
        r"(?:" + "|".join(keys) + r")\s*[:：]?\s*([\d.]+(?:\s*[万亿])?)(?=\s|$)",
        r"([\d.]+(?:\s*[万亿])?)(?:" + "|".join(keys) + r")",
    ):
        m = re.search(pat, hay)
        if m:
            return _to_int(m.group(1).replace(" ", ""))
    return 0


def _match_duration(hay: str) -> int:
    m = re.search(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", hay)
    if not m:
        return 0
    parts = [int(x) for x in m.group(1).split(":")]
    return (parts[0] * 3600 + parts[1] * 60 + parts[2]) if len(parts) == 3 else (parts[0] * 60 + parts[1])


def _match_author(hay: str) -> str:
    m = re.search(r"@([\w\-·.\u4e00-\u9fa5]{1,30})", hay)
    return m.group(1).strip() if m else ""


def _extract_dom_meta(raw: dict, platform: str):
    """从单个卡片的原始 DOM 数据解析 (title, author, likes, views, duration, cover)。

    raw 字段：text / alt / aria / titleAttr / img
    启发式优先级：
      - 标题：img.alt（小红书封面 alt 多为标题）> aria-label 主体 > text 首行 > title 属性
      - 点赞 / 播放：从合并文本里按指标词正则提取
      - 作者：抓 @xxx
      - 时长：mm:ss 形态
    """
    text = (raw.get("text") or "").strip()
    alt = (raw.get("alt") or "").strip()
    aria = (raw.get("aria") or "").strip()
    title_attr = (raw.get("titleAttr") or "").strip()
    cover = (raw.get("img") or "").strip()
    hay = " ".join([aria, alt, text, title_attr])

    # 微博结果卡片文本形如：「c {作者} {日期} 来自 {来源} {文案} L{作者}的微博视频」
    # 通用标题清理会残留用户名/日期/来源，这里单独解析出干净的「文案」与「作者」。
    if platform == "weibo":
        t = (text or "").strip()
        am = re.search(r"L(.+?)的微博视频", t)
        author = am.group(1).strip() if am else _match_author(hay)
        t2 = re.sub(r"^c\s+", "", t)
        t2 = re.sub(r"\s*L.+?的微博视频[\u200b-\u200f\ufeff\s]*", "", t2).strip()
        cm = re.search(r"来自\s*\S+\s*(.*)$", t2, re.S)
        title = (cm.group(1).strip() if cm else t2)[:80]
        likes = _match_metric(hay, ("赞", "❤", "喜欢", "点赞", "💗", "likes", "digg"))
        views = _match_metric(hay, ("播放", "次播放", "观看", "views", "plays"))
        duration = _match_duration(text) or _match_duration(aria)
        return title, author, likes, views, duration, cover

    # 标题
    title = ""
    if alt and len(alt) >= 3 and "http" not in alt and "xsec" not in alt:
        title = alt
    if not title and aria:
        # aria-label 常见形如「标题 - 作者 - 1.2万赞」，取第一段
        title = re.split(r"[|\-·•]", aria)[0].strip()
    if not title and text:
        title = text.split("\n")[0].strip()
    if not title and title_attr and "http" not in title_attr:
        title = title_attr
    # 清掉标题里误夹的点赞/播放计数与时长片段，并去掉开头的 @作者。
    # 标题清理只用『数字 指标词』方向（如「1.2万播放」「8901赞」），再用孤立指标词删除兜底。
    # 注意：不能用「指标词 数字」方向清理标题——否则会误删「播放 3201」里本属于「赞」的数字。
    # （数值提取在 _match_metric 里仍用「指标词 数字」方向，那是针对 likes/views 的正确做法。）
    _mw = r"赞|❤|喜欢|点赞|💗|播放|次播放|views?|likes?"
    # 顺序很关键：先删『数字 指标词』(如「1.2万播放」「3201赞」) 把正确的配对拆干净，
    # 再删『指标词 数字』(如「点赞 8901」「❤ 1.2万」)，最后删残留的孤立指标词与时长。
    # 这样可避免把「播放 3201赞」里本属于「赞」的 3201 误当成播放数删掉。
    title = re.sub(rf"[\d.]+\s*[万亿]?\s*次?\s*(?:{_mw})", "", title)
    title = re.sub(rf"(?:{_mw})\s*[:：]?\s*[\d.]+\s*[万亿]?", "", title)
    title = re.sub(rf"\s*(?:{_mw})\s*", " ", title)
    title = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "", title)
    title = re.sub(r"^@[\w\-·.\u4e00-\u9fa5]{1,30}\s*", "", title).strip()
    title = re.sub(r"\s{2,}", " ", title).strip()
    title = title[:80]

    likes = _match_metric(hay, ("赞", "❤", "喜欢", "点赞", "💗", "likes", "digg"))
    views = _match_metric(hay, ("播放", "次播放", "观看", "views", "plays"))
    duration = _match_duration(text) or _match_duration(aria)
    author = _match_author(hay)

    return title, author, likes, views, duration, cover


# ---------------------------------------------------------------------------
# 小红书搜索 XHR 解析：小红书搜索结果由前端异步请求
#   https://edith.xiaohongshu.com/api/sns/web/v1/search/notes
# 返回 JSON，里面直接带有 note_id / xsec_token / xsec_source / display_title /
# interact_info(liked_count, viewed_count) / user.nickname / video.time。
# 这个 token 是和「当前登录态 + 这次搜索会话」绑定的，新鲜有效——远比从 DOM 的
# data-xsec-token 属性里猜要可靠，能真正打开笔记页（否则「你访问的页面不见了」）。
# 因此拦截这个 XHR 作为小红书元数据 + 链接的「主数据源」，DOM 仅作兜底。
# ---------------------------------------------------------------------------

def _parse_xhs_note(note: dict, keyword: str):
    """把小红书搜索接口(v2: so.xiaohongshu.com/api/sns/web/v2/search/notes)返回的
    单个 note 解析成 VideoLink（含有效 token 链接）。

    v2 结构：{id, model_type, note_card:{display_title,user,interact_info,video,type,cover}, xsec_token}
    注意 xsec_token 在顶层、其余富数据都在 note_card 内。
    """
    if not isinstance(note, dict):
        return None
    # v2 用 id，旧版可能用 note_id
    nid = note.get("id") or note.get("note_id")
    if not nid:
        return None
    nc = note.get("note_card") or note   # v2 富数据在 note_card 内
    xt = (note.get("xsec_token") or "").strip()
    xs = (note.get("xsec_source") or "pc_search").strip()
    url = f"https://www.xiaohongshu.com/explore/{nid}"
    if xt:
        url += f"?xsec_source={xs}&xsec_token={xt}"
    title = (nc.get("display_title") or nc.get("title") or "").strip()
    user = nc.get("user") or {}
    author = (user.get("nickname") or user.get("nick_name") or "").strip()
    info = nc.get("interact_info") or {}
    likes = _to_int(info.get("liked_count"))
    views = _to_int(info.get("viewed_count") or info.get("view_count"))
    video = nc.get("video") or {}
    dur = _to_int(video.get("time") or video.get("duration"))
    if dur > 100000:  # 个别接口返回毫秒
        dur = dur // 1000
    # 真实视频直链：搜索接口 video.media[].url 常已可直接下载（与登录态绑定）。
    # v2 搜索接口一般不返回 video.media（防盗链），为空时由 resolve_xhs_media_url 兜底。
    media_url = ""
    for m in (video.get("media") or []):
        u = (m.get("url") if isinstance(m, dict) else "") or ""
        if u and ("xhscdn" in u or u.endswith(".mp4") or ".mp4?" in u):
            media_url = u
            break
    cover = nc.get("cover") or note.get("cover") or ""
    if isinstance(cover, dict):
        cover = cover.get("url_default") or cover.get("url") or ""
    if isinstance(cover, list):
        cover = cover[0] if cover else ""
    cover = cover if isinstance(cover, str) else ""
    is_video = (nc.get("type") == "video") or bool(video)
    return VideoLink("xiaohongshu", keyword, title, url,
                     author=author, duration=dur, likes=likes, views=views,
                     media_url=media_url, cover=cover, is_video=is_video)


def netscape_to_pw_objs(path: str) -> list[dict]:
    """把 netscape cookie 文件转成 Playwright add_cookies 所需的对象列表。"""
    objs: list[dict] = []
    if not path or not os.path.isfile(path):
        return objs
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path_, secure, _, name, value = (parts[0], parts[1], parts[2],
                                                     parts[3], parts[4], parts[5], parts[6])
        objs.append({"name": name, "value": value,
                     "domain": domain.lstrip("."), "path": path_ or "/",
                     "secure": secure.upper() == "TRUE", "httpOnly": False})
    return objs


def netscape_to_header(path: str) -> str:
    """把 netscape cookie 文件拼成 'name=value; ...' 请求头（用于直连下载）。"""
    parts: list[str] = []
    if not path or not os.path.isfile(path):
        return ""
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        segs = line.split("\t")
        if len(segs) < 7:
            continue
        parts.append(f"{segs[5]}={segs[6]}")
    return "; ".join(parts)


def resolve_xhs_media_url(note_url: str, profile_root: str = "", cookie_file: str = "",
                         headless: bool = True, timeout: int = 60) -> dict:
    """用登录态浏览器截获小红书笔记的真实视频直链。

    返回 {"ok": True, "media_url":..., "title":..., "cover":...} 或
    {"ok": False, "error":...}。优先用持久化 profile（含登录态），
    否则把 netscape cookie 注入临时 context。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"未安装 playwright：{e}"}
    profile = os.path.join(profile_root, "xiaohongshu") if profile_root else ""
    found: dict = {}
    # 目标笔记 id：只从此笔记提取标题/作者，彻底避免抓到「相关推荐」流里的其它笔记。
    _m = re.search(r"explore/([0-9a-f]{16,32})", note_url)
    target_nid = _m.group(1) if _m else ""

    def scan(o):
        _xhs_scan(o, found)

    def scan_main(o):
        _xhs_extract_main(o, target_nid, found)

    def on_resp(resp):
        try:
            u = resp.url
            # 直接命中视频 CDN 流：播放时浏览器才会请求 sns-video 地址，最可靠
            if "sns-video" in u and ("xhscdn" in u or ".mp4" in u or ".m3u8" in u):
                found.setdefault("media_url", u)
                return
            if "xiaohongshu.com/api/" not in u or resp.status >= 400:
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            # 优先按 note id 精确提取主笔记元数据；若未命中（接口结构差异）再退回宽松扫描兜底。
            # 关键：仅当无法确定目标笔记（URL 无 note_id，target_nid 为空）时才用宽松扫描兜底标题；
            # 已知目标时绝不用「相关推荐」流里第一个笔记的标题覆盖真实标题，避免下载文件名错乱。
            scan_main(resp.json())
            if not found.get("title") and not target_nid:
                scan(resp.json())
        except Exception:  # noqa: BLE001
            pass

    try:
        with sync_playwright() as p:
            # 始终用全新 context + 注入最新 cookie（来自 netscape 文件），不使用持久化 profile，
            # 避免 profile 被平台反爬标记后取不到直链。
            browser = p.chromium.launch(headless=headless,
                                        args=["--disable-blink-features=AutomationControlled",
                      "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
            ctx = browser.new_context(user_agent=DEFAULT_UA,
                                      viewport={"width": 1360, "height": 900})
            objs = netscape_to_pw_objs(cookie_file)
            if objs:
                try:
                    ctx.add_cookies(objs)
                except Exception:  # noqa: BLE001
                    pass
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", on_resp)
            # 视频流会持续加载，导致 load 事件迟迟不触发；用 domcontentloaded 并在
            # 超时时忽略（页面其实已渲染、视频可触发），避免因等待 load 而中断。
            try:
                page.goto(note_url, timeout=45000, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass
            # 触发视频加载：直接 play() + 点击播放器/封面，让浏览器请求 sns-video 流
            try:
                page.eval_on_selector("video", "v => { if (v && v.play) { try { v.muted=true; v.play().catch(()=>{}); } catch(e){} } }")
            except Exception:  # noqa: BLE001
                pass
            for sel in (".player-container", ".cover", "[class*='player']", "[class*='play']", "video"):
                try:
                    page.click(sel, timeout=2000)
                    break
                except Exception:  # noqa: BLE001
                    continue
            for _ in range(timeout):
                if found.get("media_url"):
                    break
                # 每轮再次尝试触发播放（部分站点到点击后才加载 video 元素）
                try:
                    page.eval_on_selector("video", "v => { if (v) { try { v.muted=true; v.play().catch(()=>{}); } catch(e){} } }")
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(1000)
            # 兜底1：<video> 元素 currentSrc
            if not found.get("media_url"):
                try:
                    src = page.eval_on_selector("video", "v => v ? (v.currentSrc || v.src) : ''")
                    if src and "xhscdn" in src and "blob:" not in src:
                        found["media_url"] = src
                except Exception:  # noqa: BLE001
                    pass
            # 兜底2：用页面上下文主动请求 feed 接口（自带浏览器签名头）
            if not found.get("media_url"):
                try:
                    m = re.search(r"explore/([0-9a-f]{16,32})", note_url)
                    if m:
                        nid = m.group(1)
                        data = page.evaluate(
                            """(nid) => fetch('https://edith.xiaohongshu.com/api/sns/web/v1/feed', {
                                method:'POST', headers:{'content-type':'application/json'},
                                body: JSON.stringify({source_note_id:nid, image_formats:['jpg','webp','avif'],
                                extra:'{"need_delete_master":false}', share_info:{type:'note',title:'',desc:''}, is_async_request:false})
                            }).then(r=>r.json()).then(j=>{
                                const it=((j.data||{}).items)||[]; const nc=(it[0]||{}).note_card||it[0]||{};
                                const v=nc.video||{}; const mm=(v.media||[])[0]||{}; return (v.consumer&&v.consumer.url)||mm.url||'';
                            })""", nid)
                        if data and ("xhscdn" in data or ".mp4" in data):
                            found["media_url"] = data
                except Exception:  # noqa: BLE001
                    pass
            ctx.close()
            if browser:
                browser.close()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"浏览器解析失败：{e}"}

    if found.get("media_url"):
        return {"ok": True, "media_url": found["media_url"],
                "title": found.get("title", ""), "cover": found.get("cover", ""),
                "duration": found.get("duration", 0), "likes": found.get("likes", 0),
                "views": found.get("views", 0), "author": found.get("author", "")}
    return {"ok": False, "error": "未能取到真实视频直链（可能该笔记为图文，或登录态失效需重新登录）"}


def download_url_to_file(url: str, out_path: str, cookie_header: str = "", referer: str = "",
                         emit_progress=None, timeout: int = 60) -> int:
    """用标准库直连下载文件（适用于小红书 CDN 直链）。返回下载字节数。"""
    import ssl
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": DEFAULT_UA,
        "Referer": referer or "https://www.xiaohongshu.com/",
        "Origin": "https://www.xiaohongshu.com",
        "Cookie": cookie_header,
    })
    ctx = ssl.create_default_context()
    total = 0
    done = 0
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        total = int(r.headers.get("Content-Length", 0) or 0)
        with open(out_path, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if emit_progress and total:
                    emit_progress(done, total)
    return done


def _xhs_scan(o, store: dict) -> None:
    """递归从接口 JSON 抽取小红书真实直链/标题/时长/点赞/播放/作者/封面，写入 store。

    搜索接口 / feed 接口的响应结构接近（note_card 内嵌），统一用本函数扫描，
    避免 search 与 resolve 两处重复实现导致行为不一致。
    """
    if isinstance(o, dict):
        vid = o.get("video")
        if isinstance(vid, dict):
            for m in (vid.get("media") or []):
                u = m.get("url") if isinstance(m, dict) else ""
                if u and ("xhscdn" in u or u.endswith(".mp4") or ".mp4?" in u):
                    store.setdefault("media_url", u)   # 带水印直链，作兜底
            # 优先无水印：consumer.url 是小红书「消费端」直链，不含作者水印；
            # media[].url 多为带水印版本，仅作兜底。两者都缺时才回退到带水印。
            cons = (vid.get("consumer") or {}).get("url") if isinstance(vid.get("consumer"), dict) else ""
            if cons and ("xhscdn" in cons or ".mp4" in cons):
                store["media_url"] = cons                # 覆盖为无水印直链
            if not store.get("duration"):
                d = vid.get("time") or vid.get("duration")
                if d:
                    dd = _to_int(d)
                    if dd > 100000:   # 个别接口返回毫秒
                        dd //= 1000
                    store["duration"] = dd
            vc = vid.get("cover") or {}
            if isinstance(vc, dict) and not store.get("cover"):
                store["cover"] = vc.get("url_default") or vc.get("url_pre") or vc.get("url") or ""
            if cons and "xhscdn" in cons and not store.get("cover"):
                store["cover"] = cons
        info = o.get("interact_info")
        if isinstance(info, dict):
            if not store.get("likes"):
                store["likes"] = _to_int(info.get("liked_count"))
            if not store.get("views"):
                store["views"] = _to_int(info.get("viewed_count") or info.get("view_count"))
        if not store.get("title"):
            t = o.get("title") or o.get("display_title") or ""
            if t:
                store["title"] = t
        if not store.get("author"):
            user = o.get("user") or {}
            a = (user.get("nickname") or user.get("nick_name") or "").strip()
            if a:
                store["author"] = a
        if not store.get("cover"):
            cov = o.get("cover") or o.get("image") or o.get("images") or ""
            if isinstance(cov, list):
                cov = cov[0] if cov else ""
            # 防止把作者头像（sns-avatar*/avatar/...）误当封面：
            # 头像 URL 出现在部分 user 对象的 cover 字段里，必须排除。
            if (isinstance(cov, str) and cov and "/avatar/" not in cov
                    and "sns-avatar" not in cov and "avatar-" not in cov):
                store["cover"] = cov
        for v in o.values():
            _xhs_scan(v, store)
    elif isinstance(o, list):
        for v in o:
            _xhs_scan(v, store)


def _xhs_extract_main(o, target_id: str, store: dict) -> None:
    """只从 id==target_id 的「主笔记」提取标题/作者/封面/互动数据，避免把页面右侧
    「相关推荐」流里的其它笔记标题/作者误当成当前笔记（此前会导致下载文件名对不上视频）。

    小红书笔记详情页在加载主笔记的同时，还会异步拉取推荐流（同款 note_card 结构），
    _xhs_scan 的「首次命中即写入」会偶发抓到推荐笔记的 display_title。
    """
    if isinstance(o, dict):
        nid = (o.get("id") or o.get("note_id") or "").strip()
        nc = o.get("note_card")
        if target_id and nid == target_id and isinstance(nc, dict):
            t = (nc.get("display_title") or nc.get("title")
                 or o.get("display_title") or o.get("title") or "").strip()
            if t:
                store["title"] = t
            user = nc.get("user") or {}
            a = (user.get("nickname") or user.get("nick_name") or "").strip()
            if a:
                store["author"] = a
            info = nc.get("interact_info") or {}
            if not store.get("likes"):
                store["likes"] = _to_int(info.get("liked_count"))
            if not store.get("views"):
                store["views"] = _to_int(info.get("viewed_count") or info.get("view_count"))
            vid = nc.get("video") or {}
            if not store.get("duration"):
                d = vid.get("time") or vid.get("duration")
                if d:
                    dd = _to_int(d)
                    store["duration"] = dd // 1000 if dd > 100000 else dd
            if not store.get("cover"):
                cov = nc.get("cover") or ""
                if isinstance(cov, dict):
                    cov = cov.get("url_default") or cov.get("url") or ""
                if isinstance(cov, str) and cov and "/avatar/" not in cov:
                    store["cover"] = cov
        for v in o.values():
            _xhs_extract_main(v, target_id, store)
    elif isinstance(o, list):
        for v in o:
            _xhs_extract_main(v, target_id, store)


def enrich_xhs_via_page(page, notes: list, max_nav: int = 30) -> None:
    """补全小红书搜索结果缺失的时长/播放量。

    关键事实（已实测）：小红书**搜索接口**的 note_card 只含
    display_title / user / interact_info.liked_count / cover / image_list，
    **没有 video 字段也没有 viewed_count**——所以标题/点赞/封面能从搜索接口拿全，
    但「时长」「播放量」天生缺失。这两类数据只存在于**笔记详情页**：
      - 时长：详情页 <video> 元素的真实 duration（秒），最可靠；
      - 播放量：详情页正文里的「X 浏览 / X 次播放」文本。
    因此这里逐个打开视频笔记详情页，读取真实时长 + 播放量（原地补全）。
    仅处理 xiaohongshu 的视频笔记且时长缺失的条目；图片笔记无时长，直接跳过。
    用 max_nav 限制导航次数，避免极端情况下搜索被拖得太久。
    """
    targets = []
    for v in notes:
        if v.platform != "xiaohongshu":
            continue
        if v.duration and v.views:        # 已完整则跳过
            continue
        if not getattr(v, "is_video", False):   # 图文笔记无时长
            continue
        m = re.search(r"explore/([0-9a-f]{16,32})", v.url)
        if m:
            targets.append((m.group(1), v))
    if not targets:
        return
    targets = targets[:max_nav]
    for nid, v in targets:
        try:
            page.goto(v.url, timeout=25000, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass
        # 详情页视频是懒加载的：先等 <video> 元素出现，再触发播放以加载真实时长。
        try:
            page.wait_for_selector("video", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        try:
            page.eval_on_selector(
                "video",
                "v => { if (v) { try { v.muted=true; v.play().catch(()=>{}); } catch(e){} } }")
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(2500)
        # 标题兜底：个别搜索 XHR 偶发不返回 display_title，详情页 og:title 稳定可靠。
        if not v.title:
            try:
                t = page.evaluate("""() => {
                  const m = document.querySelector('meta[property="og:title"]');
                  if (m && m.content) return m.content;
                  const h = document.querySelector('h1');
                  if (h && h.innerText) return h.innerText.trim();
                  return document.title || '';
                }""")
                if t:
                    v.title = t.strip()
            except Exception:  # noqa: BLE001
                pass
        # 时长：真实 <video>.duration（秒）
        if not v.duration:
            try:
                d = page.eval_on_selector(
                    "video",
                    "v => (v && isFinite(v.duration) && v.duration > 0) ? Math.round(v.duration) : 0")
                if d:
                    v.duration = d
            except Exception:  # noqa: BLE001
                pass
        # 兜底：点击播放器区域后再等，部分站点到点击才懒加载 video 元素
        if not v.duration:
            try:
                page.click(".player-container, [class*='player'], video", timeout=3000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(2500)
            try:
                d = page.eval_on_selector(
                    "video",
                    "v => (v && isFinite(v.duration) && v.duration > 0) ? Math.round(v.duration) : 0")
                if d:
                    v.duration = d
            except Exception:  # noqa: BLE001
                pass
        if not v.duration:
            try:
                txt = (page.inner_text("body") or "")[:3000]
                v.duration = _match_duration(txt)
            except Exception:  # noqa: BLE001
                pass
        # 播放量：详情页正文里的「X 浏览 / X 次播放」
        if not v.views:
            try:
                txt = (page.inner_text("body") or "")[:4000]
                v.views = _match_metric(txt, ("浏览", "次播放", "观看", "plays", "views"))
            except Exception:  # noqa: BLE001
                pass


def resolve_xhs_links(urls: list[str], profile_root: str = "", cookie_file: str = "",
                      headless: bool = True, timeout: int = 40) -> list[dict]:
    """批量解析小红书笔记链接：用同一个登录态浏览器上下文逐个打开，
    截获真实视频直链 + 完整元数据（标题/时长/点赞/播放/作者/封面）。

    返回 list[dict]，每条含：
        url, nid, title, duration, likes, views, author, cover, media_url, platform
    任一链接失败不影响其余。相比逐条 resolve_xhs_media_url，本函数只开一个浏览器
    上下文（多页面），批量解析小红书链接时速度快很多，且直接产出带封面的条目，
    正好喂给「批量下载」卡片与下载流程（封面预览 + 直链复用）。
    """
    if not urls:
        return []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        return [{"url": u, "error": f"未安装 playwright：{e}"} for u in urls]
    profile = os.path.join(profile_root, "xiaohongshu") if profile_root else ""
    results: list[dict] = [{"url": u} for u in urls]
    found_all: dict[int, dict] = {}

    def on_resp(resp, idx):
        try:
            u = resp.url
            if "sns-video" in u and ("xhscdn" in u or ".mp4" in u or ".m3u8" in u):
                found_all.setdefault(idx, {}).setdefault("media_url", u)
                return
            if "xiaohongshu.com/api/" not in u or resp.status >= 400:
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            _xhs_scan(resp.json(), found_all.setdefault(idx, {}))
        except Exception:  # noqa: BLE001
            pass

    try:
        with sync_playwright() as p:
            # 始终用全新 context + 注入最新 cookie（来自 netscape 文件），不使用持久化 profile，
            # 避免 profile 被平台反爬标记后取不到直链。
            browser = p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled",
                      "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
            ctx = browser.new_context(user_agent=DEFAULT_UA,
                                      viewport={"width": 1360, "height": 900})
            objs = netscape_to_pw_objs(cookie_file)
            if objs:
                try:
                    ctx.add_cookies(objs)
                except Exception:  # noqa: BLE001
                    pass
            for i, url in enumerate(urls):
                found = found_all.setdefault(i, {})
                page = ctx.new_page()
                page.on("response", lambda r, _i=i: on_resp(r, _i))
                try:
                    page.goto(url, timeout=45000, wait_until="domcontentloaded")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    page.eval_on_selector(
                        "video",
                        "v => { if (v && v.play) { try { v.muted=true; v.play().catch(()=>{}); } catch(e){} } }")
                except Exception:  # noqa: BLE001
                    pass
                for sel in (".player-container", ".cover", "[class*='player']", "[class*='play']", "video"):
                    try:
                        page.click(sel, timeout=1500)
                        break
                    except Exception:  # noqa: BLE001
                        continue
                for _ in range(timeout):
                    if found.get("media_url"):
                        break
                    try:
                        page.eval_on_selector(
                            "video",
                            "v => { if (v) { try { v.muted=true; v.play().catch(()=>{}); } catch(e){} } }")
                    except Exception:  # noqa: BLE001
                        pass
                    page.wait_for_timeout(1000)
                # 真实时长：<video>.duration（秒）。搜索/feed 接口都不含 video 字段，
                # 只有详情页 <video> 元素能给出准确时长。
                if not found.get("duration"):
                    try:
                        d = page.eval_on_selector(
                            "video",
                            "v => (v && isFinite(v.duration) && v.duration > 0) ? Math.round(v.duration) : 0")
                        if d:
                            found["duration"] = d
                    except Exception:  # noqa: BLE001
                        pass
                # 兜底：<video> 当前源
                if not found.get("media_url"):
                    try:
                        src = page.eval_on_selector("video", "v => v ? (v.currentSrc || v.src) : ''")
                        if src and "xhscdn" in src and "blob:" not in src:
                            found["media_url"] = src
                    except Exception:  # noqa: BLE001
                        pass
                # 播放量：详情页正文里的「X 浏览 / X 次播放」（笔记接口不返回 viewed_count）
                if not found.get("views"):
                    try:
                        txt = (page.inner_text("body") or "")[:4000]
                        found["views"] = _match_metric(
                            txt, ("浏览", "次播放", "观看", "plays", "views"))
                    except Exception:  # noqa: BLE001
                        pass
                # 封面：详情页接口/feed API 在本上下文都取不到 cover 直链，
                # 因此从渲染后的 DOM 取主笔记封面：
                #   1) 带 cover/player 类的容器内的 <img>（最可能是主笔记封面）
                #   2) 文档顺序中第一张【非头像 / 非 data / 非通用站点图】的 xhscdn 图片
                #      （小红书详情页中作者头像先出现，随后才是主笔记封面图，故取第一张）
                #   3) og:image 兜底（注意它常是通用站点图，仅作最后兜底）
                # 必须排除：作者头像(sns-avatar)、通用站点图(picasso-static/fe-platform)。
                if not found.get("cover"):
                    try:
                        cov = page.evaluate("""() => {
                            const sel = '.player-container img, [class*="cover"] img, '
                                       + '[class*="Cover"] img, .note-content img, .note-detail img';
                            const el = document.querySelector(sel);
                            if (el && el.src && !el.src.startsWith('data:') && !/avatar/.test(el.src)) {
                                return el.src;
                            }
                            const imgs = Array.from(document.images).map(i => i.src).filter(Boolean);
                            for (const s of imgs) {
                                if (s.startsWith('data:')) continue;
                                if (/avatar/.test(s)) continue;
                                if (/picasso-static/.test(s) && /fe-platform/.test(s)) continue;
                                if (/xhscdn|sns-img|sns-cover|sns-webpic/.test(s)) return s;
                            }
                            const og = document.querySelector('meta[property="og:image"]');
                            return (og && og.content) ? og.content : '';
                        }""")
                        if cov and isinstance(cov, str) and "avatar" not in cov:
                            if cov.startswith("//"):
                                cov = "https:" + cov
                            found["cover"] = cov
                    except Exception:  # noqa: BLE001
                        pass
                # 标题兜底：用详情页 og:title（最稳），避免抓到「猜你想搜」之类推荐栏标题。
                # 仅当当前标题为空或明显是推荐栏文案时才覆盖，保留接口返回的真实标题。
                try:
                    t = page.evaluate("""() => {
                        const m = document.querySelector('meta[property="og:title"]');
                        if (m && m.content) return m.content;
                        const h = document.querySelector('h1');
                        if (h && h.innerText) return h.innerText.trim();
                        return document.title || '';
                    }""")
                    if t:
                        cur = (found.get("title") or "")
                        if not cur or "猜你" in cur or "推荐" in cur or "相关" in cur:
                            found["title"] = t.strip()
                except Exception:  # noqa: BLE001
                    pass
                # 兜底：主动请求 feed 接口（自带浏览器签名头）。
                # 即便已拿到直链/标题也要跑——它给出权威的 note_card.cover（视频封面）
                # 以及点赞/时长/播放等元数据，是批量封面预览最可靠的来源。
                if (not found.get("note_cover") or not found.get("title")
                        or not found.get("media_url")):
                    try:
                        m = re.search(r"explore/([0-9a-f]{16,32})", url)
                        if m:
                            nid = m.group(1)
                            data = page.evaluate(
                                """(nid) => fetch('https://edith.xiaohongshu.com/api/sns/web/v1/feed', {
                                    method:'POST', headers:{'content-type':'application/json'},
                                    body: JSON.stringify({source_note_id:nid, image_formats:['jpg','webp','avif'],
                                    extra:'{"need_delete_master":false}', share_info:{type:'note',title:'',desc:''}, is_async_request:false})
                                }).then(r=>r.json()).then(j=>{
                                const it=((j.data||{}).items)||[]; const nc=(it[0]||{}).note_card||it[0]||{};
                                const v=nc.video||{}; const mm=(v.media||[])[0]||{};
                                const ncCov = nc.cover||'';
                                const noteCover = (typeof ncCov==='string') ? ncCov
                                    : (ncCov && (ncCov.url_default||ncCov.url_pre||ncCov.url)) || '';
                                return {media: (v.consumer&&v.consumer.url)||mm.url||'',
                                    title: nc.title||nc.display_title||'', dur: v.time||v.duration||0,
                                    likes: ((nc.interact_info||{}).liked_count)||'',
                                    views: ((nc.interact_info||{}).viewed_count)||'',
                                    cover: (v.cover&&(v.cover.url_default||v.cover.url_pre||v.cover.url))||'',
                                    note_cover: noteCover||''};
                                })""", nid)
                            if data:
                                if data.get("media") and ("xhscdn" in data["media"] or ".mp4" in data["media"]):
                                    found.setdefault("media_url", data["media"])
                                for k in ("title", "dur", "likes", "views", "cover", "note_cover"):
                                    if data.get(k) and not found.get(k):
                                        found[k] = data[k]
                    except Exception:  # noqa: BLE001
                        pass
                page.close()
            ctx.close()
            if browser:
                browser.close()
    except Exception as e:  # noqa: BLE001
        for r in results:
            if not r.get("title") and not r.get("error"):
                r["error"] = f"浏览器解析失败：{e}"

    for i, r in enumerate(results):
        f = found_all.get(i, {})
        mm = re.search(r"explore/([0-9a-f]{16,32})", r["url"])
        r["nid"] = mm.group(1) if mm else ""
        r["title"] = (f.get("title") or "").strip()
        r["duration"] = _to_int(f.get("duration"))
        r["likes"] = _to_int(f.get("likes"))
        r["views"] = _to_int(f.get("views"))
        r["author"] = (f.get("author") or "").strip()
        # 封面优先用 note_card.cover（笔记权威封面/视频海报帧），其次 video.cover；
        # 双重排除作者头像，确保批量卡片显示的是真实视频封面而非头像。
        _cover = (f.get("note_cover") or f.get("cover") or "").strip()
        if "avatar" in _cover:
            _cover = (f.get("cover") or "").strip()
        if "avatar" in _cover:
            _cover = ""
        r["cover"] = _cover
        r["media_url"] = (f.get("media_url") or "").strip()
        r["platform"] = "xiaohongshu"
    return results


def validate_collection(items: list[VideoLink]) -> list[str]:
    """采集结果自检：返回问题清单（空 = 全部符合预期）。

    校验项：
      - 小红书：链接必须带 xsec_token（否则打不开），标题不应为空。
      - B站/抖音/快手：本应有完整标题，若出现空标题视为异常（多半是通道失败）。
    这是「每次采集后自动核对输出」的核心，便于第一时间发现「看似成功实则字段缺失」。
    """
    issues: list[str] = []
    by_plat: dict[str, list[VideoLink]] = {}
    for it in items:
        by_plat.setdefault(it.platform, []).append(it)

    for it in by_plat.get("xiaohongshu", []):
        if "xsec_token" not in it.url:
            issues.append(f"小红书链接缺少 xsec_token（无法打开）：{it.url}")
        if not it.title:
            issues.append(f"小红书标题为空：{it.url}")

    for plat in ("bilibili", "douyin", "kuaishou"):
        lst = by_plat.get(plat)
        if not lst:
            continue
        empty = sum(1 for it in lst if not it.title)
        if empty:
            issues.append(f"{plat} 有 {empty}/{len(lst)} 条标题为空（异常，多半是接口/登录态失败）")
    return issues


class HttpClient:
    """极简 HTTP 客户端：统一 UA、节流、**按域名下发 Cookie**。

    设计说明：早期版本只有一个全局 Cookie 头，多平台混用会把 A 站的凭据发给 B 站
    （既泄露又容易触发风控）。现在按域名隔离，只有匹配的域名才会带上对应 Cookie。
    """

    def __init__(self, delay: float = 1.0, cookies: str | None = None):
        self.delay = max(0.0, delay)
        self._last = 0.0
        self._cookie_header = cookies or ""          # 兜底：未匹配到域名时使用
        self.domain_cookies: dict[str, str] = {}     # 'douyin.com' -> 'a=1; b=2'

    # ---------------- Cookie 管理 ----------------
    def set_domain_cookies(self, domain: str, header: str) -> None:
        if domain and header:
            self.domain_cookies[domain.lstrip(".")] = header

    def merge_domain_cookies(self, domain: str, header: str) -> None:
        """把新 Cookie 合并进已有域名 Cookie（同名覆盖）。"""
        domain = domain.lstrip(".")
        cur = self._parse_header(self.domain_cookies.get(domain, ""))
        cur.update(self._parse_header(header))
        self.domain_cookies[domain] = "; ".join(f"{k}={v}" for k, v in cur.items())

    @staticmethod
    def _parse_header(h: str) -> dict:
        out = {}
        for seg in (h or "").split(";"):
            seg = seg.strip()
            if "=" in seg:
                k, v = seg.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def cookie_for(self, url: str) -> str:
        host = urllib.parse.urlparse(url).hostname or ""
        for domain, header in self.domain_cookies.items():
            if host == domain or host.endswith("." + domain):
                return header
        return self._cookie_header

    def load_netscape_cookies(self, path: str, domain: str = "") -> None:
        """加载 Netscape cookies.txt；不指定 domain 时按文件里的 domain 列自动归域。"""
        try:
            grouped: dict[str, list[str]] = {}
            fallback: list[str] = []
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    cols = line.split("\t")
                    if len(cols) < 7:
                        continue
                    pair = f"{cols[5]}={cols[6]}"
                    dom = (domain or cols[0]).lstrip(".")
                    if dom:
                        grouped.setdefault(dom, []).append(pair)
                    else:
                        fallback.append(pair)
            for dom, parts in grouped.items():
                self.merge_domain_cookies(dom, "; ".join(parts))
            if fallback:
                self._cookie_header = "; ".join(fallback)
            total = sum(len(v) for v in grouped.values()) + len(fallback)
            if total:
                print(f"[cookies] 已加载 {total} 条，覆盖域名：{', '.join(grouped) or '(全局)'}")
        except Exception as e:  # noqa: BLE001
            print(f"[cookies] 加载失败，已忽略: {e}")

    # ---------------- 请求 ----------------
    def _throttle(self) -> None:
        if self.delay <= 0:
            return
        now = time.time()
        wait = self.delay - (now - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _headers(self, url: str, extra: dict | None = None) -> dict:
        h = {"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
        ck = self.cookie_for(url)
        if ck:
            h["Cookie"] = ck
        if extra:
            h.update(extra)
        return h

    def _absorb_set_cookie(self, url: str, resp) -> None:
        """把响应里的 Set-Cookie 自动收进对应域名的 Cookie 罐。

        很多平台（快手的 did、B 站的 buvid）靠首次访问下发设备指纹 Cookie，
        不吸收就永远拿不到，后续接口直接被风控拦。这一步是「无 Cookie 也能跑」的关键。
        """
        try:
            raw = resp.headers.get_all("Set-Cookie") or []
        except Exception:  # noqa: BLE001
            return
        host = urllib.parse.urlparse(url).hostname or ""
        pairs = []
        for line in raw:
            first = line.split(";", 1)[0].strip()
            if "=" in first:
                k, v = first.split("=", 1)
                if k.strip() and v.strip() not in ("", "deleted"):
                    pairs.append(f"{k.strip()}={v.strip()}")
        if not pairs:
            return
        # 归到已知主域，未知则用二级域
        target = next((d for d in self.domain_cookies if host.endswith(d)), "")
        if not target:
            parts = host.split(".")
            target = ".".join(parts[-2:]) if len(parts) >= 2 else host
        self.merge_domain_cookies(target, "; ".join(pairs))

    def get(self, url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 15):
        """返回 (text, final_url)；失败返回 (None, None)。"""
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        self._throttle()
        try:
            req = urllib.request.Request(url, headers=self._headers(url, headers))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self._absorb_set_cookie(url, resp)
                return resp.read().decode("utf-8", "ignore"), resp.geturl()
        except urllib.error.URLError as e:
            print(f"[http] 请求失败 {url}: {e}")
            return None, None
        except Exception as e:  # noqa: BLE001
            print(f"[http] 未知错误 {url}: {e}")
            return None, None

    def post_json(self, url: str, payload: dict, headers: dict | None = None, timeout: int = 20):
        """POST JSON，返回解析后的 dict；失败返回 None。"""
        self._throttle()
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        h = self._headers(url, {"Content-Type": "application/json"})
        if headers:
            h.update(headers)
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self._absorb_set_cookie(url, resp)
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as e:  # noqa: BLE001
            print(f"[http] POST 失败 {url}: {e}")
            return None


class BilibiliSearcher:
    """Bilibili 视频搜索（WBI 签名）+ 详情 enrich。"""

    def __init__(self, client: HttpClient):
        self.c = client
        self._img_key = ""
        self._sub_key = ""

    def _ensure_keys(self) -> None:
        if self._img_key and self._sub_key:
            return
        self.c.get("https://www.bilibili.com")
        # spi 接口返回 buvid3 / buvid4；两者都注入 Cookie 可绕过大部分风控凭证挑战
        spi_text, _ = self.c.get("https://api.bilibili.com/x/frontend/finger/spi")
        if spi_text:
            try:
                sp = json.loads(spi_text).get("data", {}) or {}
                b3, b4 = sp.get("b_3", ""), sp.get("b_4", "")
                ck = "; ".join(f"{k}={v}" for k, v in (("buvid3", b3), ("buvid4", b4)) if v)
                if ck:
                    self.c.merge_domain_cookies("bilibili.com", ck)
            except Exception:  # noqa: BLE001
                pass
        nav_text, _ = self.c.get("https://api.bilibili.com/x/web-interface/nav")
        if not nav_text:
            return
        try:
            wbi = json.loads(nav_text).get("data", {}).get("wbi_img", {})
            # 注意：img_url 与 sub_url 当前均为 .png 后缀
            img = re.search(r"([0-9a-f]+)\.(?:png|json)", wbi.get("img_url", ""))
            sub = re.search(r"([0-9a-f]+)\.(?:png|json)", wbi.get("sub_url", ""))
            self._img_key = img.group(1) if img else ""
            self._sub_key = sub.group(1) if sub else ""
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _mixin_key(orig: str) -> str:
        return "".join(orig[i] for i in WBI_TABLE if i < len(orig))[:32]

    def _sign(self, params: dict) -> dict:
        self._ensure_keys()
        mixin = self._mixin_key(self._img_key + self._sub_key)
        params["wts"] = int(time.time())
        params = dict(sorted(params.items()))
        query = urllib.parse.urlencode(params)
        params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
        return params

    def search(self, keyword: str, max_results: int) -> list[VideoLink]:
        out: list[VideoLink] = []
        for page in range(1, 21):
            if len(out) >= max_results:
                break
            params = self._sign({"keyword": keyword, "page": page})
            text, _ = self.c.get(
                "https://api.bilibili.com/x/web-interface/wbi/search/all/v2",
                params=params,
            )
            if not text:
                break
            try:
                payload = json.loads(text)
            except Exception:  # noqa: BLE001
                print("[bili] 响应解析失败，可能触发风控，建议在 Cookie 面板配置 Bilibili 登录态")
                break
            if payload.get("code") not in (0, None):
                print(f"[bili] 接口返回 code={payload.get('code')} msg={payload.get('message')}")
                break
            for group in payload.get("data", {}).get("result", []) or []:
                if group.get("result_type") != "video":
                    continue
                for item in group.get("data", []) or []:
                    if not isinstance(item, dict):
                        continue
                    bvid = item.get("bvid")
                    if not bvid:
                        continue
                    # 哔哩哔哩封面：item.pic 形如 //i0.hdslb.com/... 或 https://...，归一化为 https
                    _pic = (item.get("pic") or "").strip()
                    if _pic.startswith("//"):
                        _pic = "https:" + _pic
                    out.append(VideoLink(
                        platform="bilibili",
                        keyword=keyword,
                        title=re.sub(r"<[^>]+>", "", item.get("title", "")),
                        url=f"https://www.bilibili.com/video/{bvid}",
                        cover=_pic,
                    ))
                    if len(out) >= max_results:
                        break
            time.sleep(0.3)
        return out[:max_results]

    def enrich(self, items: list[VideoLink]) -> None:
        """为 Bilibili 视频补充时长/点赞/播放/UP主（原地修改）。"""
        for it in items:
            if it.platform != "bilibili":
                continue
            m = re.search(r"BV[0-9A-Za-z]+", it.url)
            if not m:
                continue
            bvid = m.group(0)
            text, _ = self.c.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid})
            if not text:
                continue
            try:
                d = json.loads(text).get("data", {})
            except Exception:  # noqa: BLE001
                continue
            it.title = (d.get("title") or it.title).strip()
            it.author = d.get("owner", {}).get("name", "")
            it.duration = int(d.get("duration", 0) or 0)
            stat = d.get("stat", {}) or {}
            it.likes = int(stat.get("like", 0) or 0)
            it.views = int(stat.get("view", 0) or 0)
            time.sleep(0.2)


class KuaishouSearcher:
    """快手 Web 搜索（GraphQL）。

    快手 Web 端搜索走 https://www.kuaishou.com/graphql，**无需 JS 签名**，
    但必须带上有效 Cookie（至少 did 设备指纹；带登录态成功率更高、翻页更稳）。
    因此配置 Cookie 后，快手是三个平台里元数据最完整的：标题/时长/点赞/播放/作者一次拿全。
    """

    ENDPOINT = "https://www.kuaishou.com/graphql"

    def __init__(self, client: HttpClient):
        self.c = client

    def _has_cookie(self) -> bool:
        return bool(self.c.cookie_for("https://www.kuaishou.com/"))

    def bootstrap(self, keyword: str = "") -> None:
        """无登录 Cookie 时，先走一遍网页拿设备指纹（did）。

        did 是快手下发在 Set-Cookie 里的；HttpClient 会自动吸收。
        若响应头没给，再从页面内联 JSON 里兜底提取。
        """
        header = self.c.cookie_for("https://www.kuaishou.com/")
        if "did=" in header:
            return
        url = ("https://www.kuaishou.com/search/video?searchKey=" + urllib.parse.quote(keyword)
               if keyword else "https://www.kuaishou.com/new-reco")
        text, _ = self.c.get(url)
        if "did=" in self.c.cookie_for("https://www.kuaishou.com/"):
            return
        if text:
            m = re.search(r'"did"\s*:\s*"([^"]+)"', text) or re.search(r'did=(web_[A-Za-z0-9]+)', text)
            if m:
                self.c.merge_domain_cookies("kuaishou.com", f"did={m.group(1)}")

    # 快手 GraphQL 顶层 result 状态码 -> 可执行的中文说明
    RESULT_HINTS = {
        1: "成功",
        2: "请求缺少签名(__NS_sig3)或被风控拦截：快手 Web 搜索需要签名，纯 HTTP 无法生成。"
           "请改用「浏览器搜索」通道（勾选 browser_search / 安装 Playwright）或分享链接解析。",
        400002: "触发反爬滑块验证（ANTICRAWL）。匿名设备指纹不足以通过，"
                "请在 Cookie 面板执行「浏览器登录获取」或粘贴登录态 Cookie 后重试。",
        400003: "参数异常或接口已变更。",
        400004: "需要登录（未携带有效登录态 Cookie）。",
        109: "登录态已失效，请重新登录获取 Cookie。",
    }

    def _diagnose(self, data, root: dict, node: dict) -> str:
        """把「拿不到数据」翻译成用户能直接行动的一句话，而不是空指针式沉默。"""
        if not data:
            return "[kuaishou] 请求失败：网络不通或被直接拒绝。"
        if (data or {}).get("errors"):
            return f"[kuaishou] GraphQL 报错：{str(data['errors'])[:160]}"
        code = root.get("result") or node.get("result")
        if code and code != 1:
            hint = self.RESULT_HINTS.get(code, "未知状态码，可能接口有变更。")
            extra = "（响应中含验证码地址，确认是风控拦截）" if root.get("url") else ""
            return f"[kuaishou] 接口返回 result={code}：{hint}{extra}"
        if not self._logged_in_hint():
            return ("[kuaishou] 未返回数据。当前只有匿名设备 Cookie，"
                    "快手搜索通常需要登录态：--cookie-login kuaishou 或在 Web 面板一键登录。")
        return "[kuaishou] 未返回数据，登录态可能已过期，请重新获取 Cookie。"

    def _logged_in_hint(self) -> bool:
        h = self.c.cookie_for("https://www.kuaishou.com/")
        return ("kuaishou.web.cp.api_ph" in h) or ("userId=" in h) or ("passToken" in h)

    def search(self, keyword: str, max_results: int) -> list[VideoLink]:
        self.bootstrap(keyword)
        if not self._has_cookie():
            print("[kuaishou] 无可用 Cookie，搜索大概率被拦截。请在 Cookie 面板导入或浏览器登录。")
        out: list[VideoLink] = []
        pcursor, session_id = "", ""
        referer = "https://www.kuaishou.com/search/video?searchKey=" + urllib.parse.quote(keyword)
        for _ in range(12):
            if len(out) >= max_results:
                break
            payload = {
                "operationName": "visionSearchPhoto",
                "variables": {"keyword": keyword, "pcursor": pcursor,
                              "page": "search", "searchSessionId": session_id},
                "query": KUAISHOU_GRAPHQL_QUERY,
            }
            data = self.c.post_json(self.ENDPOINT, payload, headers={
                "Origin": "https://www.kuaishou.com", "Referer": referer,
                "Accept": "*/*",
            })
            root = (data or {}).get("data") or {}
            node = root.get("visionSearchPhoto") or {}
            feeds = node.get("feeds") or []
            if not feeds:
                print(self._diagnose(data, root, node))
                break
            session_id = node.get("searchSessionId") or session_id
            for f in feeds:
                photo = f.get("photo") or {}
                pid = photo.get("id")
                if not pid:
                    continue
                out.append(VideoLink(
                    platform="kuaishou",
                    keyword=keyword,
                    title=(photo.get("caption") or "").replace("\n", " ").strip(),
                    url=f"https://www.kuaishou.com/short-video/{pid}",
                    author=((f.get("author") or {}).get("name") or ""),
                    duration=int((photo.get("duration") or 0) / 1000),  # 快手返回毫秒
                    likes=_to_int(photo.get("realLikeCount") or photo.get("likeCount")),
                    views=_to_int(photo.get("viewCount")),
                ))
                if len(out) >= max_results:
                    break
            pcursor = node.get("pcursor") or ""
            if not pcursor or pcursor == "no_more":
                break
            time.sleep(0.4)
        return out[:max_results]


class ShareResolver:
    """抖音/快手分享短链 -> 干净视频页 URL（可靠通道）。"""

    def __init__(self, client: HttpClient):
        self.c = client

    def resolve(self, raw: str) -> list[VideoLink]:
        links: list[VideoLink] = []
        urls = re.findall(r"https?://[^\s,，；;]+", raw)
        for u in urls:
            u = u.strip().strip("'\"")
            if "douyin.com" in u or "tiktok.com" in u or "v.douyin" in u:
                resolved = self._follow(u, r"/video/(\d+)", "https://www.douyin.com/video/")
                if resolved:
                    links.append(VideoLink("douyin", "(share)", "", resolved))
            elif "kuaishou.com" in u or "v.kuaishou" in u:
                resolved = self._follow(u, r"/short-video/([A-Za-z0-9]+)",
                                        "https://www.kuaishou.com/short-video/")
                if resolved:
                    links.append(VideoLink("kuaishou", "(share)", "", resolved))
        return links

    def _follow(self, url: str, pat: str, host: str) -> str | None:
        ref = "https://www.douyin.com/" if "douyin" in url else "https://www.kuaishou.com/"
        text, final = self.c.get(url, headers={"Referer": ref})
        if not final:
            return None
        m = re.search(pat, final)
        if m:
            return host + m.group(1)
        if "/video/" in final or "/short-video/" in final:
            return final
        return None


class BrowserSearcher:
    """多平台 Playwright 搜索（配置表驱动，见 BROWSER_SEARCH），支持登录态。

    两种登录态注入方式（优先级从高到低）：
      1. 持久化 profile：cookies/profiles/<platform>/ —— 由 Cookie 面板「浏览器登录」生成，最稳。
      2. 注入 Cookie：把已保存的 Cookie 写入浏览器上下文。

    元数据获取策略：
      - 抖音/快手：拦截搜索 XHR 直接吃 JSON，拿到标题/点赞/播放/时长/作者（最全）。
      - 其余平台：DOM 兜底，只保证「纯净链接」——这正是 yt-dlp 需要的输入。
    真实浏览器还顺带解决了快手 __NS_sig3、抖音 a_bogus 这类纯 HTTP 无法生成的签名。
    """

    def __init__(self, cookies_by_platform: dict[str, str] | None = None,
                 profile_root: str | None = None, headless: bool = True):
        self.cookies_by_platform = cookies_by_platform or {}
        self.profile_root = profile_root
        self.headless = headless
        self.available = False
        try:
            import importlib.util
            self.available = importlib.util.find_spec("playwright") is not None
        except Exception:  # noqa: BLE001
            self.available = False
        # 浏览器实例复用（跨关键词共享同一 page，避免重复启动开销）
        self._p = None
        self._ctx = None
        self._browser = None
        self._page = None
        self._open = False
        self._platform = None
        self._keyword = None
        self._add = None
        self._out = None
        self._seen = None
        self._xhs_by_id = None
        # 0 结果自诊断：记录最近一次 aweme/v1 响应与是否见过该 XHR（区分「XHR 未触发」与「响应结构未命中」）。
        self._last_aweme_raw = None
        self._saw_aweme_xhr = False
        # 抖音关键词搜索接口返回空 data + search_nil_type=verify_check 时置位（被风控验证码拦截）。
        self._verify_check = False

    @staticmethod
    def _dechunk_bytes(raw: bytes) -> bytes:
        """去除 HTTP chunked 传输分帧：抖音 general/search/stream 接口响应体首部带 'SIZE\\r\\n'
        分块长度标记（如 '14c8\\r\\n{...}\\r\\n0\\r\\n\\r\\n'），按字节尺寸精确截取数据块，
        避免直接 json() 因分帧标记而解析失败。"""
        if not isinstance(raw, (bytes, bytearray)):
            return raw
        out = bytearray()
        i, n = 0, len(raw)
        while i < n:
            j = i
            while j < n and raw[j] in b"0123456789abcdefABCDEF":
                j += 1
            if j == i:
                out += raw[i:]; break
            try:
                size = int(raw[i:j], 16)
            except ValueError:
                out += raw[i:]; break
            i = j
            if raw[i:i + 2] == b"\r\n":
                i += 2
            elif raw[i:i + 1] == b"\n":
                i += 1
            else:
                out += raw[i:]; break
            if size == 0:
                break
            out += raw[i:i + size]
            i += size
            if raw[i:i + 2] == b"\r\n":
                i += 2
            elif raw[i:i + 1] == b"\n":
                i += 1
        return bytes(out)

    @staticmethod
    def _read_json(resp) -> "dict | list | None":
        """容错读取响应 JSON：兼容 chunked 分帧（抖音 stream 接口）与 NDJSON，避免 json() 抛错。
        返回解析后的 dict/list；解析失败返回 None。"""
        try:
            raw = resp.body()
        except Exception:  # noqa: BLE001
            try:
                raw = resp.text()
            except Exception:  # noqa: BLE001
                return None
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = BrowserSearcher._dechunk_bytes(raw)
            text = raw.decode("utf-8", "ignore")
        else:
            text = str(raw)
        text = text.strip()
        try:
            return json.loads(text)
        except Exception:  # noqa: BLE001
            pass
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
        return None

    @staticmethod
    def supports(platform: str) -> bool:
        """该平台是否已在 BROWSER_SEARCH 配置表中登记。"""
        return platform in BROWSER_SEARCH

    # ---- 元数据提取：搜索结果页 与 单视频详情页 共用同一套 XHR 解析 ----
    @staticmethod
    def _build_douyin(info, cfg, keyword) -> "VideoLink | None":
        if not isinstance(info, dict):
            return None
        aid = info.get("aweme_id")
        if not aid:
            return None
        stat = info.get("statistics") or {}
        video = info.get("video") or {}
        _vc = video.get("cover") or {}
        if isinstance(_vc, dict):
            _ul = _vc.get("url_list") or []
            _cover = _ul[0] if _ul else (_vc.get("url") or "")
        elif isinstance(_vc, str):
            _cover = _vc
        else:
            _cover = ""
        # 直接视频直链（CDN 直连下载用，绕开 yt-dlp）。
        # 【2026-09 抖音改版】download_addr 已变为「带水印」流（URL 含 watermark=1，
        # 实测 api-play.amemv.com/...&watermark=1），不再是过去的无水印源；
        # 真正的无水印源是 play_addr_h264 / play_addr_bytevc1 / bit_rate[].play_addr
        # （域名 api-play-hl.amemv.com/aweme/v1/play/，URL 不携带 watermark 参数）。
        # 故把无水印源提到最前；并统一按「URL 是否含 watermark=1」分桶：
        # 无水印桶优先取用，带水印桶仅作兜底（取用时把 watermark=1 改写为 0 尝试去水印）。
        # 另：跳过背景音乐(ies-music)：图文类内容的 play_addr 仅为 BGM，并非视频本体，
        # 必须过滤，否则会把「图文」误判为「有视频」并下载到一段音乐。
        media_url = ""
        _clean: list[str] = []   # 无水印候选（按下方优先级顺序）
        _wmed: list[str] = []    # 带水印候选（兜底）

        def _is_wm(u: str) -> bool:
            """带水印特征：显式 watermark=1 参数，或 playwm 路径（wm=watermark）。"""
            return ("watermark=1" in u) or ("/playwm/" in u)

        def _dew(u: str) -> str:
            """兜底去水印：playwm 路径→play，watermark=1→watermark=0。"""
            return u.replace("/playwm/", "/play/").replace("watermark=1", "watermark=0")

        def _collect(node) -> None:
            for _u in ((node.get("url_list") if isinstance(node, dict) else None) or []):
                if not _u or "ies-music" in _u:
                    continue
                (_wmed if _is_wm(_u) else _clean).append(_u)

        # 1) 官方无水印直出版（h264 兼容性最佳，其次 h265）
        _collect(video.get("play_addr_h264") or {})
        _collect(video.get("play_addr_bytevc1") or {})
        # 2) 多码率高清源（api-play-hl，无水印）：按码率从高到低，优先取最好画质
        _brs = [b for b in (video.get("bit_rate") or []) if isinstance(b, dict)]
        _brs.sort(key=lambda b: (b.get("bit_rate") or 0), reverse=True)
        for _b in _brs:
            _collect(_b.get("play_addr") or {})
        # 3) 兜底：download_addr（新版本身带水印）→ play_addr
        _collect(video.get("download_addr") or {})
        _collect(video.get("play_addr") or {})
        if _clean:
            media_url = _clean[0]
        elif _wmed:
            media_url = _dew(_wmed[0])
        return VideoLink(
            platform="douyin", keyword=keyword,
            title=(info.get("desc") or "").replace("\n", " ").strip(),
            url=cfg.get("host", "") + str(aid),
            author=((info.get("author") or {}).get("nickname")
                    or (info.get("author") or {}).get("unique_id") or ""),
            duration=int((video.get("duration") or info.get("duration") or 0) / 1000),
            likes=_to_int(stat.get("digg_count")),
            views=_to_int(stat.get("play_count") or stat.get("show_count")),
            cover=(_cover or ""),
            media_url=media_url,
        )

    @staticmethod
    def _build_kuaishou(photo, author, cfg, keyword) -> "VideoLink | None":
        if not isinstance(photo, dict):
            return None
        pid = photo.get("id")
        if not pid:
            return None
        dur = int((photo.get("duration") or 0))
        if dur > 0 and dur < 3600 * 1000:
            dur = int(dur / 1000)
        # 直接视频直链（浏览器/CDN 直连下载用，绕开 yt-dlp——yt-dlp 无快手提取器）。
        # playUrl 为带签名的 CDN 地址；mainMvUrls 为多清晰度列表，取首个可用地址。
        media_url = (photo.get("playUrl") or "").strip()
        if not media_url:
            mv = photo.get("mainMvUrls") or []
            if isinstance(mv, list):
                for item in mv:
                    u = (item.get("url") if isinstance(item, dict) else "") or ""
                    if u and ("mp4" in u or "m3u8" in u):
                        media_url = u
                        break
        return VideoLink(
            platform="kuaishou", keyword=keyword,
            title=(photo.get("caption") or "").replace("\n", " ").strip(),
            url=cfg.get("host", "") + str(pid),
            author=((author or {}).get("name") or ""),
            duration=dur,
            likes=_to_int(photo.get("realLikeCount") or photo.get("likeCount")),
            views=_to_int(photo.get("viewCount")),
            cover=(photo.get("coverUrl") or ""),
            media_url=media_url,
        )

    def _on_response(self, resp, add, xhs_by_id, platform, keyword) -> None:
        """拦截接口 JSON，从搜索结果与单视频详情页提取元数据。按平台分支。"""
        try:
            url = resp.url
            if platform == "xiaohongshu":
                # 小红书搜索接口 v2 返回 note_card(标题/作者/点赞/视频类型)，token 新鲜有效。
                if "search/notes" not in url:
                    return
                try:
                    data = self._read_json(resp)
                except Exception:  # noqa: BLE001
                    return
                if data is None:
                    return
                d = (data or {}).get("data", {}) or {}
                items = d.get("items") or d.get("notes") or []
                for n in items:
                    v = _parse_xhs_note(n, keyword)
                    if v:
                        m = re.search(r"explore/([0-9a-f]{16,32})", v.url)
                        if m:
                            xhs_by_id[m.group(1)] = v
                return
            if platform == "kuaishou":
                # 搜索 feed 与单视频详情页(photo) 共用同一份 JSON：吃它拿标题/作者/点赞/时长/photo.id
                if "/rest/v/search/feed" not in url and "/rest/v/photo/" not in url:
                    return
                try:
                    data = self._read_json(resp)
                except Exception:  # noqa: BLE001
                    return
                if data is None:
                    return
                node = data or {}
                feeds = node.get("feeds") or []
                if feeds:
                    for f in feeds:
                        photo = f.get("photo") or {}
                        v = self._build_kuaishou(photo, f.get("author"),
                                                 BROWSER_SEARCH.get("kuaishou"), keyword)
                        if v:
                            add(v)
                else:
                    photo = node.get("photo") or {}
                    if photo:
                        v = self._build_kuaishou(photo, node.get("author"),
                                                 BROWSER_SEARCH.get("kuaishou"), keyword)
                        if v:
                            add(v)
                return
            # 抖音：搜索 / 单视频详情(aweme/detail) / 作者作品列表(aweme/post)
            #       共用 aweme/v1 接口，统一从响应里抽取全部 aweme 对象后逐个构建。
            if "aweme/v1" not in url:
                return
            try:
                data = self._read_json(resp)
            except Exception:  # noqa: BLE001
                return
            if data is None:
                return
            # 0 结果自诊断：记录最近一次 aweme/v1 原始响应（截断），用于区分「XHR 未触发」与「结构未命中」。
            try:
                self._saw_aweme_xhr = True
                _raw = data if isinstance(data, (dict, list)) else str(data)
                self._last_aweme_raw = (url, json.dumps(_raw, ensure_ascii=False)[:2000])
                # 抖音关键词搜索接口被风控：空 data + search_nil_type=verify_check（验证码墙）。
                # 与登录态无关（Cookie 有效时下载/作者搜索仍正常），仅自动化浏览器会话被拦截。
                if isinstance(data, dict):
                    _nil = (data.get("search_nil_info") or {}).get("search_nil_type")
                    if _nil == "verify_check":
                        self._verify_check = True
            except Exception:  # noqa: BLE001
                pass
            awemes = []
            if isinstance(data, dict):
                if data.get("aweme_detail"):
                    awemes.append(data["aweme_detail"])
                # 作者作品列表接口 aweme/post 返回 aweme_list（直接就是 aweme 对象数组）
                for row in (data.get("aweme_list") or []):
                    if isinstance(row, dict) and row.get("aweme_id"):
                        awemes.append(row)
                _inner = data.get("data")
                if isinstance(_inner, list):
                    for row in _inner:
                        cand = row.get("aweme_info") or \
                            (row.get("aweme_mix_info") or {}).get("mix_items", [{}])[0]
                        if cand:
                            awemes.append(cand)
                elif isinstance(_inner, dict):
                    # data 字段为字典形态（部分接口把列表包在字典里）：
                    # 优先 aweme_list，其次顶层 aweme_info。若此处结构不符，0 结果诊断会打印原始响应。
                    for row in (_inner.get("aweme_list") or []):
                        if isinstance(row, dict) and row.get("aweme_id"):
                            awemes.append(row)
                    if _inner.get("aweme_info"):
                        awemes.append(_inner["aweme_info"])
            elif isinstance(data, list):
                # 兜底：响应本身就是 aweme 对象数组（少数接口形态）
                for row in data:
                    if isinstance(row, dict) and row.get("aweme_id"):
                        awemes.append(row)
            for info in awemes:
                v = self._build_douyin(info, BROWSER_SEARCH.get("douyin"), keyword)
                if v:
                    add(v)
        except Exception:  # noqa: BLE001
            return

    def resolve_one(self, platform: str, url: str, max_wait: int = 25) -> "VideoLink | None":
        """导航到单条视频页，复用搜索同款 XHR 拦截提取标题/作者/点赞/封面等元数据。
        仅对已在 BROWSER_SEARCH 登记、且支持 XHR 元数据的平台（抖音/快手/小红书）有效；
        其余平台（或浏览器不可用）返回 None，由调用方退回 yt-dlp。"""
        if not self.available or platform not in BROWSER_SEARCH:
            return None
        import os
        from playwright.sync_api import sync_playwright  # noqa: WPS433

        cfg = BROWSER_SEARCH.get(platform)
        found: list = []

        def add_first(v: "VideoLink") -> None:
            if v.url and not found:
                found.append(v)

        xhs_by_id: dict = {}
        try:
            with sync_playwright() as p:
                # 统一采用新版 headless + 反检测参数，规避 secsdk 风控滑块（否则元数据 XHR 被拦）。
                # 浏览器形态同 _open_browser：微博走持久化 profile（依赖完整登录态），
                # 其余平台走【全新 context + 注入干净 JSON cookie】（规避被风控标记的持久化 profile）。
                profile = os.path.join(self.profile_root, platform) if self.profile_root else ""
                viewport = {"width": 1360, "height": 900}
                use_profile = (platform == "weibo") and bool(profile and os.path.isdir(profile))
                objs = self._cookie_objs(platform)
                if use_profile:
                    ctx = p.chromium.launch_persistent_context(
                        profile, headless=self.headless, user_agent=DEFAULT_UA,
                        args=BROWSER_LAUNCH_ARGS, viewport=viewport,
                        locale="zh-CN", timezone_id="Asia/Shanghai")
                    browser = None
                else:
                    browser = p.chromium.launch(
                        headless=self.headless, args=BROWSER_LAUNCH_ARGS)
                    ctx = browser.new_context(user_agent=DEFAULT_UA, viewport=viewport,
                                              locale="zh-CN", timezone_id="Asia/Shanghai")
                    if objs:
                        try:
                            ctx.add_cookies(objs)
                        except Exception:  # noqa: BLE001
                            pass
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if platform in ("douyin", "kuaishou", "xiaohongshu"):
                    page.on("response",
                            lambda r: self._on_response(r, add_first, xhs_by_id, platform, ""))
                try:
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(3000)
                for _ in range(5):
                    if found:
                        break
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(1200)
                if browser:
                    browser.close()
                else:
                    ctx.close()
        except Exception as e:  # noqa: BLE001
            print(f"[browser] resolve_one {platform} 失败: {e}")
        return found[0] if found else None

    def list_user_videos(self, platform: str, profile_url: str, max_results: int,
                          clear_login: bool = False) -> "list[VideoLink]":
        """导航到作者主页并滚动，截取其作品列表（抖音 aweme/post 等 XHR 注入到 _on_response）。

        与 search_keyword 共用浏览器实例与响应拦截；调度方负责 close()。
        抖音作者页会触发 aweme/post XHR（返回 aweme_list），由 _on_response 统一抽取。
        需浏览器可用且平台已在 BROWSER_SEARCH 登记；否则返回空列表。

        clear_login=True（仅抖音）：打开浏览器、注入 Cookie 后、导航前主动剥离登录态 Cookie，
        使首屏即以「游客」身份访问目标主页。原因：已登录会话访问 /user/{sec_uid} 时，抖音会
        把主页劫持成「我」，导致截获到的是登录账号作品而非目标作者——这正是「抖音号没生效、
        返回的是 cookie 登录账号作品」的根因。保留 s_v_web_id/ttwid 等游客标识以降低验证码概率。
        """
        if not self._open_browser(platform):
            return []
        # 关键修复：导航前剥离登录态，避免已登录账号劫持他人主页（详见 docstring）。
        if clear_login and platform == "douyin":
            self._strip_login_cookies()
        out: list = []
        seen: set = set()
        self._out, self._seen, self._xhs_by_id = out, seen, {}
        self._platform, self._keyword = platform, ""
        # 达到上限立即停止收集，避免无意义的滚动/XHR 解析开销
        self._add = lambda v: (seen.add(v.url) or out.append(v)) \
            if (len(out) < max_results and v.url and v.url not in seen) else None
        try:
            try:
                self._page.goto(profile_url, timeout=45000, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass  # 超时也继续，后续滚动期间 XHR 仍会被 on_response 捕获
            self._page.wait_for_timeout(3000)
            idle = 0
            for _ in range(20):
                if len(out) >= max_results:
                    break
                prev = len(out)
                self._page.mouse.wheel(0, 3000)
                self._page.wait_for_timeout(900)
                # 自适应早停：连续 3 轮无新增说明页面已无更多内容，提前结束滚动
                if len(out) == prev:
                    idle += 1
                    if idle >= 3:
                        break
                else:
                    idle = 0
        except Exception as e:  # noqa: BLE001
            print(f"[browser] list_user_videos {platform} 失败: {e}")
        return out[:max_results]

    # 登录态 Cookie 标识（访问 /user/{sec_uid} 等他人主页时必须剥离，否则已登录账号会
    # 把主页「劫持」成「我」，导致返回登录账号作品）。游客标识 s_v_web_id/ttwid 等予以保留。
    _LOGIN_COOKIE_KEYS = frozenset({
        "sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt", "uid_tt_ss",
        "odin_tt", "passport_auth_status", "passport_auth_status_ss",
        "login_ticket", "ticket", "sso_auth_status", "sso_uid_tt", "sso_uid_tt_ss",
        "passport_csrf_token", "csrf_session_id", "LOGIN_STATUS", "login_alert",
    })

    def _strip_login_cookies(self) -> None:
        """从当前浏览器上下文移除登录态 Cookie，使会话退化为「游客」，从而避免已登录账号
        劫持 /user/{sec_uid} 主页。游客标识（s_v_web_id/ttwid 等）予以保留。"""
        if not self._ctx:
            return
        try:
            cs = self._ctx.cookies()
            keep = [c for c in cs if (c.get("name") or "") not in self._LOGIN_COOKIE_KEYS]
            self._ctx.clear_cookies()
            if keep:
                self._ctx.add_cookies(keep)
            print(f"[browser] 已移除 {len(cs) - len(keep)} 条登录态 Cookie，会话降为游客")
        except Exception as e:  # noqa: BLE001
            print(f"[browser] 移除登录态 Cookie 失败：{e}")

    # ---- Cookie -> Playwright cookie 对象 ----
    def _cookie_objs(self, platform: str) -> list[dict]:
        header = self.cookies_by_platform.get(platform, "")
        domain = "." + PLATFORM_DOMAIN.get(platform, platform)
        objs = []
        for seg in header.split(";"):
            seg = seg.strip()
            if "=" not in seg:
                continue
            k, v = seg.split("=", 1)
            objs.append({"name": k.strip(), "value": v.strip(),
                         "domain": domain, "path": "/"})
        return objs

    def _open_browser(self, platform: str) -> bool:
        """启动浏览器 + context + page 并注册响应拦截；已打开则直接复用。返回是否就绪。

        浏览器启动是搜索中最耗时的环节（headless chromium 冷启动通常 2-4 秒）。把浏览器
        生命周期从「每次 search 都开关」提升为「一个平台一次启动、多个关键词复用同一 page」，
        是多关键词/多平台搜索提速的核心。
        """
        if self._open:
            return True
        if not self.available:
            print("[browser] 未安装 playwright，跳过。安装: pip install playwright && playwright install chromium")
            return False
        import os
        from playwright.sync_api import sync_playwright  # noqa: WPS433
        cfg = BROWSER_SEARCH.get(platform)
        if not cfg:
            print(f"[browser] 平台 {platform} 未登记浏览器搜索配置，跳过。")
            return False
        try:
            self._platform = platform
            self._p = sync_playwright().start()
            # 启动采用与 video_link_collector-v13fixedmany 一致的普通 headless（不启用 --headless=new、
            # 不做 stealth 注入），规避抖音 secsdk 风控弹出的 verify_check 验证码墙（否则搜索 XHR 被拦、0 结果）。
            # 浏览器形态选择：
            #  - 抖音/快手/小红书：用【全新 context + 注入干净 JSON cookie】。
            #  - 微博：仍依赖【持久化 profile】里的完整登录态（比单纯 cookie 更全），保留复用。
            viewport = {"width": 1360, "height": 900}
            profile = os.path.join(self.profile_root, platform) if self.profile_root else ""
            use_profile = (platform == "weibo") and bool(profile and os.path.isdir(profile))
            objs = self._cookie_objs(platform)
            if use_profile:
                self._ctx = self._p.chromium.launch_persistent_context(
                    profile, headless=self.headless, user_agent=DEFAULT_UA,
                    args=BROWSER_LAUNCH_ARGS, viewport=viewport,
                    locale="zh-CN", timezone_id="Asia/Shanghai")
                self._browser = None
            else:
                self._browser = self._p.chromium.launch(headless=self.headless,
                                                        args=BROWSER_LAUNCH_ARGS)
                self._ctx = self._browser.new_context(
                    user_agent=DEFAULT_UA, viewport=viewport,
                    locale="zh-CN", timezone_id="Asia/Shanghai")
                if objs:
                    try:
                        self._ctx.add_cookies(objs)
                        print(f"[browser] 已向 {platform} 注入 {len(objs)} 条 Cookie")
                    except Exception as e:  # noqa: BLE001
                        print(f"[browser] Cookie 注入失败：{e}")
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            if platform in ("douyin", "kuaishou", "xiaohongshu"):
                self._page.on("response", self._on_response_wrapper)
            self._open = True
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[browser] {platform} 浏览器启动失败: {e}")
            self.close()
            return False

    def _on_response_wrapper(self, resp) -> None:
        """page 级响应拦截：委托给可复用的 _on_response，参数从实例字段读取（支持关键词切换）。"""
        if self._add is not None:
            self._on_response(resp, self._add, self._xhs_by_id, self._platform, self._keyword)

    def search_keyword(self, platform: str, keyword: str, max_results: int,
                        clear_login: bool = False) -> list[VideoLink]:
        """在已打开的浏览器会话中搜索单个关键词，复用 page（免去重复启动开销）。

        调用前请确保本实例已通过 _open_browser(platform) 打开；同一平台可连续调用多个关键词，
        最后调用 close() 释放资源。

        clear_login（仅抖音，默认 False，生产路径不启用）：导航前剥离登录态 Cookie（sessionid 等），
        退化为「游客」身份。注意：抖音关键词搜索生产路径【不使用】此降级——用户实测在 Cookie 有效时，
        剥离登录态反而会触发抖音验证码墙（返回 0 条）。生产路径直接复用完整登录态 Cookie。
        （此参数仅作为异常排查的逃生舱保留，日常不会传 True。）
        """
        if not self._open_browser(platform):
            return []
        # 每次搜索重置风控标记（与作者搜索等共用实例时避免串扰）。
        self._verify_check = False
        # 抖音仅在显式 clear_login=True 时剥离登录态（游客态逃生舱）。生产关键词路径不调用，
        # 直接使用完整登录态 Cookie——避免触发验证码墙。
        if clear_login and platform == "douyin":
            self._strip_login_cookies()
        out: list[VideoLink] = []
        seen: set[str] = set()
        xhs_by_id: dict[str, VideoLink] = {}
        self._out, self._seen, self._xhs_by_id = out, seen, xhs_by_id
        self._platform, self._keyword = platform, keyword

        def add(v: VideoLink) -> None:
            # 达到上限立即停止收集：避免 XHR 一次性灌入整页（~55 条）后又被截断，
            # 造成无意义的滚动/XHR 解析开销（抖音等浏览器平台尤其明显）。
            if len(out) >= max_results:
                return
            if v.url and v.url not in seen:
                seen.add(v.url)
                out.append(v)
        self._add = add

        cfg = BROWSER_SEARCH[platform]
        url = cfg["url"].format(kw=urllib.parse.quote(keyword))
        sel = cfg["sel"]
        # 兼容单一 (pat, host) 与多形态 variants（如微博同时有 /tv/show/ 与 video.weibo.com/show?fid=）
        variants = cfg.get("variants") or [{"pat": cfg.get("pat"), "host": cfg.get("host", "")}]
        variants = [v for v in variants if v.get("pat")]
        need_xsec = bool(cfg.get("xsec"))
        need_meta = bool(cfg.get("dom_meta"))

        def _run_once() -> int:
            # 重试时清空上一次残留，避免重复累加
            out.clear(); seen.clear(); xhs_by_id.clear()
            try:
                try:
                    self._page.goto(url, timeout=45000, wait_until="domcontentloaded")
                except Exception:  # noqa: BLE001
                    pass  # 超时也继续，后续滚动期间 XHR 仍会被 on_response 捕获
                self._page.wait_for_timeout(2500)
                idle = 0
                for _ in range(14):
                    if len(out) >= max_results:
                        break
                    prev = len(out)
                    self._page.mouse.wheel(0, 3000)
                    self._page.wait_for_timeout(800)   # 自适应：单次等待收紧（原 1000ms），配合下方无新增早停
                    try:
                        if need_xsec or need_meta:
                            # 小红书(xsec) 与 强反爬平台(meta)：从元素（及祖先）读取 href / xsec token
                            # / 卡片文本 / 封面 alt / aria-label，一次性把链接与元数据都抓出来。
                            nodes = self._page.eval_on_selector_all(
                                sel,
                                """els => els.map(e => {
                                  const card = e.closest('[class*="card"],[class*="item"],[class*="note"],[class*="feed"],section,article') || e;
                                  const img = card.querySelector('img');
                                  const h = e.getAttribute('href') || e.href || '';
                                  const tk = (e.closest('[data-xsec-token]') || e).getAttribute('data-xsec-token') || '';
                                  const sc = (e.closest('[data-xsec-source]') || e).getAttribute('data-xsec-source') || '';
                                  return {
                                    href: h, token: tk, source: sc,
                                    text: (card.innerText || card.textContent || '').replace(/\\s+/g,' ').trim(),
                                    alt: img ? (img.getAttribute('alt') || '') : '',
                                    aria: e.getAttribute('aria-label') || '',
                                    titleAttr: e.getAttribute('title') || '',
                                    img: img ? (img.getAttribute('src') || img.src || '') : ''
                                  };
                                })""")
                        else:
                            nodes = [{"href": h} for h in self._page.eval_on_selector_all(
                                sel, "els => els.map(e => e.href)")]
                    except Exception:  # noqa: BLE001
                        nodes = []
                    for item in nodes:
                        href = item.get("href", "")
                        href = urllib.parse.unquote(href)  # 深链里 mid%3D -> mid= 等
                        # 依次尝试每种链接形态，命中即用其对应的 host 拼出纯净链接
                        matched = False
                        for v in variants:
                            m = re.search(v["pat"], href)
                            if not m:
                                continue
                            path = m.group(1)
                            token = item.get("token", "")
                            source = item.get("source", "")
                            # 仅当 href 内尚无 token，且属性里确有 token 时才补齐，避免重复拼接。
                            # 注意：使用 data 属性里的原始值，不要再做 URL 编码——小红书自身拼接时也是原样，
                            # 编码 + / = 反而会让 token 失效。
                            if token and "xsec_token" not in path:
                                sep = "&" if "?" in path else "?"
                                src = source or "pc_search"
                                path = f"{path}{sep}xsec_source={src}&xsec_token={token}"
                            meta = _extract_dom_meta(item, platform) if need_meta else ("", "", 0, 0, 0, "")
                            add(VideoLink(platform, keyword, meta[0], v["host"] + path,
                                          author=meta[1], likes=meta[2], views=meta[3], duration=meta[4],
                                          cover=meta[5]))
                            matched = True
                            break
                        if matched and len(out) >= max_results:
                            break
                    # 自适应早停：连续 2 轮无新增说明页面已无更多内容，提前结束滚动（避免空等 14 轮）
                    if len(out) == prev:
                        idle += 1
                        if idle >= 2:
                            break
                    else:
                        idle = 0
                # 小红书：以 XHR 数据为主源（含新鲜 token + 完整元数据），合并进结果。
                # DOM 兜底仍运行（保证即使 XHR 被风控也能出链接），这里用 XHR 富数据
                # 覆盖同 note_id 的 DOM 条目，并补回 XHR 独有、DOM 没抓到的笔记。
                if platform == "xiaohongshu" and xhs_by_id:
                    merged: list[VideoLink] = []
                    used: set[str] = set()
                    for v in out:
                        m = re.search(r"explore/([0-9a-f]{16,32})", v.url)
                        nid = m.group(1) if m else ""
                        if nid and nid in xhs_by_id:
                            used.add(nid)
                            merged.append(xhs_by_id[nid])
                        else:
                            merged.append(v)
                    for nid, v in xhs_by_id.items():
                        if nid not in used:
                            merged.append(v)
                    out[:] = merged
                # 小红书：搜索接口对部分笔记不返回完整 note_card（标题/时长/点赞/封面缺失），
                # 用登录态浏览器批量请求 feed 接口补全（权威数据源），彻底解决「信息不全」。
                if platform == "xiaohongshu":
                    enrich_xhs_via_page(self._page, out)
            except Exception as e:  # noqa: BLE001
                print(f"[browser] {platform} 搜索失败: {e}")
            return len(out)

        # 抖音/微博等 secsdk 风控会间歇性弹验证码导致 0 结果：重新导航重试几次提升通过率
        # （每次全新导航都可能通过风控，故 0 结果时立即重试，命中即止）。
        best = 0
        for attempt in range(3):
            n = _run_once()
            best = max(best, n)
            if n > 0:
                break
            self._page.wait_for_timeout(1500)
        if best == 0:
            # 0 结果诊断：抓取页面标题/URL/是否登录墙/是否触发搜索 XHR/原始响应，便于定位「无结果」根因。
            try:
                _title = self._page.title() or ""
                _url = self._page.url or ""
                _body = ""
                _html = ""
                try:
                    _body = (self._page.evaluate("() => document.body ? document.body.innerText.slice(0,300) : ''") or "")
                    _html = (self._page.evaluate(
                        "() => { const v=document.querySelector('a[href*=\"/video/\"]');"
                        " const m=document.body?document.body.innerHTML.slice(0,1200):'';"
                        " return JSON.stringify({hasVideoLink: !!v, htmlSnippet: m}); }") or "{}")
                except Exception:  # noqa: BLE001
                    _body = ""
                _login = any(k in (_title + _body) for k in ("登录", "验证", "slider", "验证", "请登录"))
                _saw = getattr(self, "_saw_aweme_xhr", False)
                _vc = getattr(self, "_verify_check", False)
                _raw = getattr(self, "_last_aweme_raw", None)
                # 精确判定验证码墙：DOM 是否出现 #captcha_container 或 captcha 元素。
                _cap = False
                try:
                    _cap = bool(self._page.query_selector("#captcha_container")
                                or self._page.query_selector('[class*="captcha"]'))
                except Exception:  # noqa: BLE001
                    _cap = False
                _diag = (f"[browser] {platform} 关键词「{keyword}」返回 0 条。"
                         f" title={_title!r} url={_url!r}"
                         f" 触发awemeXHR={_saw}")
                if _vc or _cap:
                    # 风控验证码墙：Cookie 本身有效（下载/作者搜索正常），与登录态无关，
                    # 是抖音对自动化浏览器会话的搜索接口拦截，需真实浏览器手动过一次验证码。
                    _diag += (" ⚠抖音对「关键词搜索」启用了风控验证码（verify_check）："
                              "自动化浏览器会话被判定为风险，搜索接口返回空结果。"
                              "Cookie 有效（yt-dlp 下载、作者搜索均正常），此拦截与登录态无关。"
                              "建议：① 真实浏览器手动通过一次验证码后重新导出 Cookie；"
                              "② 改用「作者搜索」或粘贴具体视频链接解析。")
                elif _login:
                    _diag += " ⚠疑似登录墙/验证码，请导入抖音登录态 Cookie 后重试"
                _diag += (f" | 页面DOM: {_html}" if _html else "")
                if _raw:
                    _diag += f" | 原始响应[{_raw[0]}]: {_raw[1]}"
                print(_diag)
            except Exception:  # noqa: BLE001
                pass
        return out[:max_results]

    def search(self, platform: str, keyword: str, max_results: int) -> list[VideoLink]:
        """单次搜索便捷封装（兼容旧调用）：打开浏览器 → 搜一个词 → 关闭。

        多关键词复用请自行持有 BrowserSearcher 实例：``bs = BrowserSearcher(...);
        bs._open_browser(p); [bs.search_keyword(p,k,b) for k in kws]; bs.close()``。
        """
        try:
            self._open_browser(platform)
            return self.search_keyword(platform, keyword, max_results)
        finally:
            self.close()

    def close(self) -> None:
        """关闭并释放浏览器资源（幂等，可重复调用）。"""
        try:
            if self._browser:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._p:
                self._p.stop()
        except Exception:  # noqa: BLE001
            pass
        self._open = False
        self._p = self._ctx = self._browser = self._page = None
        self._add = self._out = self._seen = self._xhs_by_id = None
        self._platform = self._keyword = None


def build_markdown(items: list[VideoLink], keywords: list[str], platforms: list[str]) -> str:
    """把采集结果渲染为 Markdown 文档（含元数据表格）。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append("# 视频链接采集报告")
    lines.append("")
    lines.append(f"- 生成时间：{now}")
    lines.append(f"- 关键词：{', '.join(keywords) if keywords else '(分享链接导入)'}")
    lines.append(f"- 平台：{', '.join(platforms)}")
    lines.append(f"- 总数：{len(items)}")
    by_plat = {}
    for it in items:
        by_plat[it.platform] = by_plat.get(it.platform, 0) + 1
    if by_plat:
        lines.append("- 平台分布：" + "，".join(f"{k} {v}" for k, v in by_plat.items()))
    lines.append("")
    lines.append("## 视频列表")
    lines.append("")
    lines.append("| # | 平台 | 标题 | UP主/作者 | 时长 | 点赞 | 播放 | 链接 |")
    lines.append("|---|------|------|-----------|------|------|------|------|")
    for i, it in enumerate(items, 1):
        title = it.title.replace("|", "/").replace("\n", " ")
        author = it.author or "—"
        dur = fmt_duration(it.duration)
        likes = fmt_num(it.likes) if it.likes else "—"
        views = fmt_num(it.views) if it.views else "—"
        lines.append(
            f"| {i} | {it.platform} | {title} | {author} | {dur} | {likes} | {views} | {it.url} |"
        )
    lines.append("")
    lines.append("## 批量下载")
    lines.append("")
    lines.append("```bash")
    lines.append("# 纯链接清单（links.txt）配合 yt-dlp：")
    lines.append("yt-dlp -a links.txt")
    lines.append("# 需要登录态的内容，用本工具导出的 cookies.txt：")
    lines.append("yt-dlp --cookies cookies/douyin_cookies.txt -a links.txt")
    lines.append("```")
    lines.append("")
    lines.append("> 由 collect_links 工具自动生成。请仅采集你有权使用的公开内容，遵守平台条款与著作权法。")
    return "\n".join(lines)
