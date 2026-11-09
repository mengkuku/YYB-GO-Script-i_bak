# cron: 57 8,16 * * *
#!/usr/bin/env python3
# name: 嘉立创
# -*- coding: utf-8 -*-

"""
JLC 小程序签到脚本（青龙面板版 / 多账号 / 推送 / 查询豆豆总数）
【环境变量】
- YYB_SERVER: YYB-Go-Enhanced 账号配置，格式：server:port@ref，多账号换行
- YYB_API_KEY / YYB_PROTOCOL_TOKEN       启用协议鉴权时配置，二选一
- JLC: 可选，仅运行指定 ref，格式：ref#备注，多账号用换行或 & 分隔
- JLC_AUTH: 可选手动凭据，格式：token#secret，多账号用换行或 & 分隔
【依赖】
- requests
【第七天逻辑说明】
- 状态接口返回 data.day == 7 且 haveSignIn==True 且 haveReceive==False 时，
  自动调用 receiveVoucher 领取"8 豆豆"。
"""

import os
import sys
import json
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ===================== 通知推送 =====================
try:
    from notify import send as notify_send
except ImportError:
    def notify_send(title, content):
        print(f"--- 通知 ---\n{title}\n{content}\n-------------")

# 日志收集
log_lines = []
SCRIPT_NAME = "JLC 嘉立创签到"
# ===================================================

# ===================== 手动调试开关 =====================
DEBUG = False
DEBUG_ENV = {
    "JLC": "account-ref#测试号",
}
if DEBUG:
    for _k, _v in DEBUG_ENV.items():
        os.environ.setdefault(_k, _v)
    print("⚠️ 调试模式已开启（DEBUG=True），使用脚本内置 DEBUG_ENV 配置")
# =======================================================

# ===================== YYB CodeProvider 模式 =====================
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


class CodeAccount:
    def __init__(self, label: str, ref: str):
        self.label = label   # 显示名称
        self.ref = ref       # 账号标识，提交给 /wxapp/getCode


class YybCodeProvider:
    def __init__(self, server: str, appid: str):
        self.server = normalize_server(server)
        self.appid = appid
        self.session = requests.Session()
        self.session.verify = False

    def list_accounts(self) -> List[CodeAccount]:
        # 优先用手动配置的 YYB_REFS
        refs = [str(x).strip() for x in YYB_REFS if str(x).strip()]
        if refs:
            return [CodeAccount(x, x) for x in refs]

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
            accounts.append(CodeAccount(extract_label(item, ref), ref))

        print(f"YYB /accounts 解析到 {len(accounts)} 个账号")
        return accounts

    def get_code(self, account: CodeAccount) -> Optional[str]:
        """POST /wxapp/getCode 获取微信 code，指数退避重试"""
        if not self.server:
            print("未配置 YYB 服务地址，无法请求 /wxapp/getCode")
            return None

        url = f"{self.server}/wxapp/getCode"
        payload = {"app_id": self.appid, "ref": account.ref}
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

BASE_URL = "https://m.jlc.com"
CAS_BASE_URL = "https://passport.jlc.com"
CAS_APP_ID = os.getenv("JLC_CAS_APP_ID", "JLC_MOBILE_APP").strip()
PLATFORM_TYPE = os.getenv("JLC_PLATFORM_TYPE", "MP-WEIXIN").strip()
SOURCE = os.getenv("JLC_SOURCE", "2").strip()

# 嘉立创小程序 appid
JLC_MINI_APPID = os.getenv("JLC_MINI_APPID", "wx6c7b851c877dba42").strip()
# 旧版固定 key 仅用于兼容已有缓存；当前 key 必须从官方更新接口动态获取。
LEGACY_SECRET_KEY = "34343232636134362d646135612d346335662d386464382d356237633937623132413437"
SECRET_UPDATE_URL = BASE_URL + "/api/integrated/secret/update"
CAS_CHECK_APPLET_LOGIN_URL = CAS_BASE_URL + "/api/cas/sso/login/check-applet-login"
CAS_APPLET_LOGIN_URL = CAS_BASE_URL + "/api/cas/sso/login/applet-silent-login"
SECRET_EXPIRED_CODES = {29001, 29003}

# Token 缓存路径
TOKEN_CACHE_PATH = Path(__file__).with_name("JLC_token_cache.json")

# 默认 UA
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF"
)

session = requests.Session()
_CURRENT_SECRET_KEY = ""


