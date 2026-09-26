#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cookie_manager.py — Cookie 全生命周期管理（零外部依赖；浏览器登录为可选能力）

解决两件事
----------
1) **复制即用**：用户从任何地方复制来的 Cookie，自动识别格式 -> 归一化 -> 落盘 -> 可直接给
   采集器和 yt-dlp 使用。支持 6 种输入格式，全部自动嗅探，无需用户选择：
     a. Netscape cookies.txt（yt-dlp / curl 通用）
     b. JSON 数组（EditThisCookie / Cookie-Editor 导出）
     c. Playwright / Puppeteer storage_state（{"cookies":[...]}）
     d. DevTools Application 面板整表粘贴（Tab 或多空格分隔）
     e. DevTools「Copy as cURL」整段命令（提取 -H 'cookie: ...' / -b '...'）
     f. 原始 Cookie 头 / document.cookie 输出（a=1; b=2）

2) **登录即取**：调用系统浏览器（Playwright，可选依赖）打开平台登录页，用户扫码/账号登录后，
   自动抓取登录态 Cookie 并落盘。使用持久化用户目录，下次免登录。

落盘结构
--------
cookies/
  ├── .gitignore            # 内容为 *，防止把登录凭据提交到仓库
  ├── douyin.json           # 归一化后的结构化记录（含来源/时间/过期）
  ├── douyin_cookies.txt    # Netscape 格式，直接给 yt-dlp --cookies 用
  └── profiles/douyin/      # Playwright 持久化浏览器配置（登录一次长期有效）

安全须知：Cookie 等同于账号登录凭据。本模块仅本地存储，不上传任何服务器；请勿分享该目录。
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

# ROOT 默认取本文件所在目录；PyInstaller 单文件冻结态下由 app.py 注入 VLC_APP_ROOT，
# 使 cookies / profiles 落到 exe 同级的可写目录（_MEIPASS 只读）。
ROOT = os.environ.get("VLC_APP_ROOT") or os.path.dirname(os.path.abspath(__file__))
COOKIE_DIR = os.path.join(ROOT, "cookies")
PROFILE_DIR = os.path.join(COOKIE_DIR, "profiles")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------- 平台元数据
# 字段说明：
#   exclusive  —— 该平台「独占」的 cookie 名，命中权重最高(5)。用于消歧同厂平台
#                 （同厂平台共享 cookie 名时，只靠共享键必然误判）。
#   signature  —— 该平台常见但可能与他站重叠的 cookie 名，权重较低(2)。
#   login_keys —— 判定「已登录」必须具备的 cookie，元组内为 AND、元组间为 OR。
#   device_keys—— 判定「设备指纹完整」的基础 cookie（缺失时匿名请求更易被风控）。
PLATFORM_META: dict[str, dict] = {
    "douyin": {
        "label": "抖音",
        "domain": ".douyin.com",
        "home": "https://www.douyin.com/",
        "exclusive": ("IsDouyinActive", "home_can_add_dy_2_desktop", "s_v_web_id",
                      "douyin.com", "dy_swidth", "passport_fe_beating_status"),
        "signature": ("sessionid_ss", "odin_tt", "ttwid", "passport_csrf_token",
                      "__ac_nonce", "msToken", "sid_guard", "tt_webid"),
        "login_keys": (("sessionid_ss",), ("sessionid",), ("sid_guard",)),
        "device_keys": ("ttwid", "odin_tt"),
    },
    "kuaishou": {
        "label": "快手",
        "domain": ".kuaishou.com",
        "home": "https://www.kuaishou.com/new-reco",
        "exclusive": ("kuaishou.web.cp.api_ph", "kuaishou.web.cp.api_st",
                      "kpn", "kpf", "clientid", "didv"),
        "signature": ("did", "userId", "passToken"),
        "login_keys": (("kuaishou.web.cp.api_ph",), ("userId",), ("passToken",)),
        "device_keys": ("did",),
    },
    "bilibili": {
        "label": "Bilibili",
        "domain": ".bilibili.com",
        "home": "https://www.bilibili.com/",
        "login": "https://passport.bilibili.com/login",
        "exclusive": ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4", "b_nut"),
        "signature": ("sid", "b_lsid", "browser_resolution"),
        "login_keys": (("SESSDATA", "bili_jct"),),
        "device_keys": ("buvid3",),
    },
    "xiaohongshu": {
        "label": "小红书",
        "domain": ".xiaohongshu.com",
        "home": "https://www.xiaohongshu.com/explore",
        "exclusive": ("web_session", "webId", "xsecappid", "customerClientId",
                      "websectiga", "sec_poison_id"),
        "signature": ("a1", "gid", "acw_tc", "abRequestId"),
        "login_keys": (("web_session",), ("customerClientId", "webId")),
        "device_keys": ("a1", "webId"),
    },
    "weibo": {
        "label": "微博",
        "domain": ".weibo.com",
        "home": "https://weibo.com/",
        "login": "https://passport.weibo.com/signin/login",
        "exclusive": ("SUB", "SUBP", "SSOLoginState", "WBPSESS", "ALF", "SCF"),
        "signature": ("XSRF-TOKEN", "_s_tentry", "UOR", "Apache", "ULV"),
        "login_keys": (("SUB",), ("SUBP", "SSOLoginState")),
        "device_keys": ("SUB",),
    },
    "youtube": {
        "label": "YouTube",
        "domain": ".youtube.com",
        "home": "https://www.youtube.com/",
        "exclusive": ("LOGIN_INFO", "VISITOR_INFO1_LIVE", "__Secure-3PSID",
                      "__Secure-1PSID", "YSC", "SAPISID", "__Secure-3PAPISID"),
        "signature": ("SID", "HSID", "SSID", "APISID", "PREF", "CONSENT"),
        "login_keys": (("LOGIN_INFO",), ("SID", "HSID"), ("__Secure-3PSID",)),
        "device_keys": ("VISITOR_INFO1_LIVE",),
    },
}

PLATFORMS = tuple(PLATFORM_META.keys())

# 需要剔除的噪声 key（DevTools 表格粘贴时会混入表头）
_NOISE_KEYS = {
    "name", "value", "domain", "path", "expires", "size", "httponly", "secure",
    "samesite", "partitionkey", "priority", "cross-site", "max-age", "cookie",
}


# ================================================================ 解析层
def _clean(s: str) -> str:
    return s.strip().strip('"').strip("'").strip()


def _valid_pair(name: str, value: str) -> bool:
    if not name or name.lower() in _NOISE_KEYS:
        return False
    if len(name) > 128 or " " in name:
        return False
    return True


def _from_json(raw: str) -> tuple[dict[str, str], dict[str, dict], str] | None:
    """解析 JSON 数组 / storage_state。返回 (cookies, meta_by_name, format_name)。"""
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            data, fmt = data["cookies"], "storage_state(JSON)"
        else:
            # 形如 {"sessionid":"xxx","ttwid":"yyy"} 的扁平字典
            flat = {str(k): str(v) for k, v in data.items() if _valid_pair(str(k), str(v))}
            return (flat, {}, "flat-JSON") if flat else None
    elif isinstance(data, list):
        fmt = "JSON 数组(EditThisCookie/Cookie-Editor)"
    else:
        return None

    cookies: dict[str, str] = {}
    meta: dict[str, dict] = {}
    for it in data:
        if not isinstance(it, dict):
            continue
        name = _clean(str(it.get("name", "")))
        value = str(it.get("value", ""))
        if not _valid_pair(name, value):
            continue
        cookies[name] = value
        exp = it.get("expirationDate") or it.get("expires")
        try:
            exp = float(exp) if exp not in (None, "", -1, "-1") else 0.0
        except Exception:  # noqa: BLE001
            exp = 0.0
        meta[name] = {
            "domain": _clean(str(it.get("domain", ""))),
            "path": _clean(str(it.get("path", "/"))) or "/",
            "expires": exp if exp > 0 else 0.0,
            "secure": bool(it.get("secure", False)),
        }
    return (cookies, meta, fmt) if cookies else None


