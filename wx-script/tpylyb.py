# name: 太平洋蓝医保
# cron: 48 6,7 * * *
# -*- coding: utf-8 -*-
"""
name: 太平洋蓝医保（健康分 2.0）
Date: 2026-10-28
YYB_SERVER：YYB-Go-Enhanced 路由，每行：地址@账号标识
YYB_API_KEY / YYB_PROTOCOL_TOKEN:       启用协议鉴权时配置，二选一
账号自动从 yyb_go 拉取全部存活账号

2026-10-23 活动改版要点（健康分 2.0）：
  * 旧的 /lyb-api/healthScore/upgrade/* 接口全部下线（POST 一律 500「网络繁忙」）
  * 新接口在 /asset-api/**，且所有请求必须带防篡改三件套：
        ua-encrypt: 1 / timestamp(ms) / nonce = AES("<path>_<md5(键排序JSON body)>_<ts>")
    密钥=IV 前 16 字节 = "AD42F6697B035B7580E4FEF93BE20BAD"（小程序 env.CRYPTO_KEY）
  * 登录不再需要 wx-phone：新版 silentLogin 只调 /lyb-api/login/wx-session，
    用手机号 code 换 token 会拿到受限会话（所有健康分接口 500）

自动化范围（2026-10-28 实测）：
  * 早起打卡 ZQDK   ：06:00-09:00 服务端校验，dk-V1 直接打卡            +1 分/+10 豆
  * 健康问答 JKWD   ：答题→错题本/最长选项→答错可 revive 换题          +1 分/+10 豆
  * AI 识别三餐     ：上传本地餐食图 → AI 识别 → SUCCESS 即计分        +2 分/+20 豆
                      服务端用视觉模型判定图片，故按「图片通过率」排序择优，
                      当日失败自动换图重试（默认最多 3 张），逐日沉淀高通过率图库
  * AI 解读体检     ：上传报告图片 → AI 解读 → 医生审核 → 计分         +10 分/+100 豆
                      （审核为人工环节，状态 2=解读中 3=审核中 4=完成）
  * AI 带你运动     ：start→等 40s→finish（sessionId 客户端 UUID，服务端只登记会话）
                      服务端不校验姿态识别结果，qualifiedFlag=true 即计分      +2 分/+20 豆
  * 其余（步数需微信运动密文、领保障需实名投保）不代做

素材目录（按优先级探测：环境变量 TPYLYB_ASSET_DIR > 脚本同级 tpylyb_assets
> /ql/data/scripts/tpylyb_assets > /ql/scripts/tpylyb_assets）：
    meals/*.jpg|png    餐食照片，按「账号+日期」哈希轮换挑一张
    reports/*.png|jpg|pdf  体检报告，按「账号+月份+已提交次数」轮换（也可放真实报告）

环境变量：
    TPYLYB_MEAL=0            关闭 AI 三餐
    TPYLYB_MEAL_TRY          三餐当日最多尝试几张图（默认 3）
    TPYLYB_SPORT=0           关闭 AI 运动
    TPYLYB_SPORT_CODE        运动项目（默认 Jumping-Jack 开合跳）
    TPYLYB_SPORT_WAIT        运动时长秒数（默认 40）
    TPYLYB_REPORT_MAX        每账号每月最多提交体检报告次数（默认 3，服务端上限 5）
    TPYLYB_AUTO_ASSETS=0     关闭「素材缺失时自动初始化」（默认开启）
    TPYLYB_REPORT_MONTHS     用满整月配额时自动轮空（默认不轮空，按月重置）
    TPYLYB_WXVER             小程序版本号（默认 150，跟随官方发版）

素材自举（单文件内置，仓库不含任何图片）：
    首次运行若检测不到素材，会自动从 Wikimedia Commons 下载餐食实拍图、用 PIL 绘制
    体检报告，并写 SOURCES.md 记录图片许可，约 1-3 分钟一次性完成。
    也可手动触发：
        python3 tpylyb.py --bootstrap                  # 生成到默认素材目录
        python3 tpylyb.py --bootstrap --force          # 强制重新生成
        python3 tpylyb.py --bootstrap --meals-only     # 无 Pillow 时只下餐食图
        python3 tpylyb.py --bootstrap /path/to/dir     # 指定目录

version: 3.5.0
"""