def read_token_cache():
    try:
        if not TOKEN_CACHE_PATH.exists():
            return {}
        return json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def write_token_cache(cache):
    try:
        TOKEN_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠️ 写入token缓存失败: {e}")


def mask_token(token):
    if not token:
        return ""
    return f"{token[:6]}***{token[-6:]}"


# ===================== 登录相关 =====================
def _yyb_entries() -> List[Tuple[str, str]]:
    """解析 YYB_SERVER，每行格式为 server:port@ref。"""
    entries: List[Tuple[str, str]] = []
    for line in os.getenv("YYB_SERVER", "").splitlines():
        value = line.strip()
        if not value or "@" not in value:
            continue
        server, ref = value.rsplit("@", 1)
        server, ref = server.strip().rstrip("/"), ref.strip()
        if not server or not ref:
            continue
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        entries.append((server, ref))
    return entries


def _install_yyb_account_view() -> None:
    """未显式配置 JLC/WX_ID 时，让原账号循环读取 YYB_SERVER 中的 ref。"""
    if os.getenv("JLC") or os.getenv("WX_ID"):
        return
    refs = [ref for _, ref in _yyb_entries()]
    if refs:
        os.environ["WX_ID"] = "\n".join(refs)


def _select_yyb_account(identifier: str) -> Tuple[str, str]:
    entries = _yyb_entries()
    if not entries:
        raise RuntimeError("未配置 YYB_SERVER，格式：地址@账号ref，多账号换行")
    raw = str(identifier or "").split("#", 1)[0].strip()
    for server, ref in entries:
        if raw in (ref, f"{server}@{ref}"):
            return server, ref
    if len(entries) == 1:
        return entries[0]
    raise RuntimeError(f"YYB_SERVER 中找不到账号ref：{raw}")


def get_wx_code(account_id):
    """直接调用 YYB-Go-Enhanced 获取嘉立创小程序 wx.login code。

    优先走 YybCodeProvider（重试+退避+鉴权头）；JLC/WX_ID 白名单或旧配置回落原路径。
    """
    try:
        server, ref = _select_yyb_account(account_id)
        provider = YybCodeProvider(server, JLC_MINI_APPID)
        code = provider.get_code(CodeAccount(ref, ref))
        if code:
            return str(code)
        return None
    except Exception as exc:
        print(f"[YYB] 获取code失败：{exc}")
        return None


_install_yyb_account_view()


def _extract_login_auth(resp, fallback_secret):
    """兼容从响应头或 JSON 响应体读取登录凭据。"""
    token = resp.headers.get("X-Jlc-Accesstoken") or resp.headers.get("x-jlc-accesstoken")
    secret = resp.headers.get("secretkey") or resp.headers.get("Secretkey") or fallback_secret
    try:
        payload = resp.json()
    except (ValueError, TypeError):
        payload = {}

    candidates = [payload]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        candidates.insert(0, payload["data"])
    for item in candidates:
        if not isinstance(item, dict):
            continue
        token = token or item.get("accessToken") or item.get("token") or item.get("access_token")
        secret = item.get("secretkey") or item.get("secretKey") or secret

    if token and str(token).upper() != "NONE":
        return str(token), str(secret)
    return None, None