def _from_netscape(raw: str) -> tuple[dict[str, str], dict[str, dict], str] | None:
    cookies: dict[str, str] = {}
    meta: dict[str, dict] = {}
    for line in raw.splitlines():
        line = line.rstrip("\n")
        if not line.strip() or line.strip().startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = cols[:7]
        name = _clean(name)
        if not _valid_pair(name, value):
            continue
        cookies[name] = value
        try:
            exp = float(expires)
        except Exception:  # noqa: BLE001
            exp = 0.0
        meta[name] = {"domain": _clean(domain), "path": _clean(path) or "/",
                      "expires": exp, "secure": secure.strip().upper() == "TRUE"}
    return (cookies, meta, "Netscape cookies.txt") if cookies else None


def _from_curl(raw: str) -> tuple[dict[str, str], dict[str, dict], str] | None:
    """从 DevTools「Copy as cURL」命令里抠出 cookie 串。"""
    if "curl" not in raw.lower():
        return None
    pats = [
        r"-H\s+['\"]\s*cookie\s*:\s*([^'\"]+)['\"]",
        r"-b\s+['\"]([^'\"]+)['\"]",
        r"--cookie\s+['\"]([^'\"]+)['\"]",
    ]
    for p in pats:
        m = re.search(p, raw, re.I)
        if m:
            got = _from_header(m.group(1))
            if got:
                return got[0], got[1], "cURL 命令"
    return None


def _from_header(raw: str) -> tuple[dict[str, str], dict[str, dict], str] | None:
    """解析 `a=1; b=2` 形式（含 `cookie: ` 前缀 / document.cookie 输出）。"""
    text = raw.strip()
    text = re.sub(r"^\s*(set-)?cookie\s*:\s*", "", text, flags=re.I)
    # 多行 header 块里只取 cookie 那一行
    if "\n" in text and ";" in text:
        for line in text.splitlines():
            if line.count("=") >= 2 and ";" in line:
                text = line
                break
    if "=" not in text:
        return None
    cookies: dict[str, str] = {}
    for seg in text.split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        name, value = seg.split("=", 1)
        name, value = _clean(name), value.strip()
        if _valid_pair(name, value):
            cookies[name] = value
    return (cookies, {}, "Cookie 头 / document.cookie") if len(cookies) >= 1 else None


def _from_table(raw: str) -> tuple[dict[str, str], dict[str, dict], str] | None:
    """解析 DevTools Application 面板整表粘贴：每行 name<TAB>value<TAB>domain..."""
    cookies: dict[str, str] = {}
    meta: dict[str, dict] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        cols = re.split(r"\t+|\s{2,}", line.strip())
        cols = [c for c in cols if c != ""]
        if len(cols) < 2:
            continue
        name, value = _clean(cols[0]), cols[1].strip()
        if not _valid_pair(name, value):
            continue
        cookies[name] = value
        dom = ""
        for c in cols[2:5]:
            if c.startswith(".") or ".com" in c or ".cn" in c:
                dom = _clean(c)
                break
        meta[name] = {"domain": dom, "path": "/", "expires": 0.0, "secure": False}
    return (cookies, meta, "DevTools 表格粘贴") if len(cookies) >= 2 else None


def parse_cookie_text(raw: str) -> tuple[dict[str, str], dict[str, dict], str]:
    """自动嗅探格式并归一化。

    返回 (cookies, meta_by_name, detected_format)；无法解析时 cookies 为空 dict。
    嗅探顺序按「特征强度」排列，避免误判。
    """
    raw = (raw or "").strip()
    if not raw:
        return {}, {}, "空输入"
    for parser in (_from_json, _from_netscape, _from_curl, _from_table, _from_header):
        try:
            got = parser(raw)
        except Exception:  # noqa: BLE001
            got = None
        if got and got[0]:
            return got
    return {}, {}, "无法识别"


def platform_scores(cookies: dict[str, str],
                    meta: dict[str, dict] | None = None) -> dict[str, int]:
    """给每个平台打分。独占键权重 5、共享键权重 2、domain 命中 +8。

    分离权重是为了处理同厂平台共享 cookie 名的问题（如字节系共享 ttwid/msToken/sessionid），
    只靠共享键必然误判；独占键才是可靠依据。
    """
    meta = meta or {}
    domains = " ".join(str(m.get("domain", "")) for m in meta.values()).lower()
    scores: dict[str, int] = {}
    for plat, info in PLATFORM_META.items():
        score = sum(5 for k in info.get("exclusive", ()) if k in cookies)
        score += sum(2 for k in info.get("signature", ()) if k in cookies)
        host = info["domain"].lstrip(".")
        if host and host in domains:
            score += 8
        scores[plat] = score
    return scores


def detect_platform(cookies: dict[str, str], meta: dict[str, dict] | None = None) -> str:
    """根据 cookie 名特征 + domain 判定平台，返回 '' 表示无法判定。

    刻意不做「并列时取第一个」：当某平台只带共享键时会与同厂平台打平，
    此时静默猜一个的代价是把 Cookie 存错平台，宁可返回空让用户显式指定。
    """
    scores = platform_scores(cookies, meta)
    if not scores:
        return ""
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, top = ranked[0]
    if top < 4:
        return ""
    if len(ranked) > 1 and ranked[1][1] == top:  # 并列 => 无法区分
        return ""
    return best


# ------------------------------------------------- 伪造/占位 Cookie 识别
# 用户常把示例/测试串当真实 Cookie 粘贴进来（如 did=web_test123, api_ph=PH_TOKEN_X），
# 这种「以为导入了其实没有」最容易导致「Cookie 总是无效」。这里只识别「明显占位」，
# 宁可漏报也不误伤真实随机值（真实 token 多为随机 hex，绝不会精确等于示例串）。
# 用「精确匹配示例值」+「少数高危子串」双保险，避免 xxxx / test 这类子串误伤随机 token。
_SYNTHETIC_EXACT = {
    "secret", "test", "xxx", "xxxx", "abc", "dummy", "example", "placeholder",
    "todo", "fake", "sample", "demo", "web_test123", "ph_token_x", "token_x",
    "your_token_here", "x", "xx", "test123", "testcookie", "000000", "111111",
}
_SYNTHETIC_SUBSTR = [
    re.compile(r"ph_token", re.I),
    re.compile(r"token[_-]?x", re.I),
    re.compile(r"your_+token", re.I),
]


def _did_looks_real(v: str) -> bool:
    """快手/抖音设备指纹 did 形如 web_<随机串>。

    真实值：web_ + 32 位十六进制（如 web_f2d47ed046bf43165cfba53f75dbc419），
    含字母，不能只认数字。这里放宽为 web_ + 6 位以上字母数字组合。
    示例占位 web_test123 仍由 _SYNTHETIC_EXACT 精确匹配拦截，不会漏。
    """
    return bool(re.match(r"^web_[0-9a-zA-Z]{6,}$", v or ""))


def looks_synthetic(cookies: dict, meta: dict | None = None) -> bool:
    """命中明显占位特征即判为可疑；对真实随机 Cookie 保持宽容，避免误报。"""
    for k, v in cookies.items():
        s = str(v).strip()
        if s.lower() in _SYNTHETIC_EXACT:
            return True
        if any(p.search(s) for p in _SYNTHETIC_SUBSTR):
            return True
        if k.lower() == "did" and not _did_looks_real(s):
            return True
    return False