import base64
import hashlib
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ===== YYB-Go-Enhanced + QingLong standalone adapter =====
def _yyb_routes():
    """解析 YYB_SERVER；支持 地址@ref 行与纯服务地址行（/accounts 自动发现）。"""
    routes = []
    bare_servers = []
    for number, line in enumerate(os.getenv("YYB_SERVER", "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if "@" not in line:
            server = line.strip().rstrip("/")
            if not server.lower().startswith(("http://", "https://")):
                server = "http://" + server
            bare_servers.append(server)
            continue
        server, ref = line.rsplit("@", 1)
        if not server.strip() or not ref.strip():
            raise RuntimeError(f"YYB_SERVER 第 {number} 行格式错误，应为 地址@账号标识")
        server = server.strip().rstrip("/")
        if not server.lower().startswith(("http://", "https://")):
            server = "http://" + server
        routes.append({"server": server, "ref": ref.strip()})
    # 纯服务地址行：GET /accounts 自动发现账号（失败降级，不抛异常）
    for server in dict.fromkeys(bare_servers):
        try:
            response = requests.get(server + "/accounts", timeout=15)
            data = response.json()
            items = data if isinstance(data, list) else None
            if items is None and isinstance(data, dict):
                for key in ("data", "list", "rows", "result", "accounts"):
                    value = data.get(key)
                    if isinstance(value, list) and value:
                        items = value
                        break
                    if isinstance(value, dict):
                        for k2 in ("list", "rows", "data", "result", "accounts"):
                            v2 = value.get(k2)
                            if isinstance(v2, list) and v2:
                                items = v2
                                break
                        if items:
                            break
                if items is None:
                    items = list(data.values())
            if items:
                for item in items:
                    ref = ""
                    if isinstance(item, str):
                        ref = item.strip()
                    elif isinstance(item, dict):
                        for key in ("ref", "openid", "openId", "wxid", "wx_id", "id", "account"):
                            if item.get(key) not in (None, ""):
                                ref = str(item[key]).strip()
                                break
                    if ref:
                        routes.append({"server": server, "ref": ref})
        except Exception as exc:
            print(f"⚠️ {server} /accounts 未解析到账号（{type(exc).__name__}），请确认 YYB 服务在线且已授权")
    if not routes:
        raise RuntimeError("未配置 YYB_SERVER（每行：地址@账号标识，或只填服务地址自动发现账号）")
    return routes


def _yyb_clean_ref(value):
    text = str(value or "").split("#", 1)[0].strip()
    for prefix in ("wx:", "yyb:", "wmpf:", "syzs:"):
        if text.lower().startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _yyb_route(identifier):
    routes = _yyb_routes()
    wanted = _yyb_clean_ref(identifier)
    for route in routes:
        if _yyb_clean_ref(route["ref"]) == wanted:
            return route
    if wanted.isdigit() and 0 < int(wanted) <= len(routes):
        return routes[int(wanted) - 1]
    if len(routes) == 1:
        return routes[0]
    raise RuntimeError(f"YYB_SERVER 中找不到账号标识：{wanted or '(空)'}")


def _yyb_call(endpoint, app_id, identifier, retries=3):
    """调 yyb_go；对 5xx/网络错误重试（yyb-go 偶发 502 Bad Gateway）。"""
    route = _yyb_route(identifier)
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
    if api_key:
        headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key
    last_error = None
    for attempt in range(retries):
        try:
            response = requests.post(route["server"] + endpoint,
                                     json={"ref": route["ref"], "app_id": app_id},
                                     timeout=30, headers=headers)
        except Exception as exc:
            last_error = exc
        else:
            if response.status_code >= 500:
                last_error = RuntimeError(f"HTTP {response.status_code}")
            else:
                body = response.json()
                if not isinstance(body, dict) or int(body.get("code", -1)) != 0:
                    raise RuntimeError(f"{endpoint} 返回失败："
                                       f"{body.get('msg') or body.get('message') or body}")
                result = (body.get("data") or {}).get("result")
                if result is None:
                    raise RuntimeError(f"{endpoint} 未返回 data.result")
                return result
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{endpoint} 调用失败（重试 {retries} 次）：{last_error}")


class _YybClient:
    def get_online_accounts(self):
        return [{"id": x["ref"], "openid": x["ref"], "wxid": x["ref"],
                 "remark": f"YYB账号{i}", "status": "online", "login_type": "WX"}
                for i, x in enumerate(_yyb_routes(), 1)]

    def get_accounts(self, force_refresh=False):
        return self.get_online_accounts()


class _YybCompat:
    YYBClient = _YybClient

    @staticmethod
    def load_accounts(*_args, **_kwargs):
        return _YybClient().get_online_accounts()

    @staticmethod
    def resolve_accounts(env_name=""):
        return [x["ref"] for x in _yyb_routes()]

    @staticmethod
    def get_global_server_url():
        return _yyb_routes()[0]["server"]

    @staticmethod
    def get_single_code(app_id, identifier):
        result = _yyb_call("/wxapp/getCode", app_id, identifier)
        code = result if isinstance(result, str) else result.get("code")
        if not code:
            raise RuntimeError("/wxapp/getCode 未返回 data.result.code")
        return str(code)

    @staticmethod
    def normalize_login_type(value):
        return str(value or "WX").upper()

    @staticmethod
    def login_type_label(value):
        return {"WX": "应用宝", "WMPF": "微信小程序", "SYZS": "手游助手"}.get(
            _YybCompat.normalize_login_type(value), "应用宝")

    @staticmethod
    def _cache_path(name):
        import pathlib
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
        return pathlib.Path(os.getenv("QL_DIR", "/ql/data")) / "config" / "yyb_token_caches" / f"{safe}.json"

    @classmethod
    def _read_cache(cls, name):
        try:
            return json.loads(cls._cache_path(name).read_text(encoding="utf-8"))
        except Exception:
            return {}

    @classmethod
    def get_cached_token(cls, name, identifier, options=None):
        item = cls._read_cache(name).get(_yyb_clean_ref(identifier))
        if not item:
            return None
        max_age = int((options or {}).get("max_age_ms", 0))
        if max_age and int(time.time() * 1000) - int(item.get("_saved_at", 0)) > max_age:
            return None
        return item

    @classmethod
    def save_cached_token(cls, name, identifier, value):
        data = cls._read_cache(name)
        data[_yyb_clean_ref(identifier)] = dict(value, _saved_at=int(time.time() * 1000))
        path = cls._cache_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @classmethod
    def remove_cached_token(cls, name, identifier):
        data = cls._read_cache(name)
        data.pop(_yyb_clean_ref(identifier), None)
        path = cls._cache_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


yyb = _YybCompat()


def _send_qinglong_notify(title, content):
    for path in (os.path.dirname(os.path.abspath(__file__)), "/ql/data/scripts", "/ql/scripts"):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        from notify import send as _ql_send
        _ql_send(title, content)
        return True
    except Exception as exc:
        print(f"青龙通知失败（不影响任务结果）：{exc}")
        return False
# ===== adapter end =====


# ==================== 配置 ====================
APP_NAME = "太平洋蓝医保"
WX_APPID = "wx41b377f9104ee1c2"
BASE_URL = "https://lyb-api.cpic.com.cn"
WX_VER = os.getenv("TPYLYB_WXVER", "150")
REFERER = f"https://servicewechat.com/{WX_APPID}/{WX_VER}/page-frame.html"
ENV_NAME = "lpzl_tpylyb"
CACHE_NAME = "tpylyb"

CRYPTO_KEY = b"AD42F6697B035B7580E4FEF93BE20BAD"

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANK_FILE = os.path.join(_BASE_DIR, "tpylyb_answer_bank.json")
STATE_FILE = os.path.join(_BASE_DIR, "tpylyb_state.json")
# 素材目录：环境变量优先，其次脚本同级，再退回青龙脚本根目录（订阅目录与素材目录常不同级）
_ASSET_CANDIDATES = [
    os.getenv("TPYLYB_ASSET_DIR", ""),
    os.path.join(_BASE_DIR, "tpylyb_assets"),
    "/ql/data/scripts/tpylyb_assets",
    "/ql/scripts/tpylyb_assets",
]
ASSET_DIR = next((p for p in _ASSET_CANDIDATES if p and os.path.isdir(p)), _ASSET_CANDIDATES[1])
_CK_FILE_LEGACY = os.path.join(_BASE_DIR, "tpylyb_token_cache.json")
# 体检报告状态：2=AI解读中 3=医生审核中 4=已完成（仅 4 计分）
STATE_LABEL = {2: "AI解读中", 3: "医生审核中", 4: "已完成"}

ZQDK_WINDOW = (6, 9)

# 体检报告：每月最多提交几次（服务端配额为 5）
REPORT_MONTHLY_MAX = int(os.getenv("TPYLYB_REPORT_MAX", "3"))
# 三餐：每天是否开启（默认开）
MEAL_ENABLED = os.getenv("TPYLYB_MEAL", "1") not in ("0", "false", "False")
# AI 运动：运动项目与「运动时长」（秒）；20 次开合跳约 35-40s 可判定合格
SPORT_ENABLED = os.getenv("TPYLYB_SPORT", "1") not in ("0", "false", "False")
SPORT_CODE = os.getenv("TPYLYB_SPORT_CODE", "Jumping-Jack")
SPORT_WAIT = int(os.getenv("TPYLYB_SPORT_WAIT", "40"))
# 三餐：当日最多换几张图重试（服务端视觉模型判定，换图可提高通过率）
MEAL_MAX_TRY = int(os.getenv("TPYLYB_MEAL_TRY", "3"))

TASK_HINT = {
    "JBRF": "步数任务需微信运动 encryptedData（真机授权），脚本不代做",
    "LQYWBZ": "领取意外保障需在投保页实名投保（真实保单），脚本仅给链接",
    "LQZJBZ": "领取重疾保障需在投保页实名投保（真实保单），脚本仅给链接",
}

try:
    SERVER_URL = yyb.get_global_server_url()
except Exception:
    # 未配置账号时也要能跑 --bootstrap（素材生成与账号无关）
    SERVER_URL = "(未配置 YYB_SERVER)"


def log(msg):
    print(msg, flush=True)


# ==================== 加解密 / 签名 ====================
def _aes_encrypt(text):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    cipher = AES.new(CRYPTO_KEY, AES.MODE_CBC, CRYPTO_KEY[:16])
    return base64.b64encode(cipher.encrypt(pad(text.encode("utf-8"), 16))).decode()


def _aes_decrypt(b64_text):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    cipher = AES.new(CRYPTO_KEY, AES.MODE_CBC, CRYPTO_KEY[:16])
    padded = cipher.decrypt(base64.b64decode(b64_text))
    return unpad(padded, 16).decode("utf-8")


def _sorted_json(obj):
    def norm(o):
        if isinstance(o, dict):
            return {k: norm(o[k]) for k in sorted(o)}
        if isinstance(o, list):
            return [norm(x) for x in o]
        return o
    return json.dumps(norm(obj), ensure_ascii=False, separators=(",", ":"))


def sign_headers(path, data):
    ts = int(time.time() * 1000)
    body = "" if data is None else _sorted_json(data)
    digest = hashlib.md5(body.encode("utf-8")).hexdigest()
    return {
        "ua-encrypt": "1",
        "timestamp": str(ts),
        "nonce": _aes_encrypt("%s_%s_%s" % (path, digest, ts)),
    }


# ==================== 本地文件 ====================
def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as exc:
        log(f"本地文件写入失败 {os.path.basename(path)}：{exc}")


def _asset_dirs():
    """返回实际参与扫描的素材目录，供日志说明。"""
    roots = []
    for p in _ASSET_CANDIDATES:
        if p and os.path.isdir(p) and p not in roots:
            roots.append(p)
    return roots or ["(无)"]


def _scan_assets():
    """收集素材：ASSET_DIR/meals 与 report 子目录优先，同时兼容根目录前缀命名。"""
    meals, reports = [], []

    def add(folder, prefixes, exts, bucket):
        try:
            names = sorted(os.listdir(folder))
        except Exception:
            return
        for n in names:
            low = n.lower()
            if not low.endswith(exts):
                continue
            if prefixes and not any(low.startswith(p) for p in prefixes):
                continue
            bucket.append(os.path.join(folder, n))

    roots = []
    for p in _ASSET_CANDIDATES:
        if p and os.path.isdir(p) and p not in roots:
            roots.append(p)
    for root in roots:
        add(root, ("meal_",), (".jpg", ".jpeg", ".png"), meals)
        add(os.path.join(root, "meals"), (), (".jpg", ".jpeg", ".png"), meals)
        add(root, ("report_",), (".png", ".jpg", ".jpeg", ".pdf"), reports)
        add(os.path.join(root, "reports"), (), (".png", ".jpg", ".jpeg", ".pdf"), reports)
    return meals, reports


def _file_type(path):
    """体检报告文件类型：1=图片 2=PDF。"""
    return 2 if str(path).lower().endswith(".pdf") else 1


def pick_asset(kind, seed_text):
    """按 seed 轮换挑一张素材；kind 取 meals / reports。"""
    meals, reports = _scan_assets()
    pool = sorted(set(meals if kind == "meals" else reports))
    if not pool:
        return None
    idx = int(hashlib.md5(seed_text.encode("utf-8")).hexdigest()[:8], 16) % len(pool)
    return pool[idx]


# ==================== 素材自举（下载餐食图 / 绘制体检报告） ====================
# 仓库不含任何图片：首次运行或 `--bootstrap` 时，从 Wikimedia Commons 下载餐食实拍图，
# 并用 PIL 现场绘制体检报告；同时写 SOURCES.md 记录图片许可与作者。
_ASSET_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) lyb-assets/1.0"}
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# 覆盖早/午/晚，便于按日期轮换
MEAL_QUERIES = [
    ("breakfast", "breakfast plate eggs toast"),
    ("breakfast", "congee youtiao chinese breakfast"),
    ("lunch", "chinese lunch rice dishes"),
    ("lunch", "bento box lunch"),
    ("dinner", "dinner plate meat vegetables"),
    ("dinner", "noodles bowl soup"),
]

FONT_CANDIDATES = [
    "https://cdn.jsdelivr.net/gh/googlefonts/noto-cjk@main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
    "https://mirrors.tuna.tsinghua.edu.cn/github-release/googlefonts/noto-cjk/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
]

HOSPITAL = "市第一人民医院"


def _noop(msg):
    pass


# ---------------------------------------------------------------- 餐食图
def _commons_search(query, limit=4):
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrnamespace": 6, "gsrsearch": "filetype:bitmap " + query,
        "gsrlimit": limit, "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1280,
    }
    r = requests.get(COMMONS_API, params=params, headers=_ASSET_UA, timeout=30)
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages") or {}
    out = []
    for p in pages.values():
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        meta = info.get("extmetadata") or {}
        out.append({
            "url": url,
            "width": info.get("width"),
            "height": info.get("height"),
            "title": p.get("title", ""),
            "license": (meta.get("LicenseShortName") or {}).get("value", ""),
            "artist": (meta.get("Artist") or {}).get("value", ""),
        })
    return out