def refresh_secret_key(token="NONE", previous_key=""):
    """从嘉立创官方接口获取当前 keyId，不输出密钥内容。"""
    global _CURRENT_SECRET_KEY
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "X-JLC-AccessToken": token or "NONE",
        "X-JLC-ClientType": "MP-WEIXIN",
        "X-JLC-MP-AppId": JLC_MINI_APPID,
        "X-JLC-MP-Env": os.getenv("JLC_MP_ENV", "release"),
        "X-JLC-MP-Version": os.getenv("JLC_MP_VERSION", "1.117.4"),
        "origin": BASE_URL,
        "referer": BASE_URL + "/",
        "user-agent": DEFAULT_UA,
    }
    payload = {"keyId": previous_key} if previous_key else {}
    resp = session.post(SECRET_UPDATE_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    key_info = data.get("data") or {}
    key_id = key_info.get("keyId") if isinstance(key_info, dict) else None
    if data.get("code") != 200 or not key_id:
        raise RuntimeError(f"更新secretkey失败: {str(data.get('message') or data)[:200]}")
    _CURRENT_SECRET_KEY = str(key_id)
    print("[JLC] secretkey 已动态更新")
    return _CURRENT_SECRET_KEY


def _extract_cas_auth_code(payload):
    """从 CAS 响应中递归提取真正供 login-by-code 使用的 AC- 授权码。"""
    if isinstance(payload, str):
        return payload if payload.startswith("AC-") else None
    if isinstance(payload, dict):
        for value in payload.values():
            code = _extract_cas_auth_code(value)
            if code:
                return code
    if isinstance(payload, list):
        for value in payload:
            code = _extract_cas_auth_code(value)
            if code:
                return code
    return None


def get_cas_auth_code(account_id):
    """按嘉立创小程序 154 的官方链路把 wx.login code 换成 CAS AC- code。"""
    wx_code = get_wx_code(account_id)
    if not wx_code:
        raise RuntimeError("YYB未返回有效微信code")

    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-encoding": "gzip, deflate",
        "content-type": "application/json",
        "referer": f"https://servicewechat.com/{JLC_MINI_APPID}/154/page-frame.html",
        "user-agent": DEFAULT_UA,
    }
    cas_session = requests.Session()
    check_resp = cas_session.post(
        CAS_CHECK_APPLET_LOGIN_URL,
        json={"appId": CAS_APP_ID, "appletAuthCode": wx_code},
        headers=headers,
        timeout=30,
    )
    check_resp.raise_for_status()
    check_data = check_resp.json()
    check_info = check_data.get("data") or {}
    applet_login_token = check_info.get("token") if isinstance(check_info, dict) else None
    if check_data.get("code") != 200 or not applet_login_token:
        message = check_data.get("message") or check_data.get("msg") or check_data
        raise RuntimeError(f"CAS检查小程序登录失败: {str(message)[:200]}")
    if check_info.get("isLoginBind") is False or check_info.get("bind") is False:
        raise RuntimeError("嘉立创账号尚未绑定当前微信，请先在官方小程序完成绑定")

    login_headers = dict(headers)
    login_headers["referer"] = f"{CAS_BASE_URL}/m/login/mp-login?appId={CAS_APP_ID}"
    login_resp = cas_session.post(
        CAS_APPLET_LOGIN_URL,
        json={"token": applet_login_token, "appId": CAS_APP_ID},
        headers=login_headers,
        timeout=30,
    )
    login_resp.raise_for_status()
    login_data = login_resp.json()
    cas_code = _extract_cas_auth_code(login_data)
    if login_data.get("code") != 200 or not cas_code:
        message = login_data.get("message") or login_data.get("msg") or login_data
        raise RuntimeError(f"CAS小程序静默登录失败: {str(message)[:200]}")
    print(f"[{account_id}] CAS授权码获取成功")
    return cas_code


def login_with_code(account_id):
    """使用 CAS AC- code 登录，并在密钥过期时自动更新后重试。"""
    url = BASE_URL + "/api/login/login-by-code"
    secret = refresh_secret_key()
    errors = []
    for attempt in range(2):
        headers = {
            "accept": "application/json, text/plain, */*",
            "referer": f"https://servicewechat.com/{JLC_MINI_APPID}/154/page-frame.html",
            "X-JLC-AccessToken": "NONE",
            "X-JLC-ClientType": "MP-WEIXIN",
            "X-JLC-MP-AppId": JLC_MINI_APPID,
            "X-JLC-MP-Env": os.getenv("JLC_MP_ENV", "release"),
            "X-JLC-MP-Version": os.getenv("JLC_MP_VERSION", "1.117.4"),
            "secretkey": secret,
            "xweb_xhr": "1",
            "user-agent": DEFAULT_UA,
        }
        try:
            code = get_cas_auth_code(account_id)
            resp = session.post(url, files={"code": (None, code)}, headers=headers, timeout=30)
            token, response_secret = _extract_login_auth(resp, secret)
            if token:
                print(f"[{account_id}] login-by-code 登录成功")
                return {
                    "token": token,
                    "secret": response_secret,
                    "updatedAt": int(time.time()),
                }
            body = resp.text.replace("\r", " ").replace("\n", " ")[:300]
            detail = f"第{attempt + 1}次: status={resp.status_code}, body={body}"
            errors.append(detail)
            try:
                response_code = resp.json().get("code")
            except (ValueError, AttributeError):
                response_code = None
            if attempt == 0 and (response_code in SECRET_EXPIRED_CODES or resp.status_code == 460):
                secret = refresh_secret_key(previous_key=secret)
                print(f"[{account_id}] 登录未通过，已刷新secretkey并使用新CAS授权码重试")
                continue
            print(f"[{account_id}] login-by-code 未通过（{detail}）")
            break
        except Exception as e:
            detail = f"第{attempt + 1}次: 请求异常={e}"
            errors.append(detail)
            print(f"[{account_id}] login-by-code {detail}")
            break

    raise RuntimeError("CAS登录失败；" + "；".join(errors))