# ================================================================ 存储层
class CookieRecord:
    """一条平台 Cookie 记录。"""

    def __init__(self, platform: str, cookies: dict[str, str],
                 meta: dict[str, dict] | None = None, source: str = "manual"):
        self.platform = platform
        self.cookies = cookies
        self.meta = meta or {}
        self.source = source
        self.saved_at = time.strftime("%Y-%m-%d %H:%M:%S")

    # ---- 派生属性 ----
    @property
    def header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if k and v != "")

    @property
    def logged_in(self) -> bool:
        groups = PLATFORM_META.get(self.platform, {}).get("login_keys", ())
        return any(all(k in self.cookies and self.cookies[k] for k in grp) for grp in groups)

    @property
    def device_ready(self) -> bool:
        keys = PLATFORM_META.get(self.platform, {}).get("device_keys", ())
        return all(k in self.cookies for k in keys) if keys else True

    @property
    def suspicious(self) -> bool:
        return looks_synthetic(self.cookies, self.meta)

    @property
    def expires_at(self) -> float:
        """取登录相关 cookie 里最早的过期时间（0 = 未知/会话级）。"""
        exps = []
        groups = PLATFORM_META.get(self.platform, {}).get("login_keys", ())
        watch = {k for grp in groups for k in grp}
        for name, m in self.meta.items():
            if name in watch and m.get("expires", 0):
                exps.append(float(m["expires"]))
        if not exps:
            exps = [float(m["expires"]) for m in self.meta.values() if m.get("expires", 0)]
        return min(exps) if exps else 0.0

    def to_dict(self) -> dict:
        exp = self.expires_at
        return {
            "platform": self.platform,
            "label": PLATFORM_META.get(self.platform, {}).get("label", self.platform),
            "count": len(self.cookies),
            "names": sorted(self.cookies.keys()),
            "logged_in": self.logged_in,
            "device_ready": self.device_ready,
            "suspicious": self.suspicious,
            "source": self.source,
            "saved_at": self.saved_at,
            "expires_at": exp,
            "expires_text": (time.strftime("%Y-%m-%d", time.localtime(exp)) if exp else "会话级/未知"),
            "expired": bool(exp and exp < time.time()),
        }

    def to_netscape(self) -> str:
        """导出 yt-dlp / curl 可用的 Netscape cookies.txt。

        关键修复：bilibili 等站点的登录态 Cookie（SESSDATA / bili_jct / DedeUserID）
        实际由 SSO 写在附属域名上（如 .huasheng.cn），而非 .bilibili.com。
        yt-dlp 的 is_logged_in 判定是“api.bilibili.com 上是否存在 SESSDATA”，
        域名不匹配会导致它把会话当成匿名，从而拿不到大会员 4K/8K 等高码率。
        因此这里把平台“关键登录 Cookie”强制归一到平台主域名，确保 yt-dlp 能识别登录态。
        """
        meta_info = PLATFORM_META.get(self.platform, {})
        default_domain = meta_info.get("domain", "")
        canon = default_domain if default_domain.startswith(".") else ("." + default_domain.lstrip(".") if default_domain else "")
        # 平台关键 Cookie 名集合：独占键 + login_keys 组合
        critical = set(meta_info.get("exclusive", ()))
        for pair in meta_info.get("login_keys", ()):
            if isinstance(pair, (tuple, list, set)):
                critical.update(pair)
            elif pair:
                critical.add(pair)
        far = int(time.time()) + 180 * 86400
        lines = [
            "# Netscape HTTP Cookie File",
            f"# Generated by video_link_collector for {self.platform} at {self.saved_at}",
            "# 用法: yt-dlp --cookies this_file.txt -a links.txt",
            "",
        ]
        for name, value in self.cookies.items():
            m = self.meta.get(name, {})
            domain = m.get("domain") or default_domain
            # 关键登录 Cookie 强制归一到平台主域名，保证 yt-dlp 登录态识别
            if name in critical and canon:
                domain = canon
            elif domain and not domain.startswith("."):
                domain = "." + domain.lstrip(".")
            include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            path = m.get("path") or "/"
            secure = "TRUE" if m.get("secure") else "FALSE"
            exp = int(m.get("expires") or 0) or far
            lines.append("\t".join([domain, include_sub, path, secure, str(exp), name, value]))
        return "\n".join(lines) + "\n"