def fetch_meals(target, want=6, force=False, log=None):
    log = log or _noop
    if requests is None:
        log("⚠️ 缺少 requests，无法下载餐食图：pip install requests")
        return []
    folder = os.path.join(target, "meals")
    os.makedirs(folder, exist_ok=True)
    existing = [n for n in os.listdir(folder) if n.lower().endswith((".jpg", ".jpeg", ".png"))]
    if existing and not force and len(existing) >= want:
        log("餐食图已存在 %d 张，跳过下载" % len(existing))
        return []

    index, n = [], 0
    for slot, query in MEAL_QUERIES:
        if n >= want:
            break
        try:
            cands = _commons_search(query)
        except Exception as exc:
            log("  搜索失败 %s: %s" % (query, exc))
            continue
        for cand in cands:
            if n >= want:
                break
            w = cand.get("width") or 0
            if w and w < 500:
                continue
            n += 1
            name = "meal_%02d_%s.jpg" % (n, slot)
            path = os.path.join(folder, name)
            try:
                r = requests.get(cand["url"], headers=_ASSET_UA, timeout=40)
                if r.status_code != 200 or len(r.content) < 20000:
                    n -= 1
                    continue
                with open(path, "wb") as f:
                    f.write(r.content)
                index.append(dict(cand, file=name, bytes=len(r.content), slot=slot))
                log("  ✅ %-30s %7d B  (%s)" % (name, len(r.content), slot))
            except Exception as exc:
                n -= 1
                log("  下载失败 %s: %s" % (cand["url"][:50], exc))
            time.sleep(0.3)
    if index:
        _write_sources(target, index)
    return index


def _write_sources(target, items):
    """记录图片出处与许可，便于二次分发时合规署名。"""
    path = os.path.join(target, "SOURCES.md")
    lines = ["# 素材来源与许可", "",
             "餐食照片取自 Wikimedia Commons，按各自的许可证使用（多为 CC BY-SA / CC0 / Public Domain）。",
             "若二次分发这些图片，请保留下表出处与作者信息。", "",
             "| 文件 | 主题 | 许可 | 作者 | 来源页 |",
             "| --- | --- | --- | --- | --- |"]
    for it in items:
        artist = (it.get("artist") or "").replace("|", "/").replace("\n", " ")[:60]
        title = (it.get("title") or "").replace("|", "/")
        lines.append("| %s | %s | %s | %s | https://commons.wikimedia.org/wiki/%s |"
                     % (it.get("file", ""), it.get("slot", ""),
                        it.get("license") or "-", artist or "-",
                        title.replace(" ", "_")))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------- 体检报告
def _font(size):
    from PIL import ImageFont
    return ImageFont.truetype(_REPORT_FONT_PATH, size)


_REPORT_FONT_PATH = ""


def _barcode(d, x, y, w, h, seed):
    import random
    rnd = random.Random(seed)
    cx = x
    while cx < x + w:
        bw = rnd.choice([2, 2, 3, 4])
        if rnd.random() > 0.35:
            d.rectangle([cx, y, cx + bw, y + h], fill=(0, 0, 0))
        cx += bw + rnd.choice([2, 3])