def get_cached_token(account_id):
    return read_token_cache().get(account_id)


def save_cached_token(account_id, auth_info):
    cache = read_token_cache()
    cache[account_id] = auth_info
    write_token_cache(cache)


def remove_cached_token(account_id):
    cache = read_token_cache()
    if account_id in cache:
        del cache[account_id]
        write_token_cache(cache)


def validate_token(token, secret):
    """验证 token 是否有效"""
    try:
        url = BASE_URL + "/api/activity/sign/getCurrentUserSignInConfig"
        current_secret = _CURRENT_SECRET_KEY or secret or LEGACY_SECRET_KEY
        for attempt in range(2):
            headers = {
                "x-jlc-accesstoken": token,
                "secretkey": current_secret,
                "user-agent": DEFAULT_UA,
            }
            resp = session.get(url, params={"platformType": PLATFORM_TYPE}, headers=headers, timeout=15)
            if resp.status_code != 200:
                return False
            data = resp.json()
            if attempt == 0 and data.get("code") in SECRET_EXPIRED_CODES:
                current_secret = refresh_secret_key(token=token, previous_key=current_secret)
                continue
            return data.get("success") is True
        return False
    except Exception:
        return False


def get_token_for_account(account_id, remark):
    """获取账号的 token（优先使用缓存）"""
    cached = get_cached_token(account_id)
    if cached and cached.get("token"):
        print(f"[{remark}] 使用缓存token: {mask_token(cached['token'])}")
        cached_secret = cached.get("secret", LEGACY_SECRET_KEY)
        if validate_token(cached["token"], cached_secret):
            return cached["token"], _CURRENT_SECRET_KEY or cached_secret
        print(f"[{remark}] 缓存token失效，重新登录")
        remove_cached_token(account_id)

    auth_info = login_with_code(account_id)
    save_cached_token(account_id, auth_info)
    print(f"[{remark}] 登录成功")
    return auth_info["token"], auth_info.get("secret", _CURRENT_SECRET_KEY or LEGACY_SECRET_KEY)


# ===================== 工具函数 =====================


def _env(key: str, default: str = "") -> str:
    v = os.environ.get(key, "")
    v = v.strip()
    return v if v else default


def _split_accounts(s: str) -> List[str]:
    """支持 & 或换行 分隔"""
    if not s:
        return []
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    parts: List[str] = []
    for chunk in s.split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts.extend([p.strip() for p in chunk.split("&") if p.strip()])
    return parts


def parse_accounts() -> List[Dict[str, str]]:
    """
    账号解析：
    - WX_ID / JLC（推荐）：wxid#备注，通过微信协议服务自动登录换 token
    """
    remarks_raw = _env("JLC_REMARKS")
    remarks = _split_accounts(remarks_raw)
    accounts: List[Dict[str, str]] = []

    # 1) 显式 token 模式优先。否则 getCode 导出的 WX_ID 会使 JLC_AUTH 永远失效。
    auth_raw = _env("JLC_AUTH")
    if auth_raw:
        items = _split_accounts(auth_raw)
        for idx, item in enumerate(items):
            sep = "#" if "#" in item else ("|" if "|" in item else ("," if "," in item else None))
            if not sep:
                raise RuntimeError("JLC_AUTH 格式错误，应为 token#secret（多账号用 & 或换行分隔）。")
            token, secret = item.split(sep, 1)
            token, secret = token.strip(), secret.strip()
            if not token or not secret:
                raise RuntimeError("JLC_AUTH 中存在空的 token 或 secret。")
            accounts.append({
                "mode": "token",
                "token": token,
                "secret": secret,
                "remark": remarks[idx] if idx < len(remarks) else f"账号{idx+1}",
            })
        return accounts

    # 2) wxid 自动登录模式（推荐）；JLC 可覆盖全局 YYB 账号列表。
    wxid_raw = os.getenv("JLC") or os.getenv("WX_ID", "")
    if wxid_raw:
        items = _split_accounts(wxid_raw)
        for idx, item in enumerate(items):
            if "#" in item:
                wxid, remark = item.split("#", 1)
                wxid, remark = wxid.strip(), remark.strip()
            else:
                wxid, remark = item.strip(), ""
            if not wxid:
                continue
            accounts.append({
                "mode": "wxid",
                "wxid": wxid,
                "remark": remark or (remarks[idx] if idx < len(remarks) else f"账号{idx+1}"),
            })
        if accounts:
            return accounts

    raise RuntimeError("缺少环境变量：请设置 WX_ID 或 JLC（推荐）或 JLC_AUTH。")


