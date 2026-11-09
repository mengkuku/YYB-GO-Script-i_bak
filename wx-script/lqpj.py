#!/usr/bin/env python3
# name: 漓泉啤酒生态营地
# cron: 30 9 * * *
# YYB-Go-Enhanced 适配说明：配置多行 YYB_SERVER=地址@账号标识；通知使用青龙 notify.py。
# -*- coding: utf-8 -*-
"""
name: 漓泉啤酒生态营地
cron: 30 9 * * *
"""
# 漓泉啤酒生态营地 - 每日签到得积分
# 入口: 微信小程序「漓泉啤酒生态营地」-> 会员中心 -> 每日签到
# 说明: 通过 yyb_go 自动获取存活账号并换取 wx.login code, 复刻小程序的
#       会员静默登录 (mbr/members/wxLogin/{code}) 拿到 Token, 再完成每日签到。
# 接口契约 (来自小程序主包 common/vendor.js 反编译):
#   登录  GET  /api/mbr/members/wxLogin/{code}?appId=<appid>
#            -> errcode==0 且 data.b2cMemberId/openid 存在; data.token 为会话凭证
#   鉴权  请求头 Token: <token>   (响应拦截器: errcode||code, 200 成功, 401 会话失效)
#   状态  GET  /api/b2c/member/sign/task/list  -> data.signRes.{sign, signNum,
#            taskSignDtoList[{signTime, signStatus}]}   sign==true / 今日行
#            signStatus=="sign" 即今日已签 (幂等预检依据)
#   签到  POST /api/b2c/member/sign/task  body {}  -> code==200, data.signRes.sign==true
#   积分  GET  /api/b2c/member/pointsAndCouponCardNumAndShopCardInfo?unionId=<unionId>
#            -> data.MemberCouponShopPointsVo.pointsNum   (仅用于上报余额)
# 环境变量：
#   YYB_SERVER      YYB-Go-Enhanced 路由，每行：地址@账号标识
#   账号直接来自 YYB_SERVER；无需配置 WX_ID / lqpj
#   YYB_API_KEY / YYB_PROTOCOL_TOKEN       启用协议鉴权时配置，二选一

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# 将脚本所在目录加入搜索路径（确保能找到 yyb.py 等同目录模块）
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
            import requests as _requests
            response = _requests.get(server + "/accounts", timeout=15)
            data = response.json()
            items = data if isinstance(data, list) else None
            if items is None and isinstance(data, dict):
                for key in ("data", "list", "rows", "result", "accounts"):
                    value = data.get(key)
                    if isinstance(value, list) and value:
                        items = value
                        break
                    if isinstance(value, dict):
                        nested = value
                        if nested and isinstance(nested, dict):
                            for k2 in ("list", "rows", "data", "result", "accounts"):
                                v2 = nested.get(k2)
                                if isinstance(v2, list) and v2:
                                    items = v2
                                    break
                        if items:
                            break
                if items is None and isinstance(data, dict):
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


def _yyb_call(endpoint, app_id, identifier):
    import requests as _requests
    route = _yyb_route(identifier)
    headers = {
            "Content-Type": "application/json"
        }
    api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
    if api_key:
        headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key
    response = _requests.post(route["server"] + endpoint,
                              json={"ref": route["ref"], "app_id": app_id}, timeout=30, headers=headers)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or int(body.get("code", -1)) != 0:
        raise RuntimeError(f"{endpoint} 返回失败：{body.get('msg') or body.get('message') or body}")
    result = (body.get("data") or {}).get("result")
    if result is None:
        raise RuntimeError(f"{endpoint} 未返回 data.result")
    return result


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
    def get_single_phone_code(app_id, identifier, login_type=None):
        result = _yyb_call("/wxapp/getPhoneNumber", app_id, identifier)
        return str(result if isinstance(result, str) else result.get("code") or "")

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
    import sys as _sys
    for path in (os.path.dirname(os.path.abspath(__file__)), "/ql/data/scripts", "/ql/scripts"):
        if path not in _sys.path:
            _sys.path.insert(0, path)
    try:
        from notify import send as _ql_send
        _ql_send(title, content)
        return True
    except Exception as exc:
        print(f"青龙通知失败（不影响任务结果）：{exc}")
        return False
# ===== adapter end =====


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    send = _send_qinglong_notify
except Exception:
    def send(title, content):
        print(f"\n===== {title} =====\n{content}")