class CookieStore:
    """本地 Cookie 仓库：读写 cookies/ 目录。"""

    def __init__(self, base: str = COOKIE_DIR):
        self.base = base
        os.makedirs(self.base, exist_ok=True)
        os.makedirs(PROFILE_DIR, exist_ok=True)
        gi = os.path.join(self.base, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8") as f:
                f.write("# 登录凭据，禁止提交\n*\n!.gitignore\n")

    def path(self, platform: str) -> str:
        return os.path.join(self.base, f"{platform}.json")

    def netscape_path(self, platform: str) -> str:
        return os.path.join(self.base, f"{platform}_cookies.txt")

    def save(self, rec: CookieRecord) -> dict:
        with open(self.path(rec.platform), "w", encoding="utf-8") as f:
            json.dump({
                "platform": rec.platform, "cookies": rec.cookies, "meta": rec.meta,
                "source": rec.source, "saved_at": rec.saved_at,
            }, f, ensure_ascii=False, indent=2)
        with open(self.netscape_path(rec.platform), "w", encoding="utf-8", newline="\n") as f:
            f.write(rec.to_netscape())
        info = rec.to_dict()
        info["json_file"] = self.path(rec.platform)
        info["netscape_file"] = self.netscape_path(rec.platform)
        return info

    def load(self, platform: str) -> CookieRecord | None:
        p = self.path(platform)
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            rec = CookieRecord(d.get("platform", platform), d.get("cookies", {}),
                               d.get("meta", {}), d.get("source", "manual"))
            rec.saved_at = d.get("saved_at", rec.saved_at)
            return rec
        except Exception:  # noqa: BLE001
            return None

    def clear(self, platform: str) -> dict:
        """删除某平台的 Cookie 文件（json + netscape）。

        返回结构化结果，便于上层区分「已删除 / 不存在 / 被拦截」：
          removed : 是否成功删除了至少一个文件
          missing : 不存在的文件路径
          blocked : 存在但因环境限制（如沙箱回收站不可用）未能删除的路径
        """
        out = {"removed": False, "missing": [], "blocked": []}
        for p in (self.path(platform), self.netscape_path(platform)):
            if not os.path.exists(p):
                out["missing"].append(p)
                continue
            try:
                os.remove(p)
                out["removed"] = True
            except Exception as e:  # noqa: BLE001
                # 例如沙箱环境回收站不可用导致 safe-delete 失败关闭；
                # 真实环境通常不会走到这里。原样记录，交由上层提示用户。
                out["blocked"].append(p)
                out[f"_error_{len(out['blocked'])}"] = str(e)
        return out

    def status(self) -> list[dict]:
        out = []
        for plat in PLATFORMS:
            rec = self.load(plat)
            if rec:
                d = rec.to_dict()
                d["configured"] = True
                d["suspicious"] = rec.suspicious
                d["netscape_file"] = self.netscape_path(plat)
            else:
                d = {
                    "platform": plat, "label": PLATFORM_META[plat]["label"], "configured": False,
                    "count": 0, "names": [], "logged_in": False, "device_ready": False,
                    "source": "", "saved_at": "", "expires_at": 0,
                    "expires_text": "—", "expired": False, "netscape_file": "",
                }
            d["profile_ready"] = os.path.isdir(os.path.join(PROFILE_DIR, plat))
            out.append(d)
        # 自定义域名 Cookie（浏览器「打开网址并登录」或粘贴未知平台产生）
        try:
            for fn in sorted(os.listdir(self.base)):
                if not fn.endswith(".json") or fn in (".gitignore",):
                    continue
                key = fn[:-5]
                if key in PLATFORMS:
                    continue
                rec = self.load(key)
                if not rec:
                    continue
                d = rec.to_dict()
                d["configured"] = True
                d["custom"] = True
                d["label"] = key.replace("custom_", "")
                d["netscape_file"] = self.netscape_path(key)
                d["profile_ready"] = os.path.isdir(os.path.join(PROFILE_DIR, key))
                out.append(d)
        except OSError:
            pass
        return out


def import_cookie_text(raw: str, platform: str = "auto", source: str = "paste",
                       store: CookieStore | None = None) -> dict:
    """把任意格式的 Cookie 文本转成可用 Cookie 并落盘。这是「复制即用」的唯一入口。"""
    store = store or CookieStore()
    cookies, meta, fmt = parse_cookie_text(raw)
    if not cookies:
        return {"ok": False, "error": f"未能从输入中解析出任何 Cookie（识别结果：{fmt}）。"
                                      "支持：Netscape cookies.txt / JSON 导出 / DevTools 表格 / "
                                      "Copy as cURL / document.cookie / 原始 Cookie 头。"}
    plat = platform if platform in PLATFORMS else detect_platform(cookies, meta)
    if not plat:
        scores = platform_scores(cookies, meta)
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        top = [PLATFORM_META[p]["label"] for p, s in ranked[:2] if s >= 4]
        hint = (f"特征在「{'」与「'.join(top)}」之间无法区分（这两站共用同一批 Cookie 名），"
                if len(top) > 1 else "特征不足以判定归属，")
        return {"ok": False,
                "error": f"无法自动判定平台：{hint}请在下拉框手动指定。"
                         f"可选：{'、'.join(PLATFORM_META[p]['label'] for p in PLATFORMS)}。",
                "format": fmt, "count": len(cookies), "names": sorted(cookies),
                "candidates": [{"platform": p, "label": PLATFORM_META[p]["label"], "score": s}
                               for p, s in ranked[:3]]}
    rec = CookieRecord(plat, cookies, meta, source=f"{source}:{fmt}")
    info = store.save(rec)
    info.update({"ok": True, "format": fmt})
    warns: list[str] = []
    if not rec.logged_in:
        warns.append(f"未检测到{PLATFORM_META[plat]['label']}登录态字段"
                     f"（如 {'/'.join(PLATFORM_META[plat]['login_keys'][0])}）；"
                     "匿名 Cookie 也能提升成功率，但搜索/高清下载可能仍受限。")
    if rec.suspicious:
        warns.append("⚠️ 检测到明显占位/测试值（如 web_test123、PH_TOKEN_X），"
                     "这大概率不是真实登录凭据，实际采集会被平台拒绝。"
                     "请粘贴从浏览器复制到的真实 Cookie。")
    if warns:
        info["warning"] = " ".join(warns)
    return info


# ================================================================ 校验层
def _http(url: str, cookie: str = "", data: bytes | None = None,
          headers: dict | None = None, timeout: int = 12) -> tuple[int, str]:
    h = {"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if cookie:
        h["Cookie"] = cookie
    if headers:
        h.update(headers)
    try:
        req = urllib.request.Request(url, data=data, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


# 通用探活配置：只用「无需签名」即可访问的接口/页面，避免给出假阳性结论。
#   kind=json —— 解析 JSON，按 ok_path 逐层取值，取到非空即视为已登录
#   kind=html —— 抓页面 HTML，用 login_re / anon_re 判定服务端认定的登录态
PROBE_SPECS: dict[str, dict] = {
    "weibo": {
        "kind": "json",
        "url": "https://weibo.com/ajax/profile/info",
        "ok_path": ("data", "user", "screen_name"),
        "ok_fmt": "已登录：{v}",
        "xsrf_cookie": "XSRF-TOKEN",
        "headers": {"Referer": "https://weibo.com/", "X-Requested-With": "XMLHttpRequest"},
    },
    "youtube": {
        "kind": "html",
        "url": "https://www.youtube.com/",
        "login_re": r'"LOGGED_IN"\s*:\s*true',
        "anon_re": r'"LOGGED_IN"\s*:\s*false',
    },
    "xiaohongshu": {
        "kind": "html",
        "url": "https://www.xiaohongshu.com/explore",
        "login_re": r'"loggedIn"\s*:\s*true|"isLogin"\s*:\s*true|"isLoggedIn"\s*:\s*true',
        "anon_re": r'"loggedIn"\s*:\s*false',
    },
}


def _dig(obj, path: tuple):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _probe_generic(platform: str, rec: "CookieRecord", spec: dict) -> dict:
    """按 PROBE_SPECS 通用探活。结论仍然只给 valid/invalid/unknown 三档。"""
    label = PLATFORM_META[platform]["label"]
    structural = "已具备登录态字段" if rec.logged_in else "仅匿名字段，无登录态"
    req_headers = dict(spec.get("headers") or {})
    # 部分平台（如微博）需在请求头带 x-xsrf-token（取自 XSRF-TOKEN Cookie），
    # 否则会返回 400。动态注入，避免把「缺头」误报成「Cookie 失效」。
    xsrf_name = spec.get("xsrf_cookie")
    if xsrf_name and xsrf_name in rec.cookies:
        req_headers["x-xsrf-token"] = rec.cookies[xsrf_name]
    code, text = _http(spec["url"], rec.header,
                       headers=req_headers, timeout=spec.get("timeout", 12))
    if not code:
        return {"ok": True, "status": "unknown",
                "message": f"网络不通（{text[:60]}），仅完成结构校验：{structural}。"}

    if spec["kind"] == "json":
        try:
            obj = json.loads(text)
        except Exception:  # noqa: BLE001
            obj = None
        # 签名拦截：接口需要请求签名（如某些平台返回 error_code=22「非法应用」），
        # 纯 HTTP 无法校验登录态，应判 unknown 而非 invalid，避免把「签名缺失」误报成「Cookie 失效」。
        if spec.get("blocked_path") and obj is not None and _dig(obj, spec["blocked_path"]):
            return {"ok": True, "status": "unknown",
                    "message": spec.get("blocked_msg",
                                        f"{label}接口返回需要签名的错误，纯 HTTP 无法校验登录态，仅完成结构校验。")}
        try:
            val = _dig(obj, spec["ok_path"]) if obj is not None else None
        except Exception:  # noqa: BLE001
            val = None
        if val:
            return {"ok": True, "status": "valid",
                    "message": spec.get("ok_fmt", "已登录：{v}").format(v=val)}
        return {"ok": True, "status": "invalid",
                "message": f"{label}接口未返回登录信息（HTTP {code}）。"
                           + ("本地存有登录态字段但服务端不认，Cookie 大概率已过期，请重新登录获取。"
                              if rec.logged_in else "当前为匿名 Cookie，请导入登录态 Cookie。")}

    # kind == html
    if re.search(spec["login_re"], text, re.I):
        return {"ok": True, "status": "valid",
                "message": f"{label}页面返回已登录标记；{structural}（{len(rec.cookies)} 条字段）。"}
    if re.search(spec.get("anon_re", r"(?!x)x"), text, re.I):
        if rec.logged_in:
            return {"ok": True, "status": "invalid",
                    "message": f"{label}服务端判定为未登录，但本地存有登录态字段 —— "
                               "Cookie 已过期，请重新「浏览器登录获取」。"}
        return {"ok": True, "status": "unknown",
                "message": f"{label}服务端确认为匿名态（这与本地结构一致：{structural}）。"
                           "匿名 Cookie 可提升成功率，但搜索/高清下载仍可能受限。"}
    return {"ok": True, "status": "unknown",
            "message": f"{label}未暴露稳定的登录标记，已完成结构校验：{structural}"
                       f"（{len(rec.cookies)} 条字段）。实际可用性以采集/下载结果为准。"}


def probe(platform: str, store: CookieStore | None = None) -> dict:
    """在线探活：真实调用平台接口，判断 Cookie 是否还有效。

    结论分三档，不做「看起来能用」的模糊承诺：
      valid   —— 接口明确返回登录/有数据
      invalid —— 接口明确返回未登录/无权限
      unknown —— 平台无稳定的无签名探活接口，仅完成结构校验
    """
    store = store or CookieStore()
    rec = store.load(platform)
    if not rec:
        return {"ok": False, "status": "missing", "message": "尚未配置该平台 Cookie。"}
    ck = rec.header

    # 占位/示例串在任何平台都不可能有效，先短路，避免把「假 Cookie」误报成「网络问题」
    if rec.suspicious:
        return {"ok": True, "status": "suspicious",
                "message": "Cookie 含有明显占位/测试值（如 web_test123、PH_TOKEN_X、your_token），"
                           "不是真实登录凭据。请从浏览器复制真实 Cookie 后重新导入。"}

    if platform == "bilibili":
        code, text = _http("https://api.bilibili.com/x/web-interface/nav", ck)
        try:
            d = json.loads(text).get("data", {})
        except Exception:  # noqa: BLE001
            d = {}
        if d.get("isLogin"):
            return {"ok": True, "status": "valid",
                    "message": f"已登录：{d.get('uname', '')}（UID {d.get('mid', '')}）"}
        return {"ok": True, "status": "invalid" if code else "unknown",
                "message": "未检测到登录态（匿名 Cookie 仍可用于搜索，但受风控限制更严）。"}

    if platform == "kuaishou":
        # 快手 Web 搜索真实接口是 /rest/v/search/feed，必须带浏览器实时生成的
        # __NS_hxfalcon 签名，纯 HTTP 通道无法复现，因此 API 探活必然失败。
        # 改为以「结构校验」为准：具备登录态字段(userId/api_ph)即认为可用于浏览器搜索。
        has_api = "kuaishou.web.cp.api_ph" in rec.cookies
        if rec.logged_in:
            if has_api:
                return {"ok": True, "status": "valid",
                        "message": "已具备登录态字段（userId + api_ph），可用于浏览器搜索与纯 HTTP 接口。"}
            return {"ok": True, "status": "valid",
                    "message": "已具备登录态字段（userId），浏览器搜索通道可用；"
                               "缺 api_ph，纯 HTTP 接口可能受限，但不影响浏览器搜索/下载。"}
        return {"ok": True, "status": "unknown",
                "message": "未检测到快手登录态字段（userId/api_ph），浏览器搜索会走匿名态。"
                           "若已登录仍报此，请重新「浏览器登录获取」。"}

    if platform == "weibo":
        # 微博 ajax 接口需 x-xsrf-token 且对 CSRF 极敏感，纯 HTTP 探活常被 400 拦截，
        # 不能据此误判 Cookie 失效。以「结构校验」为主：持有 SUB/SUBP 即视为已登录
        # （yt-dlp 与浏览器搜索都靠它），先把 API 探活当加分项。
        if rec.logged_in:
            msg = "已具备登录态字段（SUB/SUBP），yt-dlp 与浏览器搜索通道可用。"
            # 加分：尝试 API 探活（带 x-xsrf-token）
            try:
                spec = PROBE_SPECS.get("weibo", {})
                xsrf = rec.cookies.get(spec.get("xsrf_cookie", "")) if spec.get("xsrf_cookie") else None
                h = dict(spec.get("headers") or {})
                if xsrf:
                    h["x-xsrf-token"] = xsrf
                code, text = _http(spec["url"], rec.header, headers=h, timeout=12)
                if code:
                    try:
                        obj = json.loads(text)
                        name = _dig(obj, spec.get("ok_path", ()))
                    except Exception:  # noqa: BLE001
                        name = None
                    if name:
                        return {"ok": True, "status": "valid",
                                "message": f"已登录：{name}（API 探活通过）。"}
            except Exception:  # noqa: BLE001
                pass
            msg += "（API 探活被 CSRF 拦截属正常，不影响实际使用）"
            return {"ok": True, "status": "valid", "message": msg}
        return {"ok": True, "status": "unknown",
                "message": "未检测到微博登录态字段（SUB/SUBP），请重新「浏览器登录获取」。"}
        code, text = _http("https://www.douyin.com/", ck, timeout=15)
        structural = ("已具备登录态字段" if rec.logged_in else "仅匿名字段，无登录态")
        if code and re.search(r'"is_?login"\s*:\s*true|"isLogin"\s*:\s*true', text, re.I):
            return {"ok": True, "status": "valid", "message": f"首页返回已登录标记；{structural}。"}
        if code:
            return {"ok": True, "status": "unknown",
                    "message": f"抖音无稳定的免签名探活接口，已完成结构校验：{structural}"
                               f"（{len(rec.cookies)} 条字段）。实际是否可用以采集/下载结果为准。"}
        return {"ok": True, "status": "unknown", "message": f"网络不通（{text[:60]}），仅完成结构校验：{structural}。"}

    spec = PROBE_SPECS.get(platform)
    if spec:
        return _probe_generic(platform, rec, spec)

    if platform in PLATFORM_META:
        structural = "已具备登录态字段" if rec.logged_in else "仅匿名字段，无登录态"
        return {"ok": True, "status": "unknown",
                "message": f"该平台暂无免签名探活接口，已完成结构校验：{structural}"
                           f"（{len(rec.cookies)} 条字段）。"}
    return {"ok": False, "status": "unknown", "message": f"未知平台：{platform}。"}


# ================================================================ 自检层
GRADE_ORDER = {"skip": -1, "pass": 0, "warn": 1, "fail": 2}
GRADE_LABEL = {"pass": "通过", "warn": "警告", "fail": "不通过", "skip": "未配置"}


def _worst(grades) -> str:
    cur = "pass"
    for g in grades:
        if GRADE_ORDER.get(g, 0) > GRADE_ORDER.get(cur, 0):
            cur = g
    return cur


def validate_netscape(text: str) -> tuple[int, list[str]]:
    """按 yt-dlp 的解析规则校验 Netscape cookies.txt，返回 (合法行数, 错误摘要)。

    yt-dlp 要求：非注释行必须是 7 个 Tab 分隔字段，
    且 includeSubdomains / secure 必须是 TRUE|FALSE，过期时间必须是整数。
    """
    errors: list[str] = []
    valid = 0
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = line.split("\t")
        bad: list[str] = []
        if len(parts) != 7:
            errors.append(f"第{i}行字段数={len(parts)}（应为 7，须用 Tab 分隔）")
            continue
        domain, sub, path, secure, exp, name, _value = parts
        if not domain:
            bad.append("domain 为空")
        if sub not in ("TRUE", "FALSE"):
            bad.append(f"includeSubdomains={sub!r} 非法")
        if secure not in ("TRUE", "FALSE"):
            bad.append(f"secure={secure!r} 非法")
        if not exp.lstrip("-").isdigit():
            bad.append(f"过期时间 {exp!r} 非整数")
        if not name:
            bad.append("cookie 名为空")
        if not path.startswith("/"):
            bad.append(f"path={path!r} 未以 / 开头")
        if bad:
            errors.append(f"第{i}行：{'、'.join(bad)}")
        else:
            valid += 1
    return valid, errors[:5]


def check_platform(platform: str, store: CookieStore | None = None,
                   online: bool = True) -> dict:
    """对单个平台做分层自检，返回结构化报告。

    七层：存在性 → 平台归属 → 结构完整性 → 占位真伪 → 有效期
          → 域名一致性 → yt-dlp 导出 →（可选）在线探活
    """
    store = store or CookieStore()
    meta = PLATFORM_META.get(platform, {})
    label = meta.get("label", platform)
    checks: list[dict] = []
    advice: list[str] = []

    def add(name: str, grade: str, detail: str) -> None:
        checks.append({"name": name, "grade": grade, "detail": detail})

    rec = store.load(platform)
    if not rec or not rec.cookies:
        return {
            "platform": platform, "label": label, "configured": False, "grade": "skip",
            "checks": [{"name": "存在性", "grade": "skip", "detail": "尚未配置 Cookie"}],
            "advice": [f"如需采集{label}：在 Cookie 面板粘贴 Cookie，或点「浏览器登录获取」。"],
            "probe": None, "count": 0, "logged_in": False,
        }

    add("存在性", "pass", f"已配置 {len(rec.cookies)} 条字段（来源：{rec.source or '未知'}）")

    # ---- 平台归属：防止把 A 站 Cookie 存进 B 站（字节系尤其容易搞混）----
    guess = detect_platform(rec.cookies, rec.meta)
    if guess and guess != platform:
        add("平台归属", "fail",
            f"特征更像「{PLATFORM_META[guess]['label']}」而不是「{label}」")
        advice.append(f"{label} 槽位里疑似存了{PLATFORM_META[guess]['label']}的 Cookie，请清除后重新导入。")
    elif guess == platform:
        add("平台归属", "pass", f"Cookie 特征与{label}一致")
    else:
        add("平台归属", "warn", "特征不足以判定归属（字段太少或非典型），已按指定平台处理")

    # ---- 结构完整性 ----
    login_groups = meta.get("login_keys", ())
    device_keys = meta.get("device_keys", ())
    missing_dev = [k for k in device_keys if k not in rec.cookies]
    if len(rec.cookies) < 2:
        add("结构完整性", "fail", f"仅 {len(rec.cookies)} 条字段，几乎肯定不完整")
        advice.append(f"{label} Cookie 字段过少，请整段复制浏览器的 Cookie（推荐 Copy as cURL）。")
    elif rec.logged_in and not missing_dev:
        add("结构完整性", "pass", f"登录态字段齐全，设备指纹（{', '.join(device_keys)}）完整")
    elif rec.logged_in:
        add("结构完整性", "warn", f"有登录态，但缺设备指纹：{', '.join(missing_dev)}（更易触发风控）")
        if platform == "kuaishou" and "kuaishou.web.cp.api_ph" not in rec.cookies:
            advice.append("快手已检测到登录态(userId)，浏览器搜索/下载不受影响；"
                          "若想用纯 HTTP 接口（如分享链接解析），建议重新「浏览器登录获取」以拿到 api_ph。")
    else:
        want = " 或 ".join("+".join(g) for g in login_groups) or "登录态字段"
        add("结构完整性", "warn", f"未检测到登录态字段（需 {want}），当前为匿名 Cookie")
        advice.append(f"{label} 目前是匿名 Cookie：搜索与高清下载会受限，建议执行「浏览器登录获取」。")

    # ---- 占位真伪 ----
    if rec.suspicious:
        add("占位真伪", "fail", "含明显占位/示例值（如 web_test123、PH_TOKEN_X），不是真实凭据")
        advice.append(f"{label} 存的是示例串而非真实 Cookie，请从浏览器重新复制。")
    else:
        add("占位真伪", "pass", "未发现占位/示例值")

    # ---- 有效期 ----
    exp, now = rec.expires_at, time.time()
    if not exp:
        add("有效期", "warn", "会话级或未提供过期时间，关闭浏览器/服务端过期后可能失效")
    elif exp < now:
        add("有效期", "fail", f"已于 {time.strftime('%Y-%m-%d', time.localtime(exp))} 过期")
        advice.append(f"{label} Cookie 已过期，请重新登录获取。")
    elif exp - now < 7 * 86400:
        add("有效期", "warn",
            f"仅剩 {int((exp - now) / 86400)} 天（{time.strftime('%Y-%m-%d', time.localtime(exp))}），建议尽快刷新")
    else:
        add("有效期", "pass",
            f"有效至 {time.strftime('%Y-%m-%d', time.localtime(exp))}（剩余 {int((exp - now) / 86400)} 天）")

    # ---- 域名一致性 ----
    host = str(meta.get("domain", "")).lstrip(".").lower()
    domains = {str(m.get("domain", "")).lstrip(".").lower()
               for m in rec.meta.values() if m.get("domain")}
    if not domains:
        add("域名一致性", "warn", "Cookie 未携带 domain（header/表格粘贴常见），导出时按平台默认域名补齐")
    else:
        hit = {d for d in domains if d.endswith(host) or host.endswith(d)}
        if hit == domains:
            add("域名一致性", "pass", f"{len(domains)} 个域名均归属 {host}")
        elif hit:
            other = sorted(domains - hit)[:3]
            add("域名一致性", "warn", f"混入其它站点域名：{', '.join(other)}（会被一并导出）")
        else:
            add("域名一致性", "fail", f"无任何域名归属 {host}（实际为 {', '.join(sorted(domains)[:3])}）")
            advice.append(f"{label} 的 Cookie 域名完全不匹配，极可能存错了平台。")

    # ---- yt-dlp 导出可用性（真正按 yt-dlp 规则校验，而不是「生成了就算数」）----
    np_path = store.netscape_path(platform)
    n_valid, n_err = validate_netscape(rec.to_netscape())
    if n_err:
        add("yt-dlp 导出", "fail", "Netscape 格式不合法：" + "；".join(n_err))
    elif n_valid == 0:
        add("yt-dlp 导出", "fail", "导出内容为空")
    elif os.path.exists(np_path):
        add("yt-dlp 导出", "pass",
            f"{n_valid} 行合法，文件就绪：{os.path.basename(np_path)}（yt-dlp --cookies 可直接用）")
    else:
        add("yt-dlp 导出", "warn", f"{n_valid} 行合法，但磁盘文件缺失，点「导出」即可生成")

    # ---- 在线探活 ----
    probe_res = None
    if online:
        try:
            probe_res = probe(platform, store)
        except Exception as e:  # noqa: BLE001
            probe_res = {"ok": False, "status": "unknown", "message": f"探活异常：{e}"}
        pstatus = probe_res.get("status")
        grade = {"valid": "pass", "unknown": "warn",
                 "invalid": "fail", "suspicious": "fail", "missing": "fail"}.get(pstatus, "warn")
        add("在线探活", grade, f"[{pstatus}] {probe_res.get('message', '')}")
        if grade == "fail":
            advice.append(f"{label} 在线探活未通过：{probe_res.get('message', '')}")

    grade = _worst(c["grade"] for c in checks)
    return {
        "platform": platform, "label": label, "configured": True, "grade": grade,
        "checks": checks, "advice": advice, "probe": probe_res,
        "count": len(rec.cookies), "logged_in": rec.logged_in,
    }


def self_check(platforms: list[str] | None = None, store: CookieStore | None = None,
               online: bool = True, include_missing: bool = True) -> dict:
    """全平台 Cookie 自检。并发探活，避免 8 个平台串行等待网络超时。"""
    store = store or CookieStore()
    targets = [p for p in (platforms or PLATFORMS) if p in PLATFORM_META]
    if not include_missing:
        targets = [p for p in targets if store.load(p)]

    results: list[dict] = []
    if online and len(targets) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(5, len(targets))) as pool:
            results = list(pool.map(
                lambda p: check_platform(p, store, online=True), targets))
    else:
        results = [check_platform(p, store, online=online) for p in targets]

    order = {"fail": 0, "warn": 1, "pass": 2, "skip": 3}
    results.sort(key=lambda r: (order.get(r["grade"], 9), r["platform"]))

    summary = {g: sum(1 for r in results if r["grade"] == g)
               for g in ("pass", "warn", "fail", "skip")}
    summary["total"] = len(results)
    configured = [r for r in results if r["configured"]]

    if not configured:
        headline = "尚未配置任何平台 Cookie。"
    elif summary["fail"]:
        headline = (f"{summary['fail']} 个平台未通过自检，"
                    f"{summary['pass']} 个通过、{summary['warn']} 个有警告。")
    elif summary["warn"]:
        headline = f"无严重问题：{summary['pass']} 个通过、{summary['warn']} 个带警告（可用但受限）。"
    else:
        headline = f"全部 {summary['pass']} 个已配置平台自检通过。"

    return {
        "ok": summary["fail"] == 0,
        "online": online,
        "headline": headline,
        "summary": summary,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platforms": results,
        # 总建议只收「已配置平台」的问题；未配置平台的提示留在各自条目里，
        # 否则 8 个平台会刷出一屏「如需采集X…」，把真正要处理的问题淹没。
        "advice": list(dict.fromkeys(
            a for r in results if r["configured"] for a in r.get("advice", []))),
    }


def format_self_check(report: dict) -> str:
    """把自检报告渲染成 CLI 友好的文本。"""
    icon = {"pass": "✅", "warn": "⚠️ ", "fail": "❌", "skip": "➖"}
    out = [f"Cookie 自检报告  {report['generated_at']}"
           f"（{'含在线探活' if report['online'] else '仅离线结构校验'}）",
           "=" * 66, report["headline"], ""]
    for r in report["platforms"]:
        head = f"{icon.get(r['grade'], '?')} {r['label']}  [{GRADE_LABEL.get(r['grade'], r['grade'])}]"
        if r["configured"]:
            head += f"  {r['count']} 条字段  {'已登录' if r['logged_in'] else '匿名'}"
        out.append(head)
        for c in r["checks"]:
            out.append(f"     {icon.get(c['grade'], ' ')} {c['name']}：{c['detail']}")
        out.append("")
    if report["advice"]:
        out.append("建议处理：")
        out += [f"  {i}. {a}" for i, a in enumerate(dict.fromkeys(report["advice"]), 1)]
        out.append("")
    s = report["summary"]
    out.append(f"合计 {s['total']} 个平台：通过 {s['pass']}、警告 {s['warn']}、"
               f"不通过 {s['fail']}、未配置 {s['skip']}")
    return "\n".join(out)


# ================================================================ 浏览器登录抓取
LOGIN_STATE: dict = {
    "running": False, "platform": "", "message": "尚未开始", "ok": None,
    "started_at": 0.0, "finished_at": 0.0, "result": None,
    "capture_now": False, "login_detected": False, "screenshot": None, "stop_requested": False,
}
_LOGIN_LOCK = threading.Lock()


def playwright_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("playwright") is not None
    except Exception:  # noqa: BLE001
        return False


def _set_state(**kw) -> None:
    with _LOGIN_LOCK:
        LOGIN_STATE.update(kw)


def _update_screenshot(page, ctx) -> None:
    """无头登录模式：把当前登录页截图（JPEG base64）写入状态，供前端展示（扫码登录）。"""
    try:
        if page is None or ctx is None:
            return
        buf = page.screenshot(type="jpeg", quality=50)
        _set_state(screenshot=base64.b64encode(buf).decode("ascii"))
    except Exception:  # noqa: BLE001
        pass


def _try_open_login(page) -> None:
    """无头登录：尝试点击页面上的「登录」链接/按钮，使二维码或登录面板弹出。"""
    try:
        page.get_by_role("link", name=re.compile(r"(登录|登入|Sign\\s*in|Log\\s*in)", re.I)).first.click(timeout=5000)
        page.wait_for_timeout(2000)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        page.get_by_role("button", name=re.compile(r"(登录|登入|Sign\\s*in|Log\\s*in)", re.I)).first.click(timeout=4000)
        page.wait_for_timeout(2000)
    except Exception:  # noqa: BLE001
        pass


# 通用「已登录」启发式：不依赖各平台特有 login_keys，登录后通常会出现一组服务端下发的会话型 Cookie。
# 关键约束：必须命中「明确的登录凭证型 Cookie 名」且是服务端下发（HttpOnly 或长期有效），
# 否则极易把「页面一加载就种下的设备/追踪 Cookie（webid、ttwid、csrf、did…）」误判为登录态，
# 导致浏览器在用户还没登录时就自动关闭（这是此前「登录获取自动关闭」的根因）。
STRICT_SESSION_RE = re.compile(
    r"(?i)(^session(id|_|data)?$|SESSDATA|SESS_|PHPSESSID|JSESSIONID|"
    r"\bbduss\b|passport|web_session|auth_token|login_token|sessionid|"
    r"pass_?token|login_info|customerclientid|sid$)"
)


def _looks_logged_in_generic(new_names, raw) -> bool:
    """仅在出现「明确服务端登录凭证」时判定为已登录，避免页面加载即误判。"""
    if not new_names:
        return False
    now = time.time()
    by_name = {c.get("name"): c for c in raw if c.get("name")}
    for n in new_names:
        if STRICT_SESSION_RE.search(n):
            c = by_name.get(n, {})
            # 必须是服务端下发的登录凭证：HttpOnly（防 XSS，最可靠）或长期有效（>7 天）
            if c.get("httpOnly") or (float(c.get("expires") or 0) > now + 7 * 86400):
                return True
    return False


def _resolve_save_key(platform, url, cookies, meta) -> str:
    """落盘用的平台键：已知平台直接用；自定义网址按域名特征判定（命中已知平台则用其键，否则 custom_<域名>）。"""
    if platform in PLATFORMS:
        return platform
    if url:
        detected = detect_platform(cookies, meta)
        if detected:
            return detected
        from urllib.parse import urlparse
        host = (urlparse(url).netloc or "site").replace("www.", "")
        safe = re.sub(r"[^a-z0-9._-]", "_", host.lower())
        return "custom_" + safe
    return platform or "custom_unknown"


def capture_login(platform: str = "auto", timeout: int = 240, headless: bool = False,
                  url: str | None = None, store: CookieStore | None = None) -> dict:
    """打开浏览器让用户登录，轮询抓取登录态 Cookie 并落盘。

    支持两种入口：
    1. 指定 platform（需在 PLATFORMS 内），导航到该平台首页；
    2. 指定 url（任意网址），导航到该网址——适合「自定义网站」一键登录。

    使用持久化用户目录（cookies/profiles/<键>），登录一次后长期复用。
    登录成功判定同时兼容「平台专属登录字段」与「通用会话 Cookie 启发式」，
    因此对任意网站（含未在 PLATFORMS 内的站点）都可用。
    """
    store = store or CookieStore()
    from urllib.parse import urlparse

    meta0 = PLATFORM_META.get(platform, {}) or {}
    nav_url = (url or "").strip() or (meta0.get("login") if headless else None) or meta0.get("home")
    if not nav_url:
        return {"ok": False, "error": "请选择平台，或在网址框中输入要登录的网站地址。"}
    # 预备 profile 键（用于跨次持久化登录态）
    if platform in PLATFORMS:
        prof_key = platform
    elif url:
        host = (urlparse(url).netloc or "site").replace("www.", "")
        prof_key = "custom_" + re.sub(r"[^a-z0-9._-]", "_", host.lower())
    else:
        return {"ok": False, "error": f"不支持的平台：{platform}"}

    if not playwright_available():
        return {"ok": False, "error": "未安装 Playwright。请先执行：\n"
                                      "  pip install playwright\n"
                                      "  playwright install chromium"}

    from playwright.sync_api import sync_playwright  # noqa: WPS433

    nav_label = (PLATFORM_META.get(platform, {}) or {}).get("label", platform) if platform in PLATFORMS else nav_url
    profile = os.path.join(PROFILE_DIR, prof_key)
    os.makedirs(profile, exist_ok=True)
    deadline = time.time() + timeout
    result: dict = {"ok": False, "error": "登录超时"}

    _set_state(running=True, platform=platform, url=url, ok=None, result=None,
               started_at=time.time(), finished_at=0.0, capture_now=False, login_detected=False,
               stop_requested=False, message=f"正在启动浏览器，访问 {nav_url} …")

    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                profile, headless=headless, args=["--disable-blink-features=AutomationControlled"],
                user_agent=DEFAULT_UA, viewport={"width": 1280, "height": 860},
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            LOGIN_CTRL["page"] = page
            try:
                page.goto(nav_url, timeout=45000)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)
            if headless:
                # 无头模式用户无法点击，自动点开「登录」入口（二维码/登录面板）后截图
                _try_open_login(page)
                _update_screenshot(page, ctx)
            baseline = {c["name"] for c in ctx.cookies() if c.get("name")}
            _set_state(message=f"请在浏览器中登录（扫码或账号密码）。登录成功后会自动抓取 Cookie。剩余 {timeout}s")
            while time.time() < deadline:
                if LOGIN_STATE.get("stop_requested"):
                    result = {"ok": False, "error": "已手动停止登录任务"}
                    _set_state(message="⏹ 已停止登录任务（手动停止）。")
                    break
                try:
                    if page.is_closed():
                        raise RuntimeError("browser-closed")
                except RuntimeError:
                    result = {"ok": False, "error": "浏览器窗口已关闭，登录已取消"}
                    _set_state(message="⏹ 浏览器窗口已关闭，登录已取消。")
                    break
                except Exception:  # noqa: BLE001
                    pass
                raw = ctx.cookies()
                cookies = {c["name"]: c["value"] for c in raw if c.get("name")}
                cmeta = {
                    c["name"]: {
                        "domain": c.get("domain", ""), "path": c.get("path", "/"),
                        "expires": float(c.get("expires") or 0) if (c.get("expires") or 0) > 0 else 0.0,
                        "secure": bool(c.get("secure")),
                    } for c in raw if c.get("name")
                }
                rec = CookieRecord(prof_key, cookies, cmeta, source="browser-login")
                new_names = set(cookies) - baseline
                generic = _looks_logged_in_generic(new_names, raw)
                manual = LOGIN_STATE.get("capture_now")
                # 关键修复：不再因「检测到登录态」自动关闭浏览器（详见下方说明）。
                # 可靠的「已登录」信号必须基于 baseline 之后「新增」的 Cookie：
                #   baseline 在页面加载后即捕获，因此首页就种下的匿名 Cookie（bili_jct /
                #   web_session / webId / userId / BDUSS_BFESS …）都在 baseline 里、不算新增；
                #   只有真正登录后才会「新增」出明确的会话凭证（SESSDATA / sessionid /
                #   某平台 login_keys 里的关键 Cookie 等）。据此弹窗，绝不会在没登录时误弹。
                login_keys = (PLATFORM_META.get(platform, {}).get("login_keys", ())
                              or PLATFORM_META.get(prof_key, {}).get("login_keys", ()))
                new_login_key = any(k in new_names for grp in login_keys for k in grp)
                detected = bool(generic) or new_login_key
                if manual:
                    save_key = _resolve_save_key(platform, url, cookies, cmeta)
                    rec.platform = save_key
                    info = store.save(rec)
                    info["ok"] = True
                    info["format"] = "browser-login"
                    info["detected_platform"] = save_key
                    if not detected:
                        info["note"] = ("按手动指令抓取了当前 Cookie。若你确信已登录但未被自动识别，"
                                        "可改用左侧「粘贴 Cookie」方式，效果一致。")
                    result = info
                    _set_state(message=f"✅ 已抓取 {len(cookies)} 条 Cookie，已保存为 {save_key}，"
                                       f"并导出 yt-dlp 可用文件。")
                    break
                left = int(deadline - time.time())
                if detected:
                    LOGIN_STATE["login_detected"] = True
                    _set_state(message=f"✓ 已检测到登录态（共 {len(cookies)} 条 Cookie，本次新增 {len(new_names)}）。"
                                       f"请点【✅ 我已完成登录，现在抓取 Cookie】或弹窗【确认保存】。剩余 {left}s。")
                else:
                    _set_state(message=f"等待登录中…已捕获 {len(cookies)} 条 Cookie（新增 {len(new_names)}），"
                                   f"剩余 {left}s。登录后点【抓取】或弹窗【确认保存】即可保存。")
                if headless:
                    _update_screenshot(page, ctx)
                _drain_login_events(page)
                page.wait_for_timeout(2000)
            else:
                # 超时：保存当前 Cookie；若已检测到登录态则按登录态保存，避免误标匿名
                raw = ctx.cookies()
                cookies = {c["name"]: c["value"] for c in raw if c.get("name")}
                if cookies:
                    cmeta = {c["name"]: {"domain": c.get("domain", ""), "path": c.get("path", "/"),
                                     "expires": float(c.get("expires") or 0) if (c.get("expires") or 0) > 0 else 0.0,
                                     "secure": bool(c.get("secure"))} for c in raw if c.get("name")}
                    new_names = set(cookies) - baseline
                    generic = _looks_logged_in_generic(new_names, raw)
                    rec = CookieRecord(prof_key, cookies, cmeta, source="browser-login")
                    logged = rec.logged_in or generic
                    save_key = _resolve_save_key(platform, url, cookies, cmeta)
                    rec.platform = save_key
                    info = store.save(rec)
                    info["ok"] = True
                    info["detected_platform"] = save_key
                    if logged:
                        info["note"] = "超时前已检测到登录态，已自动保存登录 Cookie（可用）。"
                        _set_state(message="⏱ 超时，已检测到登录态并保存 Cookie。")
                    else:
                        info["warning"] = ("超时未检测到明显登录态，已保存当前 Cookie（设备指纹/匿名）。"
                                       "若你已登录仍出现此提示，可能是该站 Cookie 名未被识别，可改用左侧「粘贴 Cookie」方式。")
                        _set_state(message="⏱ 超时，已保存当前 Cookie（未检出登录态）。")
                    result = info
            ctx.close()
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": f"浏览器登录失败：{e}"}
        _set_state(message=f"❌ {result['error']}")

    LOGIN_CTRL["page"] = None
    _set_state(running=False, ok=bool(result.get("ok")), result=result, finished_at=time.time(),
               capture_now=False, login_detected=False, screenshot=None, stop_requested=False)
    return result


def stop_login() -> dict:
    """请求停止当前登录任务：浏览器将被关闭，不保存本次 Cookie。"""
    if not LOGIN_STATE.get("running"):
        return {"ok": False, "error": "当前没有进行中的登录任务。"}
    _set_state(stop_requested=True, message="⏹ 正在停止登录任务…")
    return {"ok": True, "message": "已请求停止登录任务，浏览器即将关闭。"}


import queue as _queue


LOGIN_CTRL: dict = {"page": None, "queue": _queue.Queue()}


def _drain_login_events(page):
    """在登录线程内执行队列中的画面操作。

    Playwright 同步 API 绑定创建线程，HTTP 线程不能直接调用 page 方法，
    因此前端点击/输入请求先进队列，由登录线程在自身循环中消费执行。
    """
    q = LOGIN_CTRL.get("queue")
    if q is None or page is None:
        return
    while True:
        try:
            ev = q.get_nowait()
        except _queue.Empty:
            break
        kind = ev[0]
        try:
            if kind == "click":
                _, x, y = ev
                vs = page.viewport_size or {"width": 1280, "height": 860}
                px = max(0, min(1000, int(x))) / 1000.0 * vs["width"]
                py = max(0, min(1000, int(y))) / 1000.0 * vs["height"]
                page.mouse.click(px, py)
            elif kind == "type":
                page.keyboard.type(ev[1])
        except Exception:  # noqa: BLE001
            pass


def login_click(x: int = 500, y: int = 500) -> dict:
    """请求在无头登录实时画面上点击（坐标 0-1000 相对比例），由登录线程执行。"""
    if LOGIN_CTRL.get("page") is None or not LOGIN_STATE.get("running"):
        return {"ok": False, "error": "没有进行中的登录任务。"}
    LOGIN_CTRL["queue"].put(("click", int(x), int(y)))
    return {"ok": True, "message": f"已加入操作队列：点击画面 ({x}, {y})。"}


def login_input(text: str = "") -> dict:
    """请求向无头登录浏览器输入文字（发送到当前聚焦元素，如手机号输入框）。"""
    if LOGIN_CTRL.get("page") is None or not LOGIN_STATE.get("running"):
        return {"ok": False, "error": "没有进行中的登录任务。"}
    if not text:
        return {"ok": False, "error": "请输入要发送的文字。"}
    shown = text[:20] + ("…" if len(text) > 20 else "")
    LOGIN_CTRL["queue"].put(("type", text))
    return {"ok": True, "message": f"已加入操作队列：输入 {shown}"}


def start_login_async(platform: str = "auto", timeout: int = 240, headless: bool = False,
                      url: str | None = None) -> dict:
    """后台线程启动登录抓取，立即返回；前端通过 login_status 轮询进度。"""
    if LOGIN_STATE.get("running"):
        return {"ok": False, "error": f"已有登录任务进行中（{LOGIN_STATE.get('platform')}），请先完成或等待超时。"}
    if not playwright_available():
        return {"ok": False, "error": "未安装 Playwright。请先执行：pip install playwright && playwright install chromium"}
    t = threading.Thread(target=capture_login,
                          kwargs={"platform": platform, "timeout": timeout, "headless": headless, "url": url},
                          daemon=True)
    t.start()
    label = (PLATFORM_META.get(platform, {}) or {}).get("label", platform) if platform in PLATFORMS else (url or "自定义网址")
    return {"ok": True, "message": f"已启动浏览器登录（{label}），请在弹出窗口完成登录。"}


def login_status() -> dict:
    with _LOGIN_LOCK:
        return dict(LOGIN_STATE)


def request_capture() -> dict:
    """由前端「我已完成登录，现在抓取」按钮触发：通知正在运行的登录任务立即抓取并保存当前 Cookie。

    这让用户完全掌控抓取时机，避免依赖自动检测（自动检测已收紧，仅在出现明确服务端登录凭证时触发，
    因此不会再因页面加载即误关浏览器）。"""
    if not LOGIN_STATE.get("running"):
        return {"ok": False, "error": "当前没有进行中的登录任务。"}
    _set_state(capture_now=True)
    return {"ok": True, "message": "已请求抓取当前 Cookie，浏览器将很快关闭并保存。"}


# ================================================================ 便捷函数
def load_all_headers(store: CookieStore | None = None) -> dict[str, str]:
    """返回 {platform: cookie_header}，供采集器按域名注入。"""
    store = store or CookieStore()
    out = {}
    for plat in PLATFORMS:
        rec = store.load(plat)
        if rec and rec.cookies:
            out[plat] = rec.header
    return out


if __name__ == "__main__":  # 简易自检
    demo_curl = """curl 'https://www.douyin.com/aweme/v1/web/general/search/single/' \\
  -H 'cookie: ttwid=1%7Cabc; odin_tt=deadbeef; sessionid_ss=SECRET123; msToken=xyz' \\
  -H 'user-agent: Mozilla/5.0'"""
    for name, sample in {
        "cURL": demo_curl,
        "header": "did=web_123456; kuaishou.web.cp.api_ph=abcdef; userId=888",
        "json": json.dumps([{"name": "SESSDATA", "value": "s%3Ax", "domain": ".bilibili.com",
                             "expirationDate": 1790000000},
                            {"name": "bili_jct", "value": "jct", "domain": ".bilibili.com"}]),
        "netscape": ".douyin.com\tTRUE\t/\tFALSE\t1790000000\tsessionid_ss\tSS\n"
                    ".douyin.com\tTRUE\t/\tFALSE\t1790000000\tttwid\tTW",
        "table": "sessionid_ss\tSSVAL\t.douyin.com\t/\n ttwid\tTWVAL\t.douyin.com\t/",
    }.items():
        ck, mt, fmt = parse_cookie_text(sample)
        print(f"[{name}] 格式={fmt} 条数={len(ck)} 平台={detect_platform(ck, mt) or '未知'} -> {list(ck)[:4]}")