def build_headers(token: str, secret: str) -> Dict[str, str]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "x-jlc-accesstoken": token,
        "secretkey": _CURRENT_SECRET_KEY or secret,
        "origin": BASE_URL,
        "referer": BASE_URL + "/",
        "user-agent": DEFAULT_UA,
    }
    return {k: v for k, v in headers.items() if v}


def api_get(session: requests.Session, path: str, params: Optional[Dict[str, Any]] = None,
            token: str = "", secret: str = "") -> Dict[str, Any]:
    url = BASE_URL + path
    for attempt in range(2):
        headers = build_headers(token, secret) if token else {}
        r = session.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"接口返回非 JSON：{url}\n{r.text[:300]}")
        if attempt == 0 and data.get("code") in SECRET_EXPIRED_CODES:
            refresh_secret_key(token=token, previous_key=headers.get("secretkey", ""))
            continue
        return data
    return data


def get_sign_status(s, token, secret) -> Dict[str, Any]:
    return api_get(s, "/api/activity/sign/getCurrentUserSignInConfig",
                   params={"platformType": PLATFORM_TYPE}, token=token, secret=secret)


def do_signin(s, token, secret) -> Dict[str, Any]:
    return api_get(s, "/api/activity/sign/signIn",
                   params={"platformType": PLATFORM_TYPE, "source": SOURCE}, token=token, secret=secret)


def receive_voucher(s, token, secret) -> Dict[str, Any]:
    return api_get(s, "/api/activity/sign/receiveVoucher",
                   params={"platformType": PLATFORM_TYPE}, token=token, secret=secret)


def get_doudou_total(s, token, secret) -> Dict[str, Any]:
    return api_get(s, "/api/activity/front/getCustomerIntegral", token=token, secret=secret)


def try_send_notify(title: str, content: str) -> None:
    """青龙通知（兼容）"""
    try:
        notify_send(title, content)
    except Exception as e:
        print(f"（推送失败：{e}）")


def format_line(ok: bool, text: str) -> str:
    return ("✅ " if ok else "❌ ") + text