# ---------------------------------------------------------------------------
# 常量 (均来自小程序反编译源码, 非机密)
# ---------------------------------------------------------------------------
MINI_APP_ID = "wx08dabaec2783f4e9"
BASE_URL = "https://api.mbr.liquan.com/api"          # vendor.js api_HOST
SUCCESS_CODE = 200
SESSION_INVALID_CODE = 401                            # 拦截器: 清 token 并重新登录
SIGNED = "sign"                                       # taskSignDtoList[].signStatus

# 账号列表：自动从 yyb_go 同步存活账号（可用 lqpj / WX_ID 指定白名单）
ACCOUNT_REFS = yyb.resolve_accounts("lqpj")

# ============ YYB 相关（CodeProvider 模式） ============
# 服务地址优先级：WECHAT_SERVER 环境变量 > YYB_SERVER（地址@ref 行，兼容旧配置）
WECHAT_SERVER = os.getenv("WECHAT_SERVER", "").strip()
SERVERS = [s.strip() for s in WECHAT_SERVER.split(",") if s.strip()]

YYB_REFS: List[str] = []  # 空=自动从 /accounts 获取，非空=只用这里指定的 ref

CODE_RETRY_TIMES = 3      # 获取 code 最大重试次数
RETRY_BASE_DELAY = 3      # 指数退避基数（秒）
REQUEST_TIMEOUT = 30      # 请求超时（秒）


def normalize_server(server: str) -> str:
    """标准化服务地址：去末尾斜杠，补 http:// 前缀"""
    server = str(server or "").strip().rstrip("/")
    if not server.startswith("http://") and not server.startswith("https://"):
        server = "http://" + server
    return server


def json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text[:300]


def extract_code(data: Any) -> str:
    """从 YYB 服务多层响应结构中提取 code"""
    if not isinstance(data, dict):
        return ""
    direct = data.get("code") or data.get("wx_code")
    if direct not in (None, "", 0):
        return str(direct)
    nested = data.get("Data") or data.get("data") or {}
    if isinstance(nested, dict):
        nested_code = nested.get("code") or nested.get("wx_code")
        if nested_code not in (None, "", 0):
            return str(nested_code)
        result = nested.get("result") or {}
        if isinstance(result, dict):
            result_code = result.get("code") or result.get("wx_code")
            if result_code not in (None, "", 0):
                return str(result_code)
    return ""


def extract_accounts_payload(data: Any) -> List[Any]:
    """从 YYB /accounts 响应中提取账号列表（支持多层嵌套）"""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("accounts", "data", "list", "rows", "result"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_accounts_payload(value)
            if nested:
                return nested
    return []


def extract_ref(item: Any) -> str:
    """从账号项中提取 ref 字段"""
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    for key in ("ref", "openid", "openId", "wxid", "wx_id", "id", "account"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def extract_label(item: Any, fallback: str) -> str:
    """从账号项中提取显示名（remark > name > nickname > ... > fallback）"""
    if not isinstance(item, dict):
        return fallback
    for key in ("remark", "name", "nickname", "nickName", "label", "wxid", "openid", "ref"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return fallback


@dataclass
class CodeAccount:
    label: str   # 显示名称
    ref: str     # 账号标识，提交给 /wxapp/getCode


class YybCodeProvider:
    def __init__(self, server: str):
        self.server = normalize_server(server)
        self.session = requests.Session()
        self.session.verify = False

    def list_accounts(self) -> List[CodeAccount]:
        # 优先用手动配置的 YYB_REFS
        refs = [str(x).strip() for x in YYB_REFS if str(x).strip()]
        if refs:
            return [CodeAccount(label=x, ref=x) for x in refs]

        # 自动从 /accounts 获取
        if not self.server:
            print("未配置 YYB 服务地址，且 YYB_REFS 为空")
            return []

        url = f"{self.server}/accounts"
        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            data = json_or_text(response)
            items = extract_accounts_payload(data)
        except Exception as exc:
            print(f"读取 YYB /accounts 异常：{str(exc)[:120]}")
            return []

        accounts: List[CodeAccount] = []
        seen = set()
        for item in items:
            ref = extract_ref(item)
            if not ref or ref in seen:
                continue
            seen.add(ref)
            accounts.append(CodeAccount(label=extract_label(item, ref), ref=ref))

        print(f"YYB /accounts 解析到 {len(accounts)} 个账号")
        return accounts

    def get_code(self, account: CodeAccount) -> Optional[str]:
        """POST /wxapp/getCode 获取微信 code，指数退避重试"""
        if not self.server:
            print("未配置 YYB 服务地址，无法请求 /wxapp/getCode")
            return None

        url = f"{self.server}/wxapp/getCode"
        payload = {"app_id": MINI_APP_ID, "ref": account.ref}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 MicroMessenger/8.0.50",
        }
        api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
        if api_key:
            headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key

        for attempt in range(1, CODE_RETRY_TIMES + 1):
            try:
                response = self.session.post(
                    url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
                )
                data = json_or_text(response)
                code = extract_code(data)
                if code:
                    return code
                print(f"{account.label} 获取 code 为空 resp={str(data)[:160]}")
            except Exception as exc:
                print(f"{account.label} 获取 code 异常：{str(exc)[:120]}")

            if attempt < CODE_RETRY_TIMES:
                time.sleep(RETRY_BASE_DELAY * attempt)

        return None
# =================================================

TOKEN_CACHE_PATH = Path(__file__).with_name("liquanpijiu_token_cache.json")
DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G9910 Build/TP1A.220624.014) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.49.2600(0x28003137) NetType/WIFI Language/zh_CN "
    "miniProgram/" + MINI_APP_ID
)