def _qrcode_block(d, x, y, size, seed):
    import random
    rnd = random.Random(seed)
    cell = max(size // 21, 1)
    for i in range(21):
        for j in range(21):
            if rnd.random() > 0.5:
                d.rectangle([x + i * cell, y + j * cell,
                             x + (i + 1) * cell - 1, y + (j + 1) * cell - 1], fill=(0, 0, 0))
    for (ox, oy) in ((0, 0), (14, 0), (0, 14)):
        d.rectangle([x + ox * cell, y + oy * cell,
                     x + (ox + 7) * cell, y + (oy + 7) * cell], outline=(0, 0, 0), width=2)


def _draw_report(out_path, title, en_title, meta, rows, conclusion, seed):
    from PIL import Image, ImageDraw
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), (252, 252, 250))
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, 12], fill=(0, 92, 132))
    d.text((70, 46), HOSPITAL, font=_font(38), fill=(0, 70, 105))
    d.text((70, 96), "健康体检中心  HEALTH EXAMINATION CENTER", font=_font(20), fill=(110, 110, 110))
    d.text((W - 70, 52), "报告编号：TJ2026102800117", font=_font(20), fill=(60, 60, 60), anchor="ra")
    _barcode(d, W - 300, 84, 230, 34, seed)
    d.text((W - 70, 124), "2026102800117", font=_font(16), fill=(60, 60, 60), anchor="ra")

    d.text((W // 2, 190), title, font=_font(44), fill=(0, 0, 0), anchor="mm")
    d.text((W // 2, 238), en_title, font=_font(19), fill=(120, 120, 120), anchor="mm")
    d.line([(70, 272), (W - 70, 272)], fill=(0, 92, 132), width=3)

    y = 296
    for line in meta:
        x = 80
        for cell in line:
            d.text((x, y), cell, font=_font(23), fill=(35, 35, 35))
            x += 390
        y += 40

    y += 18
    heads = [("序号", 80), ("检验项目", 150), ("结果", 520), ("标志", 650), ("单位", 720), ("参考区间", 850)]
    d.rectangle([70, y, W - 70, y + 46], fill=(226, 238, 244))
    for name, x in heads:
        d.text((x + 8, y + 11), name, font=_font(23), fill=(0, 60, 90))
    y += 46
    for i, (item, val, unit, ref, flag) in enumerate(rows, 1):
        if i % 2 == 0:
            d.rectangle([70, y, W - 70, y + 44], fill=(246, 249, 251))
        d.text((88, y + 10), str(i), font=_font(21), fill=(60, 60, 60))
        d.text((158, y + 10), item, font=_font(22), fill=(20, 20, 20))
        color = (190, 30, 30) if flag else (20, 20, 20)
        d.text((528, y + 10), val, font=_font(22), fill=color)
        if flag:
            d.text((658, y + 10), "↑" if flag == "H" else "↓", font=_font(22), fill=color)
        d.text((728, y + 10), unit, font=_font(21), fill=(80, 80, 80))
        d.text((858, y + 10), ref, font=_font(21), fill=(80, 80, 80))
        y += 44
    d.rectangle([70, y - 44 * len(rows) - 46, W - 70, y], outline=(160, 180, 190), width=1)

    y += 26
    d.text((80, y), "检查结论 / 建议：", font=_font(26), fill=(0, 70, 105))
    y += 44
    for line in conclusion:
        d.text((100, y), line, font=_font(22), fill=(45, 45, 45))
        y += 36

    y += 40
    d.text((80, y), "检验医师：李静", font=_font(22), fill=(40, 40, 40))
    d.text((360, y), "审核医师：王强", font=_font(22), fill=(40, 40, 40))
    d.text((640, y), "报告日期：2026-10-28", font=_font(22), fill=(40, 40, 40))

    cx, cy, r = W - 210, y + 40, 92
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(200, 55, 55), width=4)
    d.text((cx, cy - 26), "市第一人民医院", font=_font(19), fill=(200, 55, 55), anchor="mm")
    d.text((cx, cy + 4), "体检专用章", font=_font(19), fill=(200, 55, 55), anchor="mm")
    d.text((cx, cy + 34), "2026.10.28", font=_font(15), fill=(200, 55, 55), anchor="mm")

    _qrcode_block(d, 90, H - 190, 126, seed + 7)
    d.text((236, H - 176), "扫码查看电子报告", font=_font(19), fill=(90, 90, 90))
    d.text((236, H - 146), "本报告仅对本次检查项目负责", font=_font(17), fill=(130, 130, 130))
    d.text((236, H - 120), "如有疑问请咨询体检中心 0551-8888xxxx", font=_font(17), fill=(130, 130, 130))

    img.save(out_path, "PNG")


def _ensure_font(target, log=None):
    """下载中文字体（约 16MB）；失败返回空串，报告改用英文标题。"""
    log = log or _noop
    global _REPORT_FONT_PATH
    path = os.path.join(target, "_font.otf")
    # 复用已有字体，避免重复下载 16MB
    for cand in (path, os.path.join(target, "sc.otf"), "/ql/data/scripts/tpylyb_assets/sc.otf"):
        if os.path.exists(cand) and os.path.getsize(cand) > 1_000_000:
            _REPORT_FONT_PATH = cand
            log("  复用已有字体 %s" % cand)
            return cand
    for url in FONT_CANDIDATES:
        try:
            log("  下载中文字体 …（约 16MB，仅首次）")
            r = requests.get(url, headers=_ASSET_UA, timeout=180)
            if r.status_code == 200 and len(r.content) > 1_000_000:
                with open(path, "wb") as f:
                    f.write(r.content)
                _REPORT_FONT_PATH = path
                log("  ✅ 字体就绪 %d B" % len(r.content))
                return path
        except Exception as exc:
            log("  字体下载失败：%s" % exc)
    log("  ⚠️ 未获取中文字体，报告将使用默认字体（中文可能显示为方框）")
    return ""


CBC = [("白细胞计数 WBC", "6.24", "10^9/L", "3.50-9.50", ""),
       ("红细胞计数 RBC", "4.86", "10^12/L", "4.30-5.80", ""),
       ("血红蛋白 HGB", "149", "g/L", "130-175", ""),
       ("红细胞压积 HCT", "44.8", "%", "40.0-50.0", ""),
       ("平均红细胞体积 MCV", "92.2", "fL", "82.0-100.0", ""),
       ("平均血红蛋白量 MCH", "30.7", "pg", "27.0-34.0", ""),
       ("血小板计数 PLT", "231", "10^9/L", "125-350", ""),
       ("中性粒细胞百分比 NEUT%", "58.6", "%", "40.0-75.0", ""),
       ("淋巴细胞百分比 LYM%", "30.8", "%", "20.0-50.0", ""),
       ("单核细胞百分比 MONO%", "7.4", "%", "3.0-10.0", ""),
       ("嗜酸性粒细胞百分比 EO%", "2.6", "%", "0.4-8.0", ""),
       ("C反应蛋白 CRP", "2.14", "mg/L", "0.00-10.00", "")]

BIO = [("谷丙转氨酶 ALT", "26", "U/L", "9-50", ""),
       ("谷草转氨酶 AST", "22", "U/L", "15-40", ""),
       ("谷草/谷丙比值 AST/ALT", "0.85", "", "0.60-1.60", ""),
       ("总胆红素 TBIL", "14.2", "umol/L", "3.4-20.5", ""),
       ("直接胆红素 DBIL", "3.6", "umol/L", "0.0-6.8", ""),
       ("总蛋白 TP", "72.4", "g/L", "65.0-85.0", ""),
       ("白蛋白 ALB", "45.6", "g/L", "40.0-55.0", ""),
       ("总胆固醇 TC", "4.28", "mmol/L", "2.80-5.20", ""),
       ("甘油三酯 TG", "1.86", "mmol/L", "0.45-1.70", "H"),
       ("高密度脂蛋白 HDL-C", "1.28", "mmol/L", "1.00-1.90", ""),
       ("低密度脂蛋白 LDL-C", "2.51", "mmol/L", "0.00-3.40", ""),
       ("空腹血糖 GLU", "5.12", "mmol/L", "3.90-6.10", ""),
       ("肌酐 CREA", "78", "umol/L", "57-97", ""),
       ("尿酸 UA", "342", "umol/L", "208-428", ""),
       ("尿素氮 BUN", "5.42", "mmol/L", "3.10-8.00", "")]

URINE = [("尿颜色 URO", "淡黄色", "", "淡黄色", ""),
         ("尿透明度", "清晰", "", "清晰", ""),
         ("尿比重 SG", "1.020", "", "1.003-1.030", ""),
         ("尿酸碱度 PH", "6.0", "", "4.5-8.0", ""),
         ("尿蛋白 PRO", "阴性", "", "阴性", ""),
         ("尿糖 GLU", "阴性", "", "阴性", ""),
         ("尿潜血 BLD", "阴性", "", "阴性", ""),
         ("尿酮体 KET", "阴性", "", "阴性", ""),
         ("尿胆原 URO", "正常", "", "正常", ""),
         ("尿白细胞 LEU", "阴性", "", "阴性", ""),
         ("尿亚硝酸盐 NIT", "阴性", "", "阴性", ""),
         ("尿沉渣红细胞", "1.2", "/HP", "0.0-3.0", "")]


def make_reports(target, force=False, log=None):
    log = log or _noop
    folder = os.path.join(target, "reports")
    os.makedirs(folder, exist_ok=True)
    existing = [n for n in os.listdir(folder) if n.lower().endswith((".png", ".jpg", ".pdf"))]
    if existing and not force:
        log("体检报告已存在 %d 份，跳过生成" % len(existing))
        return []
    try:
        import PIL  # noqa: F401
    except ImportError:
        log("⚠️ 缺少 Pillow，跳过体检报告生成：pip install pillow")
        return []

    if not _ensure_font(target, log):
        log("⚠️ 无中文字体，报告标题改用英文（表格中文会显示异常）")

    meta = [("姓　名：张伟", "性　别：男", "年　龄：38 岁"),
            ("体检编号：TJ2026102800117", "体检日期：2026-10-26", "报告日期：2026-10-28"),
            ("科　室：体检中心", "标本类型：静脉血", "床　号：—")]
    plan = [
        ("report_cbc.png", "健康体检报告（血常规）", "Blood Routine Examination", CBC,
         ["1. 血常规各项指标均在参考区间内，未见明显异常。",
          "2. 建议保持规律作息，均衡饮食，适量运动。"], 11),
        ("report_bio.png", "健康体检报告（生化全套）", "Biochemistry Profile", BIO,
         ["1. 甘油三酯轻度升高，建议低脂饮食、限制饮酒、增加有氧运动。",
          "2. 肝肾功能、血糖、血脂其余项目未见明显异常，建议 3-6 个月复查血脂。"], 23),
        ("report_urine.png", "健康体检报告（尿常规）", "Urine Routine Examination", URINE,
         ["1. 尿常规各项指标未见明显异常。",
          "2. 建议多饮水，注意个人卫生，每年复查一次。"], 37),
    ]
    made = []
    for name, title, en, rows, conc, seed in plan:
        path = os.path.join(folder, name)
        try:
            _draw_report(path, title, en, meta, rows, conc, seed)
            made.append(name)
            log("  ✅ %-20s %7d B" % (name, os.path.getsize(path)))
        except Exception as exc:
            log("  ❌ %s 生成失败：%s" % (name, exc))
    return made


# ---------------------------------------------------------------- 入口
def ensure_assets(target=None, want_meals=12, meals_only=False, force=False, log=None):
    """生成素材到 target；返回 {'meals': [...], 'reports': [...]}。"""
    log = log or _noop
    # 默认写到主脚本实际使用的素材目录，避免 --bootstrap 与定时任务落到两处
    target = os.path.abspath(target or ASSET_DIR)
    os.makedirs(target, exist_ok=True)
    log("素材目录：%s" % target)
    meals = fetch_meals(target, want=want_meals, force=force, log=log)
    reports = [] if meals_only else make_reports(target, force=force, log=log)
    return {"meals": meals, "reports": reports}

def _assets_ready():
    """餐食图与体检报告是否都已就位。"""
    meals, reports = _scan_assets()
    return bool(meals) and bool(reports)


def _bootstrap_assets():
    """素材缺失时自动下载餐食图 + 绘制体检报告（仅首次，合并后直接调用）。"""
    if os.getenv("TPYLYB_AUTO_ASSETS", "1") in ("0", "false", "False"):
        log("素材缺失，但 TPYLYB_AUTO_ASSETS=0 已关闭自动初始化，相关任务将跳过")
        return False
    log(f"ℹ️ 首次运行：未检测到素材，开始自动初始化 → {ASSET_DIR}（约 1-3 分钟）")
    try:
        res = ensure_assets(ASSET_DIR, log=log)
    except Exception as exc:
        log(f"⚠️ 素材初始化失败：{exc}（相关任务将跳过，可稍后手动重试）")
        return False
    log(f"✅ 素材初始化完成：餐食 {len(res.get('meals') or [])} 张、"
        f"体检报告 {len(res.get('reports') or [])} 份")
    return _assets_ready()


def _meal_stats(state_root):
    """全局餐食图通过率统计：{文件名: {"success": n, "fail": n}}。"""
    return state_root.setdefault("asset_stats", {}).setdefault("meals", {})


def _bump_stat(stats, name, ok):
    item = stats.setdefault(name, {})
    key = "success" if ok else "fail"
    item[key] = int(item.get(key, 0)) + 1
    item["updated"] = int(time.time())


def _rank_meals(stats, seed_text):
    """按通过率排序：零失败优先 → 成功次数多优先 → 同档按日期哈希轮换。"""
    meals, _ = _scan_assets()
    pool = sorted(set(meals))

    def key(path):
        name = os.path.basename(path)
        item = stats.get(name) or {}
        fail = int(item.get("fail", 0))
        success = int(item.get("success", 0))
        return (fail, -success,
                hashlib.md5((seed_text + name).encode("utf-8")).hexdigest())

    return sorted(pool, key=key)


def _stat_text(stats, name):
    item = stats.get(name) or {}
    if not item:
        return "新图"
    return "成功%s/失败%s" % (item.get("success", 0), item.get("fail", 0))


# ==================== 账号 ====================
def parse_accounts():
    try:
        accounts = yyb.load_accounts(ENV_NAME)
    except Exception as e:
        log(f"❌ 拉取 yyb_go 账号失败: {e}")
        return []

    result = []
    for idx, acc in enumerate(accounts or [], 1):
        identifier = str(acc.get("openid") or acc.get("id") or acc.get("wxid") or "").strip()
        if not identifier:
            continue
        remark = acc.get("remark") or acc.get("nickname") or acc.get("alias") or f"账号{idx}"
        result.append({
            "identifier": identifier,
            "remark": remark,
            "login_type": yyb.normalize_login_type(acc.get("login_type")),
            "protocol": yyb.login_type_label(acc.get("login_type")),
        })

    if result:
        log(f"✅ 从 yyb_go 同步到 {len(result)} 个存活账号（服务地址: {SERVER_URL}）")
    else:
        log(f"❌ 未获取到存活账号，请确认 yyb_go 已启动且有在线账号（服务地址: {SERVER_URL}）")
    return result


# ==================== 客户端 ====================
class TpylybClient:
    """健康分 2.0 客户端：/asset-api/** + 防篡改签名头。"""

    def __init__(self, account):
        self.identifier = account["identifier"]
        self.remark = account["remark"]
        self.protocol = account["protocol"]
        self.login_type = account.get("login_type", "")
        self.openid = ""
        self.unionid = ""
        self.token = ""
        self.score = None
        self.bean = None
        self.session = requests.Session()
        self.headers = {
            "Host": "lyb-api.cpic.com.cn",
            "Connection": "keep-alive",
            "xweb_xhr": "1",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) "
                           "NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF "
                           "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254181d) XWEB/19841"),
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": REFERER,
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        cached = yyb.get_cached_token(CACHE_NAME, self.identifier, {"max_age_ms": 6 * 3600 * 1000})
        if not (cached and cached.get("token")):
            cached = self._legacy_cache()
        if cached:
            self.openid = cached.get("openid", "")
            self.unionid = cached.get("unionid", "")
            self.token = cached.get("token", "")

    # ---------- 缓存 ----------
    def _legacy_cache(self):
        data = _load_json(_CK_FILE_LEGACY, {})
        for key in (f"yyb:{self.identifier}", f"niuzi:{self.identifier}", self.identifier):
            item = data.get(key)
            if isinstance(item, dict) and item.get("token"):
                return item
        return None

    def _save_cache(self):
        yyb.save_cached_token(CACHE_NAME, self.identifier, {
            "openid": self.openid,
            "unionid": self.unionid,
            "token": self.token,
        })

    def _clear_cache(self):
        yyb.remove_cached_token(CACHE_NAME, self.identifier)

    # ---------- 请求 ----------
    def _headers(self, signed_path=None, data=None):
        h = dict(self.headers)
        if self.openid:
            h["lyb-m-openid"] = self.openid
        if self.unionid:
            h["lyb-m-unionid"] = self.unionid
        if self.token:
            h["lyb-m-token"] = self.token
        else:
            h.pop("lyb-m-token", None)
        if signed_path:
            h.update(sign_headers(signed_path, data))
        return h

    def _post(self, path, data=None, prefix="/asset-api", signed_path=None):
        body = data if data is not None else {}
        headers = self._headers(signed_path if signed_path is not None else (path if prefix == "/asset-api" else None),
                                body)
        resp = self.session.post(BASE_URL + prefix + path, headers=headers, json=body, timeout=20)
        token_header = resp.headers.get("lyb-m-token")
        if token_header:
            self.token = token_header
        try:
            raw = resp.json()
        except Exception:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")

        code = raw.get("code")
        payload = raw.get("data")
        if isinstance(payload, str) and payload:
            try:
                payload = json.loads(_aes_decrypt(payload))
            except Exception as exc:
                payload = {"_decrypt_error": str(exc)}
        if code != 0:
            return code, {"msg": raw.get("msg") or raw.get("message") or f"code={code}"}
        return code, payload

    def _upload(self, path, file_path, form, signed_path, sign_body):
        headers = self._headers(signed_path, sign_body)
        headers.pop("Content-Type", None)
        ext = os.path.splitext(file_path)[1].lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".pdf": "application/pdf"}.get(ext, "application/octet-stream")
        name = "%d_%06d%s" % (int(time.time() * 1000), random.randint(0, 999999), ext or ".png")
        with open(file_path, "rb") as fh:
            resp = self.session.post(BASE_URL + path, headers=headers, data=form,
                                     files={"file": (name, fh, mime)}, timeout=90)
        token_header = resp.headers.get("lyb-m-token")
        if token_header:
            self.token = token_header
        try:
            raw = resp.json()
        except Exception:
            raise RuntimeError(f"上传返回异常 HTTP {resp.status_code}: {resp.text[:120]}")
        payload = raw.get("data")
        if isinstance(payload, str) and payload:
            try:
                payload = json.loads(_aes_decrypt(payload))
            except Exception:
                payload = {"fileCode": payload}
        if raw.get("code") != 0:
            raise RuntimeError(raw.get("msg") or f"上传失败 code={raw.get('code')}")
        return payload or {}

    # ---------- 登录 ----------
    def login(self):
        code = yyb.get_single_code(WX_APPID, self.identifier)
        if not code:
            raise RuntimeError("获取微信 code 失败")
        log(f"[{self.remark}] 获取微信 code 成功: {code[:8]}...")
        headers = {"content-type": "application/json", "Referer": REFERER,
                   "User-Agent": self.headers["User-Agent"]}
        resp = self.session.post(BASE_URL + "/lyb-api/login/wx-session",
                                 headers=headers, json={"code": code}, timeout=20)
        token_header = resp.headers.get("lyb-m-token")
        try:
            body = resp.json()
        except Exception:
            raise RuntimeError(f"/wx-session 返回异常: HTTP {resp.status_code}")
        if body.get("code") != 0:
            raise RuntimeError(f"/wx-session 失败: {body.get('msg') or body}")
        data = body.get("data") or {}
        self.openid = data.get("openid") or ""
        self.unionid = data.get("unionid") or ""
        self.token = token_header or ""
        if not self.openid:
            raise RuntimeError("/wx-session 未返回 openid")
        if not self.token:
            raise RuntimeError("/wx-session 未返回 lyb-m-token")
        self._save_cache()
        log(f"[{self.remark}] 登录成功，token: {self.token[:8]}...")

    def ensure_login(self):
        if self.token and self.openid:
            code, _ = self._post("/user-asset/get-growth-info-V1", {})
            if code == 0:
                log(f"[{self.remark}] 命中 token 缓存，跳过取码登录")
                return
            log(f"[{self.remark}] token 失效(code={code})，尝试重新取码登录")
            backup = (self.token, self.openid, self.unionid)
            self._clear_cache()
            try:
                self.login()
                return
            except Exception as exc:
                self.token, self.openid, self.unionid = backup
                log(f"[{self.remark}] 取码登录失败（{exc}），回退旧 token 继续尝试")
                return
        self.login()

    # ---------- 基础 ----------
    def growth(self):
        code, data = self._post("/user-asset/get-growth-info-V1", {})
        if code != 0:
            raise RuntimeError(f"获取健康分失败: {data.get('msg')}")
        self.score = data.get("currentHealthScore")
        return data

    def blue_bean(self):
        code, data = self._post("/blue-bean/get-balance-V1", {})
        if code == 0:
            self.bean = data.get("currentBlueBean")
        return self.bean

    def task_list(self, query_type=1):
        code, data = self._post("/task/list-V1", {"queryType": query_type})
        if code != 0:
            raise RuntimeError(f"获取任务列表失败: {data.get('msg')}")
        return data.get("healthTaskList") or []

    # ---- 早起打卡 ----
    def zqdk_home(self):
        code, data = self._post("/task/zqdk/homeInit-V1", {"taskCode": "ZQDK"})
        return data if code == 0 else {"_error": data.get("msg")}

    def zqdk_sign(self):
        code, data = self._post("/task/zqdk/dk-V1", {"taskCode": "ZQDK"})
        if code == 0:
            return True, data
        return False, data.get("msg") or f"code={code}"

    # ---- 健康问答 ----
    def jkwd_question(self):
        code, data = self._post("/task/jkwd/getQuestion-V1", {"taskCode": "JKWD"})
        if code != 0:
            return None, data.get("msg") or f"code={code}"
        return data, None

    def jkwd_answer(self, question_id, select_value):
        code, data = self._post("/task/jkwd/getQuestionAnswer-V1",
                                {"questionId": question_id, "selectValue": select_value})
        if code != 0:
            return None, data.get("msg") or f"code={code}"
        return data, None

    def jkwd_revive(self, answer_id):
        code, data = self._post("/task/jkwd/revive-V1", {"answerId": str(answer_id)})
        if code != 0:
            return False, data.get("msg") or f"code={code}"
        return data.get("reviveResult") == "Y", data.get("reviveFailReason") or ""

    # ---- 协议 ----
    def agreement(self, agreement_type):
        code, data = self._post("/agreement/get-status-V1", {"agreementType": agreement_type})
        return data if code == 0 else {}

    def consent(self, agreement_type, version):
        return self._post("/agreement/consent-V1",
                          {"agreementType": agreement_type, "agreementVersion": version})

    # ---- AI 三餐 ----
    def meal_status(self):
        code, data = self._post("/task/ai/ai-meal/home-status-V1", {})
        return data if code == 0 else {}

    def meal_submit(self, file_path, meal_type):
        return self._upload("/asset-api/task/ai/ai-meal/submit-V1", file_path,
                            {"mealType": meal_type},
                            "/task/ai/ai-meal/submit-V1", {"mealType": meal_type})

    def meal_result(self, record_id):
        code, data = self._post("/task/ai/ai-meal/result-V1", {"recordId": str(record_id)})
        return data if code == 0 else {}

    # ---- AI 体检 ----
    def report_quota(self):
        code, data = self._post("/task/ai/ai-health-report/get-monthly-quota-V1", {},
                                signed_path="ai-health-report/get-monthly-quota-V1")
        return data if code == 0 else {}

    def report_upload(self, file_path, file_type=1):
        return self._upload("/asset-api/task/ai/ai-health-report/upload-V1", file_path,
                            {"source": "AI_HEALTH_REPORT", "fileType": str(file_type)},
                            "ai-health-report/upload-V1",
                            {"source": "AI_HEALTH_REPORT", "fileType": str(file_type)})

    def report_submit(self, file_code, file_type=1):
        code, data = self._post("/task/ai/ai-health-report/submit-V1",
                                {"fileType": file_type, "fileCodes": [file_code]},
                                signed_path="ai-report/submit-V1")
        if code != 0:
            raise RuntimeError(data.get("msg") or f"提交失败 code={code}")
        return data

    def report_history(self):
        code, data = self._post("/task/ai/ai-health-report/list-history-V1", {},
                                signed_path="ai-health-report/list-history-V1")
        return data.get("list") or [] if code == 0 else []

    # ---- AI 运动 ----
    def sport_list(self):
        code, data = self._post("/task/ai/sport/list", {},
                                signed_path="/task/ai/sport/list")
        return data if code == 0 else {}

    @staticmethod
    def _enc_payload(obj):
        """小程序用 JSON.stringify(obj)（无空格）再 AES，保持一致。"""
        return {"encodeString": _aes_encrypt(
            json.dumps(obj, ensure_ascii=False, separators=(",", ":")))}

    def sport_start(self, sport_code, session_id, start_ms):
        body = self._enc_payload({"sportCode": sport_code, "startTime": start_ms,
                                  "sessionId": session_id})
        code, data = self._post("/task/ai/sport/start", body,
                                signed_path="/task/ai/sport/start")
        if code != 0:
            raise RuntimeError(data.get("msg") or f"start 失败 code={code}")
        return data

    def sport_finish(self, sport_code, session_id, start_ms, end_ms, actual_count):
        body = self._enc_payload({"sportCode": sport_code, "startTime": start_ms,
                                  "sessionId": session_id, "bizStage": "2",
                                  "finishType": "1", "endTime": end_ms,
                                  "errorCount": 0, "actualCount": actual_count,
                                  "duration": None})
        code, data = self._post("/task/ai/sport/finish", body,
                                signed_path="/task/ai/sport/finish")
        if code != 0:
            raise RuntimeError(data.get("msg") or f"finish 失败 code={code}")
        return data

    def do_sport(self, tasks):
        if not SPORT_ENABLED:
            return
        target = next((t for t in tasks if t.get("taskCode") == "AI_SPORT"), None)
        if target is None:
            log(f"[{self.remark}] AI 带你运动：今日已完成")
            return
        if target.get("taskStatus") not in (1, None):
            log(f"[{self.remark}] AI 带你运动：{target.get('buttonText') or '已完成'}")
            return

        agree = self.agreement("AI_SPORT")
        if agree.get("needConsent") == 1:
            self.consent("AI_SPORT", agree.get("latestVersion") or "1.0")

        sport_code, target_count = SPORT_CODE, 20
        try:
            info = self.sport_list()
            for group in info.get("categoryGroups") or []:
                for item in group.get("sportList") or []:
                    if item.get("sportCode") == SPORT_CODE:
                        target_count = int(item.get("targetCount") or target_count)
        except Exception:
            pass

        session_id = str(uuid.uuid4())
        start_ms = int(time.time() * 1000)
        try:
            started = self.sport_start(sport_code, session_id, start_ms)
        except Exception as exc:
            log(f"[{self.remark}] AI 带你运动：start 失败 {exc}")
            return
        log(f"[{self.remark}] AI 带你运动：会话已建立 {sport_code} 目标 {target_count} 次"
            f"（recordId={started.get('recordId')}），练习 {SPORT_WAIT}s 后提交…")

        time.sleep(SPORT_WAIT)

        end_ms = int(time.time() * 1000)
        try:
            res = self.sport_finish(sport_code, session_id, start_ms, end_ms, target_count)
        except Exception as exc:
            log(f"[{self.remark}] AI 带你运动：finish 失败 {exc}")
            return

        if res.get("qualifiedFlag"):
            log(f"[{self.remark}] AI 带你运动成功 ✅ +{res.get('healthScore') or 2}分"
                f"/+{res.get('blueBeans') or 20}豆（首次获得：{res.get('firstTodayFinish')}）")
        else:
            log(f"[{self.remark}] AI 带你运动：本次判定不合格 {res}")

    def report_result(self, task_biz_no):
        code, data = self._post("/task/ai/ai-health-report/get-interpret-result-V1",
                                {"taskBizNo": task_biz_no},
                                signed_path="ai-health-report/get-interpret-result-V1")
        return data if code == 0 else {}

    # ---------- 任务编排 ----------
    def _answer_pick(self, bank, question):
        text = (question.get("questionText") or "").strip()
        options = question.get("options") or []
        keys = [o.get("optionKey") for o in options]
        item = bank.get(text)
        if item and item.get("answer") in keys:
            return item["answer"], "错题本"
        if not keys:
            return None, ""
        best = max(options, key=lambda o: len(str(o.get("optionValue") or "")))
        return best.get("optionKey"), "最长选项"

    def do_jkwd(self, bank):
        bank_changed = False
        for attempt in range(1, 5):
            question, err = self.jkwd_question()
            if err:
                log(f"[{self.remark}] 健康问答取题失败：{err}")
                return bank_changed, False
            if (question.get("historyAnswerResult") or "") == "Y":
                log(f"[{self.remark}] 健康问答：今日已完成")
                return bank_changed, True

            qid = question.get("questionId")
            text = (question.get("questionText") or "").strip()
            if not qid:
                log(f"[{self.remark}] 健康问答：未返回 questionId")
                return bank_changed, False

            key, source = self._answer_pick(bank, question)
            if not key:
                log(f"[{self.remark}] 健康问答：题目无选项")
                return bank_changed, False

            result, err = self.jkwd_answer(qid, key)
            if err:
                log(f"[{self.remark}] 健康问答作答失败：{err}")
                return bank_changed, False

            if (result.get("historyAnswerResult") or "") == "Y":
                bank[text] = {"answer": key, "updated": int(time.time())}
                bank_changed = True
                gain = str(result.get("answerScoreResultDesc") or "+1").lstrip("+")
                beans = str(result.get("answerBeanResultDesc") or "+10").lstrip("+")
                log(f"[{self.remark}] 健康问答答对 ✅ {question.get('questionText', text)}"
                    f"（{source}→{key}，+{gain}分/+{beans}豆）")
                return bank_changed, True

            right = (result.get("answerResultDesc") or "").strip()
            bank[text] = {"answer": right, "updated": int(time.time())}
            bank_changed = True
            log(f"[{self.remark}] 健康问答答错 ❌ {text}｜我选 {key}，正确答案 {right}"
                f"（第 {attempt} 轮，已记入错题本）")

            if not result.get("showReviveButton"):
                log(f"[{self.remark}] 无复活入口，今日问答结束")
                return bank_changed, False
            ok, reason = self.jkwd_revive(result.get("answerId"))
            if not ok:
                log(f"[{self.remark}] 分享复活失败：{reason}")
                return bank_changed, False
            log(f"[{self.remark}] 分享复活成功，换一题继续")
            time.sleep(random.uniform(1.0, 2.0))
        log(f"[{self.remark}] 健康问答：4 轮未答对，今日放弃")
        return bank_changed, False

    def do_zqdk(self, tasks):
        now_hour = datetime.now().hour
        target = next((t for t in tasks if t.get("taskCode") == "ZQDK"), None)
        if target is None:
            log(f"[{self.remark}] 早起打卡：今日任务已消失（视为已完成）")
            return True
        if target.get("taskStatus") not in (1, None):
            log(f"[{self.remark}] 早起打卡：{target.get('buttonText') or '已完成'}")
            return True

        home = self.zqdk_home()
        today = next((r for r in (home.get("records") or []) if r.get("todayFlag") == "1"), None)
        if today and today.get("taskStatus") != 2:
            log(f"[{self.remark}] 早起打卡：今日已打卡")
            return True
        start, end = home.get("dkStartTime") or "06:00", home.get("dkEndTime") or "09:00"
        log(f"[{self.remark}] 早起打卡窗口 {start}-{end}（当前 {datetime.now():%H:%M}）")
        if not (ZQDK_WINDOW[0] <= now_hour < ZQDK_WINDOW[1]):
            log(f"[{self.remark}] 早起打卡：不在打卡时段，跳过")
            return False

        ok, data = self.zqdk_sign()
        if ok:
            log(f"[{self.remark}] 早起打卡成功 ✅ +{data.get('taskScoreReward') or 1}分"
                f"/+{data.get('taskBeanReward') or 10}豆")
            return True
        log(f"[{self.remark}] 早起打卡失败：{data}")
        return False

    def _meal_type(self):
        hour = datetime.now().hour
        if hour < 10:
            return "BREAKFAST", "早餐"
        if hour < 15:
            return "LUNCH", "午餐"
        return "DINNER", "晚餐"

    def do_meal(self, tasks, state_today, state_root):
        """AI 三餐：按图片通过率择优，当日失败自动换图（最多 MEAL_MAX_TRY 张）。"""
        if not MEAL_ENABLED:
            return
        target = next((t for t in tasks if t.get("taskCode") == "AI_MEAL_REPORT"), None)
        if target is None:
            log(f"[{self.remark}] AI 识别三餐：今日已完成")
            return
        if target.get("taskStatus") not in (1, None):
            log(f"[{self.remark}] AI 识别三餐：{target.get('buttonText') or '已完成'}")
            return

        today = datetime.now().strftime("%Y-%m-%d")
        if state_today.get("meal_date") == today:
            log(f"[{self.remark}] AI 识别三餐：本日已上传成功，跳过")
            return

        stats = _meal_stats(state_root)
        if state_today.get("meal_tried_date") != today:
            state_today["meal_tried_date"] = today
            state_today["meal_tried"] = []
        tried = state_today.setdefault("meal_tried", [])

        ranked = _rank_meals(stats, f"{self.identifier}-{today}")
        if not ranked:
            log(f"[{self.remark}] AI 识别三餐：未找到餐食素材（已扫描：{_asset_dirs()}），跳过")
            return
        queue = [p for p in ranked if os.path.basename(p) not in tried][:max(MEAL_MAX_TRY, 1)]
        if not queue:
            log(f"[{self.remark}] AI 识别三餐：今日已试 {len(tried)} 张均未通过，明日再试")
            return

        agree = self.agreement("AI_MEAL_REPORT")
        if agree.get("needConsent") == 1:
            self.consent("AI_MEAL_REPORT", agree.get("latestVersion") or "1.0")

        meal_type, label = self._meal_type()
        for attempt, img in enumerate(queue, 1):
            name = os.path.basename(img)
            try:
                res = self.meal_submit(img, meal_type)
            except Exception as exc:
                _bump_stat(stats, name, False)
                log(f"[{self.remark}] AI 识别三餐：{name} 上传失败（{exc}），换下一张")
                continue

            record_id = res.get("recordId")
            log(f"[{self.remark}] AI 识别三餐：第 {attempt}/{len(queue)} 张 {name}"
                f"（{label}｜{_stat_text(stats, name)}）已上传，状态 {res.get('status')}")
            if not record_id:
                _bump_stat(stats, name, False)
                continue

            status = res.get("status")
            detail = {}
            for _ in range(10):
                if status in ("SUCCESS", "NOT_MEAL", "FAIL", "TIMEOUT"):
                    break
                time.sleep(5)
                detail = self.meal_result(record_id)
                status = detail.get("status") or status

            if status == "SUCCESS":
                _bump_stat(stats, name, True)
                state_today["meal_date"] = today
                log(f"[{self.remark}] AI 识别三餐成功 ✅ +{detail.get('taskScoreReward') or 2}分"
                    f"/+{detail.get('taskBeanReward') or 20}豆｜第 {attempt} 张通过"
                    f"｜{detail.get('dietAdvice') or ''}")
                return

            _bump_stat(stats, name, False)
            tried.append(name)
            state_today["meal_tried"] = tried
            reason = "未识别到餐食" if status == "NOT_MEAL" else f"识别未成功({status})"
            log(f"[{self.remark}] AI 识别三餐：{name} {reason}，换下一张")
            time.sleep(random.uniform(1.0, 2.0))

        log(f"[{self.remark}] AI 识别三餐：本次 {len(queue)} 张均未通过，今日放弃")

    def do_report(self, tasks, state_today):
        target = next((t for t in tasks if t.get("taskCode") == "AI_HEALTH_REPORT"), None)
        if target is None:
            self._report_track(state_today)
            return
        if target.get("taskStatus") not in (1, None):
            log(f"[{self.remark}] AI 解读体检：{target.get('buttonText') or '已完成'}")
            self._report_track(state_today)
            return

        month = datetime.now().strftime("%Y%m")
        quota = self.report_quota()
        remain = int(quota.get("remainCount") or 0)
        used_here = int(state_today.get("report_month_count") or 0) if state_today.get("report_month") == month else 0
        if remain <= 0:
            log(f"[{self.remark}] AI 解读体检：本月配额已用完（{quota.get('usedCount')}/{quota.get('limitCount')}）")
            self._report_track(state_today)
            return
        if used_here >= REPORT_MONTHLY_MAX:
            log(f"[{self.remark}] AI 解读体检：本月本账号已提交 {used_here} 次"
                f"（上限 {REPORT_MONTHLY_MAX}），服务端剩余 {remain} 次")
            self._report_track(state_today)
            return

        # 已有报告在 AI 解读/医生审核中时不再重复提交，避免浪费月度配额
        try:
            pending = [h for h in self.report_history()
                       if h.get("reportStatus") in (2, 3)]
        except Exception:
            pending = []
        if pending:
            log(f"[{self.remark}] AI 解读体检：已有 {len(pending)} 份报告在审核中，暂不重复提交")
            self._report_track(state_today)
            return

        img = pick_asset("reports", f"{self.identifier}-{month}-{used_here}")
        if not img:
            log(f"[{self.remark}] AI 解读体检：未找到报告素材（已扫描：{_asset_dirs()}），跳过")
            return

        agree = self.agreement("AI_HEALTH_REPORT")
        if agree.get("needConsent") == 1:
            self.consent("AI_HEALTH_REPORT", agree.get("latestVersion") or "1.0")

        ftype = _file_type(img)
        try:
            up = self.report_upload(img, ftype)
            file_code = up.get("fileCode")
            if not file_code:
                raise RuntimeError(f"未返回 fileCode: {up}")
            sub = self.report_submit(file_code, ftype)
        except Exception as exc:
            log(f"[{self.remark}] AI 解读体检提交失败：{exc}")
            return

        biz = sub.get("taskBizNo") or ""
        log(f"[{self.remark}] AI 解读体检：已提交 {os.path.basename(img)}"
            f"（类型 {'PDF' if ftype == 2 else '图片'}，{biz}，"
            f"状态 {STATE_LABEL.get(sub.get('reportStatus'), sub.get('reportStatus'))}，"
            f"配额剩 {max(remain - 1, 0)}）")
        state_today["report_month"] = month
        state_today["report_month_count"] = used_here + 1
        biz_list = state_today.setdefault("report_biz", [])
        if biz and biz not in biz_list:
            biz_list.append(biz)
        self._report_track(state_today)

    def _report_track(self, state_today):
        """跟踪已提交报告的状态：2=AI解读中 3=医生审核中 4=已完成。"""
        biz_list = state_today.get("report_biz") or []
        if not biz_list:
            return
        try:
            hist = {h.get("taskBizNo"): h for h in self.report_history()}
        except Exception:
            return
        for biz in biz_list[-3:]:
            item = hist.get(biz)
            if not item:
                continue
            status = item.get("reportStatus")
            label = STATE_LABEL.get(status, "状态" + str(status))
            mark = "（已计分）" if status == 4 else ""
            print(f"[{self.remark}] 体检报告 {biz}：{label}{mark}", flush=True)

    def task_overview(self, tasks):
        """一行打印当前任务清单，便于一眼核对全量状态。"""
        parts = []
        for t in tasks:
            name = t.get("taskName") or t.get("taskCode") or "?"
            btn = t.get("buttonText") or ""
            parts.append(f"{name}[{btn}]" if btn else name)
        if parts:
            log(f"[{self.remark}] 任务清单：{' | '.join(parts)}")

    def report_others(self, tasks):
        for task in tasks:
            code_name = task.get("taskCode") or ""
            status = task.get("taskStatus")
            label = task.get("taskName") or code_name
            if code_name in ("ZQDK", "JKWD", "AI_MEAL_REPORT", "AI_HEALTH_REPORT", "AI_SPORT"):
                continue
            if status not in (1, None):
                log(f"[{self.remark}] {label}：{task.get('buttonText') or '已完成'}")
                continue
            hint = TASK_HINT.get(code_name)
            if code_name in ("LQYWBZ", "LQZJBZ"):
                code, info = self._post("/task/lqbz/checkStatus-V1", {"taskCode": code_name})
                info = info if code == 0 else {}
                log(f"[{self.remark}] {label}：需实名投保（{info.get('toastMsg') or '去投保'}）"
                    f"{info.get('insureUrl') or ''}")
            elif hint:
                log(f"[{self.remark}] {label}：{hint}")

    def run(self, bank, state_today, state_root):
        self.ensure_login()
        info = self.growth()
        self.blue_bean()
        log(f"[{self.remark}] 执行前：健康分 {self.score}"
            f"（{info.get('currentLevel')}，下一档 {info.get('nextLevelThreshold')}），蓝豆 {self.bean}")
        score_before = float(self.score or 0)
        bean_before = float(self.bean or 0)

        tasks = self.task_list(1)
        changed = False
        self.do_zqdk(tasks)
        changed |= self.do_jkwd(bank)[0]
        self.do_meal(tasks, state_today, state_root)
        self.do_sport(tasks)
        self.do_report(tasks, state_today)
        all_tasks = self.task_list(2)
        self.task_overview(all_tasks)
        self.report_others(all_tasks)

        self.growth()
        self.blue_bean()
        gain_score = float(self.score or 0) - score_before
        gain_bean = float(self.bean or 0) - bean_before
        log(f"[{self.remark}] 执行后：健康分 {self.score}，蓝豆 {self.bean}"
            f"（本次 +{gain_score:g} 分 / +{gain_bean:g} 豆）")
        return changed


def main():
    accounts = parse_accounts()
    if not accounts:
        log("未找到账号：请确认 yyb_go 已启动且有存活账号")
        return 1

    log(f"{' ' * 10}꧁༺ {APP_NAME}（健康分 2.0）༻꧂\n")
    if not _assets_ready():
        _bootstrap_assets()
    bank = _load_json(BANK_FILE, {})
    state = _load_json(STATE_FILE, {})
    refs = state.setdefault("refs", {})
    bank_dirty = False
    summaries = []
    failed = 0

    for i, acc in enumerate(accounts, 1):
        log(f"\n----------- 🎊 第 {i} 个账号 🎊 -----------")
        state_today = refs.setdefault(acc["identifier"], {})
        try:
            client = TpylybClient(acc)
            bank_dirty |= client.run(bank, state_today, state)
            summaries.append(f"账号{i}：健康分 {client.score}，蓝豆 {client.bean}")
        except Exception as exc:
            failed += 1
            log(f"[{acc['remark']}] 执行异常: {exc}")
            summaries.append(f"账号{i}：执行异常 {exc}")

    if bank_dirty:
        _save_json(BANK_FILE, bank)
    _save_json(STATE_FILE, state)

    log("\n----------- 🎊 执 行  结 束 🎊 -----------\n")
    _send_qinglong_notify(APP_NAME, "\n".join(summaries))
    return 0 if failed < len(accounts) else 1


def bootstrap_main(argv):
    """命令行素材初始化：python3 tpylyb.py --bootstrap [--force] [--meals-only] [目录]"""
    target = None
    force = meals_only = False
    for arg in argv:
        if arg == "--force":
            force = True
        elif arg == "--meals-only":
            meals_only = True
        elif not arg.startswith("-"):
            target = arg
    log(f"{APP_NAME} · 素材初始化")
    target = os.path.abspath(target or ASSET_DIR)
    res = ensure_assets(target, force=force, meals_only=meals_only, log=log)
    meals, reports = _scan_assets()
    log("完成：素材目录 %s" % target)
    log("      餐食图 %d 张、体检报告 %d 份（本次新增 %d / %d）"
        % (len(set(meals)), len(set(reports)), len(res["meals"]), len(res["reports"])))
    return 0


if __name__ == "__main__":
    _args = sys.argv[1:]
    if "--bootstrap" in _args:
        sys.exit(bootstrap_main([a for a in _args if a != "--bootstrap"]))
    sys.exit(main())