def _extract_streak_day(st_data: Dict[str, Any]) -> Optional[int]:
    for k in ("day", "continuousDay", "continueDay", "signDay"):
        v = st_data.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def run_one_account(idx: int, remark: str, token: str, secret: str) -> Tuple[bool, str, Dict[str, Any]]:
    result: Dict[str, Any] = {
        "remark": remark,
        "signed": None,
        "gain_signin": None,
        "gain_day7": None,
        "total": None,
        "expireTime": None,
        "streak_day": None,
    }

    log_lines: List[str] = []
    ok_all = True

    with requests.Session() as s:
        # 1) 查状态
        st = get_sign_status(s, token, secret)
        if not st.get("success", False):
            ok_all = False
            log_lines.append(format_line(False, f"[{remark}] 查询签到状态失败：{json.dumps(st, ensure_ascii=False)}"))
            return ok_all, "\n".join(log_lines), result

        st_data = st.get("data") or {}
        have_signin = st_data.get("haveSignIn") is True
        have_receive = st_data.get("haveReceive") is True
        streak_day = _extract_streak_day(st_data)
        result["streak_day"] = streak_day

        if streak_day is not None:
            log_lines.append(format_line(True, f"[{remark}] 当前连续签到天数：{streak_day} 天"))
        else:
            log_lines.append(format_line(True, f"[{remark}] 当前连续签到天数：未知"))

        # 2) 签到（若未签）
        if have_signin:
            result["signed"] = True
            log_lines.append(format_line(True, f"[{remark}] 今日已签到"))
        else:
            si = do_signin(s, token, secret)
            if not si.get("success", False):
                ok_all = False
                result["signed"] = False
                log_lines.append(format_line(False, f"[{remark}] 签到失败：{json.dumps(si, ensure_ascii=False)}"))
            else:
                si_data = si.get("data") or {}
                gain = si_data.get("gainNum") or 0
                result["signed"] = True
                result["gain_signin"] = gain
                log_lines.append(format_line(True, f"[{remark}] 签到成功，本次获得：{gain} 豆豆"))

                # 签到后刷新状态
                st2 = get_sign_status(s, token, secret)
                if st2.get("success", False):
                    st_data = st2.get("data") or {}
                    have_signin = st_data.get("haveSignIn") is True
                    have_receive = st_data.get("haveReceive") is True
                    streak_day = _extract_streak_day(st_data)
                    result["streak_day"] = streak_day

        # 3) 第七天领取 8 豆豆
        if streak_day == 7 and have_signin and (not have_receive):
            log_lines.append(format_line(True, f"[{remark}] 检测到连续签到第 7 天且未领取，开始领取..."))
            rv = receive_voucher(s, token, secret)
            if not rv.get("success", False):
                ok_all = False
                log_lines.append(format_line(False, f"[{remark}] 第七天领取失败：{json.dumps(rv, ensure_ascii=False)}"))
            else:
                got = rv.get("data")
                result["gain_day7"] = got
                log_lines.append(format_line(True, f"[{remark}] 第七天领取成功：+{got} 豆豆"))
        elif streak_day == 7 and have_signin and have_receive:
            log_lines.append(format_line(True, f"[{remark}] 连续签到第 7 天奖励已领取"))

        # 4) 查豆豆总数
        ct = get_doudou_total(s, token, secret)
        if not ct.get("success", False):
            ok_all = False
            log_lines.append(format_line(False, f"[{remark}] 查询豆豆总数失败：{json.dumps(ct, ensure_ascii=False)}"))
        else:
            data = ct.get("data") or {}
            total = data.get("integralVoucher")
            expire_time = data.get("expireTime")
            result["total"] = total
            result["expireTime"] = expire_time
            extra = f"，有效期至：{expire_time}" if expire_time else ""
            log_lines.append(format_line(True, f"[{remark}] 当前豆豆总数：{total}{extra}"))

    return ok_all, "\n".join(log_lines), result


def main() -> int:
    global log_lines

    accounts = parse_accounts()
    any_fail = False

    log_lines.append(f"\n{' ' * 5}{SCRIPT_NAME}")
    log_lines.append("-------- 开 始 执 行 --------")
    log_lines.append(f"账号数量：{len(accounts)}")
    print(f"\n{' ' * 5}{SCRIPT_NAME}")
    print("-------- 开 始 执 行 --------")
    print(f"账号数量：{len(accounts)}")

    for idx, acc in enumerate(accounts, start=1):
        remark = acc["remark"]

        log_lines.append(f"\n📋 账号 [{idx}/{len(accounts)}]")
        log_lines.append(f"📋 当前账号：{remark or f'账号{idx}'}")
        print(f"\n📋 账号 [{idx}/{len(accounts)}]")
        print(f"📋 当前账号：{remark or f'账号{idx}'}")

        # wxid 模式：先用微信协议服务登录换取 token
        if acc.get("mode") == "wxid":
            try:
                token, secret = get_token_for_account(acc["wxid"], remark)
                log_lines.append("✅ 登录成功")
            except Exception as e:
                any_fail = True
                log_lines.append(format_line(False, f"❌ 登录失败: {e}"))
                print(format_line(False, f"❌ 登录失败: {e}"))
                if idx < len(accounts):
                    time.sleep(2)
                continue
        else:
            token, secret = acc["token"], acc["secret"]
            log_lines.append("📌 手动模式，跳过登录")

        ok, log_text, res = run_one_account(idx, remark, token, secret)
        log_lines.append(log_text)

        if not ok:
            any_fail = True

        # 账号间延迟
        if idx < len(accounts):
            time.sleep(2)

    log_lines.append("\n-------- 执 行 结 束 --------")
    print("\n-------- 执 行 结 束 --------")

    # 推送完整日志
    try:
        notify_send(f"{SCRIPT_NAME} 运行日志", "\n".join(log_lines))
    except Exception:
        pass

    return 1 if any_fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("❌ 脚本异常：", str(e))
        traceback.print_exc()
        try_send_notify("JLC 签到脚本异常", str(e))
        sys.exit(1)