session = requests.Session()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def bj_now():
    return datetime.now(timezone(timedelta(hours=8)))


def mask(value):
    if not value:
        return ""
    value = str(value)
    if len(value) <= 12:
        return value[:2] + "***"
    return f"{value[:6]}***{value[-4:]}"


def read_token_cache():
    try:
        if TOKEN_CACHE_PATH.exists():
            return json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def write_token_cache(cache):
    try:
        TOKEN_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠️ 写入token缓存失败: {e}")


def get_cached_session(openid):
    return read_token_cache().get(openid) or {}


def save_cached_session(openid, token, union_id):
    cache = read_token_cache()
    cache[openid] = {"token": token, "unionId": union_id,
                     "updatedAt": int(time.time())}
    write_token_cache(cache)


def remove_cached_session(openid):
    cache = read_token_cache()
    if openid in cache:
        del cache[openid]
        write_token_cache(cache)


# ---------------------------------------------------------------------------
# yyb_go: 账号标识 -> wx.login code
# ---------------------------------------------------------------------------
def get_wx_code(openid):
    """从 yyb_go 获取一次性微信 code（openid 为账号标识）"""
    try:
        code = yyb.get_single_code(MINI_APP_ID, str(openid).split("#")[0].strip())
        if code:
            print("✅ [授权] code 获取成功")
            return str(code)
        print("❌ [授权] code 获取失败")
        return None
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


# ---------------------------------------------------------------------------
# CodeProvider 路径：provider -> wx.login code
# ---------------------------------------------------------------------------
_PROVIDER_CONTEXT: Dict[str, Tuple[YybCodeProvider, CodeAccount]] = {}


def provider_for(account: CodeAccount, provider: YybCodeProvider) -> None:
    """注册账号到 provider 的映射，供 run_account 内部取码使用。"""
    _PROVIDER_CONTEXT[account.ref] = (provider, account)


def get_wx_code_provider(openid):
    """优先走 YybCodeProvider 取码，失败回落旧适配器。"""
    context = _PROVIDER_CONTEXT.get(str(openid).split("#")[0].strip())
    if context:
        provider, account = context
        code = provider.get_code(account)
        if code:
            print("✅ [授权] code 获取成功")
            return str(code)
        print("❌ [授权] provider code 获取失败")
        return None
    return get_wx_code(openid)


# ---------------------------------------------------------------------------
# 漓泉会员 API
# ---------------------------------------------------------------------------
def api_headers(token=None):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": DEFAULT_UA,
        "Referer": f"https://servicewechat.com/{MINI_APP_ID}/0/page-frame.html",
    }
    if token:
        headers["Token"] = token            # 请求拦截器: headers.Token = 本地 token
    return headers


def api_get(path, token=None, params=None):
    resp = session.get(f"{BASE_URL}{path}", params=params or {},
                       headers=api_headers(token), timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_post(path, body, token=None):
    resp = session.post(f"{BASE_URL}{path}",
                        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                        headers=api_headers(token), timeout=30)
    resp.raise_for_status()
    return resp.json()


def resp_code(resp):
    """拦截器口径: errcode 优先, 否则 code。"""
    node = resp if isinstance(resp, dict) else {}
    for key in ("errcode", "code"):
        v = node.get(key)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return -1


def resp_msg(resp):
    node = resp if isinstance(resp, dict) else {}
    for key in ("errmsg", "message", "msg"):
        v = node.get(key)
        if v:
            return str(v)
    data = node.get("data")
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])
    return ""


# ---------------------------------------------------------------------------
# 登录 / 会话
# ---------------------------------------------------------------------------
def login(openid):
    """wx.login code -> wxLogin -> (token, unionId)。code 放在 URL 路径中。"""
    code = get_wx_code_provider(openid)
    resp = api_get(f"/mbr/members/wxLogin/{code}", params={"appId": MINI_APP_ID})
    if resp_code(resp) != 0:
        raise RuntimeError(f"登录失败: {resp_msg(resp) or resp_code(resp)}")
    data = resp.get("data") or {}
    token = data.get("token")
    union_id = data.get("unionid") or (data.get("wxUserInfo") or {}).get("unionId")
    if not (token and data.get("b2cMemberId")):
        raise RuntimeError("登录响应缺少 token/b2cMemberId, 需在小程序内登录一次后重试")
    return token, union_id


def obtain_session(openid, index, force=False):
    if force:
        remove_cached_session(openid)
    else:
        cached = get_cached_session(openid)
        if cached.get("token"):
            print(f"账号 {index} 使用缓存 token: {mask(cached['token'])}")
            return cached["token"], cached.get("unionId")
    token, union_id = login(openid)
    save_cached_session(openid, token, union_id)
    print(f"账号 {index} 静默登录成功: {mask(token)}")
    return token, union_id


# ---------------------------------------------------------------------------
# 签到业务
# ---------------------------------------------------------------------------
def sign_state(token):
    return api_get("/b2c/member/sign/task/list", token=token)


def do_sign(token):
    return api_post("/b2c/member/sign/task", {}, token=token)


def points_balance(token, union_id):
    """仅读取积分余额用于上报, 失败返回 None (不影响签到结论)。"""
    if not union_id:
        return None
    try:
        resp = api_get("/b2c/member/pointsAndCouponCardNumAndShopCardInfo",
                       token=token, params={"unionId": union_id})
        if resp_code(resp) != SUCCESS_CODE:
            return None
        vo = (resp.get("data") or {}).get("MemberCouponShopPointsVo") or {}
        return vo.get("pointsNum")
    except Exception:
        return None


def sign_res(resp):
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict) and isinstance(data.get("signRes"), dict):
        return data["signRes"]
    return {}


def signed_today(res):
    """今日已签: signRes.sign 为真, 或今日日期行 signStatus == 'sign'。"""
    if res.get("sign") is True:
        return True
    today = bj_now().strftime("%Y-%m-%d")
    for row in res.get("taskSignDtoList") or []:
        if row.get("signTime") == today and row.get("signStatus") == SIGNED:
            return True
    return False


def fmt_num(value):
    """服务端 signNum 为浮点(1.0), 展示成整数更自然。"""
    try:
        f = float(value)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(value)


def run_account(openid, index):
    lines = [f"【账号 {index}】"]

    token, union_id = obtain_session(openid, index)

    # 读取签到状态 (首个鉴权调用); 会话失效则强制重登重试一次
    state = sign_state(token)
    if resp_code(state) == SESSION_INVALID_CODE:
        print(f"账号 {index} 会话失效, 重新登录...")
        token, union_id = obtain_session(openid, index, force=True)
        state = sign_state(token)

    scode = resp_code(state)
    if scode != SUCCESS_CODE:
        msg = resp_msg(state) or f"获取签到状态失败 code={scode}"
        print(f"❌ 账号 {index} {msg}")
        lines.append(f"❌ {msg}")
        return "\n".join(lines), False

    res = sign_res(state)

    # 幂等预检: 今日已签则不再提交
    if signed_today(res):
        msg = f"今日已签到, 无需重复 (本周已签 {fmt_num(res.get('signNum'))} 天)"
        print(f"✅ 账号 {index} {msg}")
        lines.append(f"✅ {msg}")
        pts = points_balance(token, union_id)
        if pts is not None:
            lines.append(f"当前积分: {pts}")
            print(f"账号 {index} 当前积分: {pts}")
        return "\n".join(lines), True

    # 执行一次签到
    result = do_sign(token)
    rcode = resp_code(result)
    if rcode == SESSION_INVALID_CODE:
        print(f"账号 {index} 会话失效, 重新登录后重试签到...")
        token, union_id = obtain_session(openid, index, force=True)
        result = do_sign(token)
        rcode = resp_code(result)

    if rcode != SUCCESS_CODE:
        msg = resp_msg(result) or f"签到失败 code={rcode}"
        print(f"❌ 账号 {index} {msg}")
        lines.append(f"❌ {msg}")
        return "\n".join(lines), False

    res = sign_res(result)
    if not signed_today(res) and res.get("sign") is not True:
        msg = resp_msg(result) or "签到接口返回成功但未标记已签, 请稍后重试"
        print(f"⚠️ 账号 {index} {msg}")
        lines.append(f"⚠️ {msg}")
        return "\n".join(lines), False

    msg = f"签到成功, 本周已签 {fmt_num(res.get('signNum'))} 天"
    print(f"🎉 账号 {index} {msg}")
    lines.append(f"🎉 {msg}")

    pts = points_balance(token, union_id)
    if pts is not None:
        lines.append(f"当前积分: {pts}")
        print(f"账号 {index} 当前积分: {pts}")
    return "\n".join(lines), True


def main():
    # Step 1: 先收集账号（WECHAT_SERVER /accounts 优先，YYB_SERVER 地址@ref 行兼容回退）
    all_accounts: List[Tuple[CodeAccount, YybCodeProvider]] = []
    for server in SERVERS:
        provider = YybCodeProvider(server)
        for account in provider.list_accounts():
            all_accounts.append((account, provider))
    if not all_accounts:
        providers: Dict[str, YybCodeProvider] = {}
        for entry in ACCOUNT_REFS:
            parts = entry.split("#", 1)
            openid = parts[0].strip()
            if not openid:
                continue
            # 经 _yyb_routes 找到该 ref 对应的服务地址
            try:
                route = _yyb_route(openid)
            except Exception:
                route = None
            server = normalize_server(route["server"]) if route else ""
            if not server:
                continue
            provider = providers.setdefault(server, YybCodeProvider(server))
            remark = parts[1].strip() if len(parts) > 1 else openid
            all_accounts.append((CodeAccount(label=remark, ref=openid), provider))
        # 只填地址（无 @ref）的行：自动从 /accounts 发现账号
        if not all_accounts:
            for line in (os.getenv("YYB_SERVER", "") or "").splitlines():
                line = line.strip()
                if not line or "@" in line:
                    continue
                server = normalize_server(line)
                if not server:
                    continue
                provider = providers.setdefault(server, YybCodeProvider(server))
                found = provider.list_accounts()
                if not found:
                    print(f"⚠️ {server} /accounts 未解析到账号（请确认 YYB 服务在线且已授权）")
                    continue
                for account in found:
                    all_accounts.append((account, provider))

    if not all_accounts:
        print("❌ 未从 yyb_go 获取到存活账号(可配置环境变量 lqpj 指定白名单), 退出。")
        return

    print("=============== 漓泉啤酒 签到开始 ===============")
    summaries = []
    ok_count = 0
    # Step 2: 执行任务
    for i, (account, provider) in enumerate(all_accounts, 1):
        print(f"\n-------------- 账号 {i}{('/' + account.label if account.label != account.ref else '')} --------------")
        try:
            provider_for(account, provider)
            summary, ok = run_account(account.ref, i)
            summaries.append(summary)
            ok_count += 1 if ok else 0
        except Exception as e:
            print(f"❌ 账号 {i} 执行异常: {e}")
            summaries.append(f"【账号 {i}】\n❌ 执行异常: {e}")
        time.sleep(1)

    print("\n=============== 漓泉啤酒 签到结束 ===============")
    title = f"漓泉啤酒签到 {ok_count}/{len(all_accounts)} 成功"
    try:
        send(title, "\n\n".join(summaries))
    except Exception as e:
        print(f"⚠️ 通知发送失败: {e}")


if __name__ == "__main__":
    main()
