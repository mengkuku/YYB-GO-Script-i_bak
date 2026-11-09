#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 神州车友会
# cron: 18 9 * * *
"""
name: 神州车友会签到
cron: 18 9 * * *

神州车友会（神州租车）微信小程序每日签到，基于 YYB-Go-Enhanced 自动取码登录。

青龙环境变量：
  YYB_SERVER          必填，YYB-Go-Enhanced地址@微信账号标识，多账号每行一条
                      例：yyb-go:8000@1
  YYB_API_KEY         可选，对应 YYB_PROTOCOL_TOKEN，设置后带 Authorization 头
  QSW_DRY_RUN         可选，=1 只查询不签到（建议首次这样试）
  QSW_BIND_PHONE      可选，默认 1；=0 关闭手机号自动绑定（仅适合已绑定的账号）
  QSW_ENABLE_LOTTERY  可选，默认 1；=0 关闭周周签到抽奖
  QSW_LOGIN_RETRY     可选，登录重试次数，默认 3（code 一次性，失败会换新 code 重试）
  QSW_RANDOM_HEADERS  可选，默认 1；=0 关闭随机请求头
  QSW_UA_MODE         可选，默认 request（每次请求随机）；run=每次运行固定一个 UA
  QSW_DEBUG           可选，=1 打印请求明细，便于排查接口变动
  YYB_REQUEST_TIMEOUT 可选，YYB 接口超时秒数，默认 40

依赖：requests（仓库 requirements.txt 已声明）
通知：优先使用青龙内置 notify.py；未配置时回落到 PushPlus / Server酱 / 企业微信 / Bark。
      通知失败不影响签到任务结果。
功能：自动登录（YYB 取码 + 小程序 wechatminiprogram/login 通道，无需抓包）、
      必要时自动绑定手机号、每日签到领油量、周周签到抽奖，并推送油量与连签天数。

作者：lcmovie https://github.com/lcmovie
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("❌ 缺少依赖：pip install requests")
    sys.exit(1)

APP_NAME = "神州车友会签到"

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
APP_NO = "wx645266d7bda741c9"          # 小程序 AppID
BASE_URL = "https://cgi.qswnet.com"    # 小程序接口域名
BIZ_APP_ID = 1006                      # 业务侧 APP_ID（登录必填）
BIZ_COMPANY_ID = 1005                  # 业务侧 Company_ID

API_LOGIN = "/user/login"                                               # 手机号绑定/登录
API_D240925 = "/user/zuche/wechat-miniprogram/user/d240925-car-friend"   # 日签/油量
API_WEEKLY = "/user/zuche/d260714-checkin-ac"                           # 周周签到抽奖

HERE = os.path.dirname(os.path.abspath(__file__))

YYB_TIMEOUT = float(os.getenv("YYB_REQUEST_TIMEOUT", "40") or 40)
QSW_TIMEOUT = float(os.getenv("QSW_REQUEST_TIMEOUT", "25") or 25)
DRY_RUN = os.getenv("QSW_DRY_RUN", "0").strip().lower() in ("1", "true", "yes", "on")
LOGIN_RETRY = max(1, int(os.getenv("QSW_LOGIN_RETRY", "3") or 3))
BIND_PHONE = os.getenv("QSW_BIND_PHONE", "1").strip().lower() not in ("0", "false", "no", "off")
ENABLE_LOTTERY = os.getenv("QSW_ENABLE_LOTTERY", "1").strip().lower() not in ("0", "false", "no", "off")
DEBUG = os.getenv("QSW_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on")

# --------------------------------------------------------------------------- #
# 随机请求头（不依赖任何抓包数据）
# --------------------------------------------------------------------------- #
RANDOM_HEADERS = os.getenv("QSW_RANDOM_HEADERS", "1").strip().lower() not in ("0", "false", "no", "off")
UA_MODE = os.getenv("QSW_UA_MODE", "request").strip().lower()      # request | run | fixed
FIXED_UA = os.getenv("QSW_USER_AGENT", "").strip()
FIXED_REFERER_VER = os.getenv("QSW_REFERER_VER", "").strip()

UA_POOL = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.75(0x18004b21) NetType/WIFI Language/zh_CN",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x18003123) NetType/WIFI Language/zh_CN",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50(0x18003223) NetType/4G Language/zh_CN",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.53(0x18003528) NetType/WIFI Language/zh_CN",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.55(0x18003728) NetType/WIFI Language/zh_CN",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_7_9 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.42(0x18002a2f) NetType/WIFI Language/zh_CN",
    "Mozilla/5.0 (Linux; Android 13; PGT110 Build/TKQ1.220829.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36 XWEB/1160065 MMWEBSDK/20231202 MicroMessenger/8.0.49.2600(0x28003133) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64",
    "Mozilla/5.0 (Linux; Android 14; V2309A Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/122.0.6261.120 Mobile Safari/537.36 XWEB/1220093 MMWEBSDK/20240301 MicroMessenger/8.0.50.2701(0x2800323D) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64",
    "Mozilla/5.0 (Linux; Android 12; 2201123C Build/SP1A.210812.016; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/105.0.5195.136 Mobile Safari/537.36 XWEB/1050263 MMWEBSDK/20221206 MicroMessenger/8.0.32.2300(0x2800203B) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090b19) XWEB/11581",
]
REFERER_VER_RANGE = (108, 122)
ACCEPT_LANGUAGE_POOL = ["zh_CN", "zh-CN", "zh-Hans-CN", "zh_CN,zh;q=0.9", "zh-CN,zh;q=0.9,en;q=0.8"]

_RUN_UA = ""


def pick_user_agent() -> str:
    global _RUN_UA
    if UA_MODE == "fixed" or FIXED_UA:
        return FIXED_UA or UA_POOL[0]
    if not RANDOM_HEADERS:
        return UA_POOL[0]
    if UA_MODE == "run":
        if not _RUN_UA:
            _RUN_UA = random.choice(UA_POOL)
        return _RUN_UA
    return random.choice(UA_POOL)


def pick_referer() -> str:
    if FIXED_REFERER_VER:
        ver = FIXED_REFERER_VER
    elif RANDOM_HEADERS:
        ver = str(random.randint(*REFERER_VER_RANGE))
    else:
        ver = "117"
    return f"https://servicewechat.com/{APP_NO}/{ver}/page-frame.html"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def mask_phone(value: Any) -> str:
    text = str(value or "")
    return f"{text[:3]}****{text[-4:]}" if len(text) == 11 else (text or "-")


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def preview(data: Any, limit: int = 300) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False)
    except Exception:
        text = str(data)
    return text[:limit]


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def banner(lines: List[str]) -> None:
    print("=" * 78)
    for line in lines:
        print(line)
    print("=" * 78)


def deep_values(node: Any, key: str, out: Optional[List[Any]] = None) -> List[Any]:
    """递归收集所有指定 key 的值。"""
    if out is None:
        out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, (str, int, float)):
                out.append(v)
            deep_values(v, key, out)
    elif isinstance(node, list):
        for item in node:
            deep_values(item, key, out)
    return out


def first_str(node: Any, key: str) -> str:
    for value in deep_values(node, key):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# --------------------------------------------------------------------------- #
# 账号配置
# --------------------------------------------------------------------------- #
class AccountTarget:
    def __init__(self, endpoint: str = "", ref: str = "", index: int = 0):
        self.endpoint = normalize_endpoint(endpoint) if endpoint else ""
        self.ref = str(ref or "")
        self.index = index
        self.label_suffix = ""

    @property
    def yyb_base(self) -> str:
        return self.endpoint

    @property
    def label(self) -> str:
        number = self.ref if self.ref else str(self.index or "?")
        suffix = f"（{self.label_suffix}）" if self.label_suffix else ""
        return f"账号 {number}{suffix}"

    @property
    def server(self) -> str:
        return f"{self.endpoint}@{self.ref}" if self.endpoint and self.ref else "未配置 YYB_SERVER"


def normalize_endpoint(value: str) -> str:
    text = (value or "").strip().rstrip("/")
    if not text:
        return ""
    if not re.match(r"^https?://", text, re.I):
        text = "http://" + text
    return text


def load_accounts() -> List[AccountTarget]:
    raw = os.getenv("YYB_SERVER", "").strip()
    accounts: List[AccountTarget] = []
    bare_servers: List[str] = []
    if raw:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "@" not in line:
                # 纯服务地址行：记录后走 /accounts 自动发现
                bare_servers.append(line)
                continue
            endpoint, ref = (part.strip() for part in line.rsplit("@", 1))
            if not endpoint or not ref:
                print(f"⚠️ [配置] 忽略不完整行：{line}")
                continue
            accounts.append(AccountTarget(endpoint=endpoint, ref=ref, index=len(accounts) + 1))
    for server in dict.fromkeys(bare_servers):
        provider = YybCodeProvider(server)
        found = provider.list_accounts()
        if not found:
            print(f"⚠️ [配置] {provider.server} /accounts 未解析到账号（请确认 YYB 服务在线且已授权）")
            continue
        for item in found:
            accounts.append(AccountTarget(endpoint=provider.server, ref=item.ref, index=len(accounts) + 1))
    if not accounts:
        raise RuntimeError(
            "未读取到有效账号。请配置环境变量 YYB_SERVER，支持两种格式：\n"
            "  YYB地址@账号ID      例：http://192.168.1.10:8000@1（多账号多行）\n"
            "  只填服务地址        例：http://192.168.1.10:8000（自动发现全部账号）"
        )
    return accounts


# --------------------------------------------------------------------------- #
# YYB 调用
# --------------------------------------------------------------------------- #
# YYB CodeProvider 模式
# --------------------------------------------------------------------------- #
# 服务地址优先级：WECHAT_SERVER 环境变量 > YYB_SERVER（地址@ref 行，兼容旧配置）
WECHAT_SERVER = os.getenv("WECHAT_SERVER", "").strip()
SERVERS = [s.strip() for s in WECHAT_SERVER.split(",") if s.strip()]

YYB_REFS: List[str] = []  # 空=自动从 /accounts 获取，非空=只用这里指定的 ref

CODE_RETRY_TIMES = 3      # 获取 code 最大重试次数
RETRY_BASE_DELAY = 3      # 指数退避基数（秒）


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
        self.server = (server or "").strip().rstrip("/")
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
            response = self.session.get(url, timeout=YYB_TIMEOUT)
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
        payload = {"app_id": APP_NO, "ref": account.ref}
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "okhttp/3.12.13"}
        api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
        if api_key:
            headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key

        for attempt in range(1, CODE_RETRY_TIMES + 1):
            try:
                response = self.session.post(
                    url, json=payload, headers=headers, timeout=YYB_TIMEOUT
                )
                data = json_or_text(response)
                code = extract_code(data)
                if code and len(code) >= 16:
                    return code
                print(f"{account.label} 获取 code 为空 resp={str(data)[:160]}")
            except Exception as exc:
                print(f"{account.label} 获取 code 异常：{str(exc)[:120]}")

            if attempt < CODE_RETRY_TIMES:
                time.sleep(RETRY_BASE_DELAY * attempt)

        return None
# =================================================


# --------------------------------------------------------------------------- #
# YYB 调用
# --------------------------------------------------------------------------- #
def yyb_post(account: AccountTarget, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = account.yyb_base + path
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "okhttp/3.12.13"}
    api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
    if api_key:
        headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key
    resp = requests.post(url, json=payload, headers=headers, timeout=YYB_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if DEBUG:
        print(f"    · [YYB] {path} → {preview(body, 400)}")
    if isinstance(body, dict) and body.get("code") not in (0, None):
        raise RuntimeError(f"YYB {path} 返回错误：{body.get('msg') or preview(body)}")
    return body


# provider 上下文：main 收集账号时注册
_PROVIDER_CONTEXT: Dict[str, Tuple[YybCodeProvider, CodeAccount]] = {}


def yyb_get_wx_code(account: AccountTarget) -> str:
    """取 wx.login code（一次性，拿到后要尽快用于登录）。provider 可用时走 provider。"""
    context = _PROVIDER_CONTEXT.get(account.ref)
    if context is not None:
        provider, code_account = context
        code = provider.get_code(code_account)
        if code:
            return code
        raise RuntimeError("YYB 未返回有效 code（provider 重试耗尽）")
    body = yyb_post(account, "/wxapp/getCode", {"ref": account.ref, "app_id": APP_NO})
    data = body.get("data") or {}
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    for candidate in (result.get("code"), data.get("code"), first_str(body, "code")):
        if isinstance(candidate, str) and len(candidate) >= 16:
            return candidate
    raise RuntimeError(f"YYB 未返回有效 code：{preview(body, 200)}")


def yyb_get_crypto_key(account: AccountTarget) -> Dict[str, Any]:
    """取用户加密密钥（wx.getUserCryptoManager().getLatestUserKey 的协议实现）。"""
    payload = {
        "ref": account.ref,
        "app_id": APP_NO,
        "payload": {
            "api_name": "webapi_getuserencryptkey",
            "data": {"appid": APP_NO},
        },
    }
    body = yyb_post(account, "/wx/getlatestuserkey", payload)
    node: Any = (body.get("data") or {}).get("result")
    if isinstance(node, dict) and isinstance(node.get("data"), str):
        try:
            node = json.loads(node["data"])
        except Exception:
            pass
    elif isinstance(node, dict) and isinstance(node.get("data"), dict):
        node = node["data"]

    if not isinstance(node, dict):
        raise RuntimeError(f"密钥返回结构异常：{preview(body, 300)}")

    key = node.get("encrypt_key") or node.get("encryptKey") or ""
    iv = node.get("iv") or ""
    version = node.get("version")
    if not key or not iv or version in (None, ""):
        raise RuntimeError(f"密钥字段缺失（可能是该账号/小程序不支持）：{preview(node, 260)}")
    return {
        "encrypt_key": str(key),
        "iv": str(iv),
        "version": str(version),
        "expire_in": node.get("expire_in") or node.get("expireIn") or 0,
        "create_time": node.get("create_time") or 0,
    }


def yyb_get_phone_package(account: AccountTarget) -> Dict[str, str]:
    """取手机号授权加密包（对应小程序 wx getPhoneNumber 的返回值）。

    返回 encryptedData / iv，另附 code、cloud_id、mobile 备用。
    注意：必须与本次 wx.login 用同一个 ref，session_key 才对得上。
    """
    body = yyb_post(account, "/wxapp/getPhoneNumber", {"ref": account.ref, "app_id": APP_NO})
    result = (body.get("data") or {}).get("result")
    if isinstance(result, dict) and isinstance(result.get("data"), str):
        # 有的版本把真正结果再套一层 JSON 字符串
        try:
            inner = json.loads(result["data"])
            if isinstance(inner, dict) and (inner.get("encryptedData") or inner.get("encrypted_data")):
                result = inner
        except Exception:
            pass

    node: Any = result if isinstance(result, dict) else {}
    enc = node.get("encryptedData") or node.get("encrypted_data") or ""
    iv = node.get("iv") or node.get("IV") or ""
    if not enc or not iv:
        # 兜底：整棵树上找
        enc = enc or (first_str(body, "encryptedData") or "")
        iv = iv or (first_str(body, "iv") or "")
    if not enc or not iv:
        raise RuntimeError(f"YYB 未返回手机号加密包：{preview(body, 260)}")

    mobile = ""
    raw_data = node.get("data")
    if isinstance(raw_data, str) and raw_data.startswith("{"):
        try:
            mobile = json.loads(raw_data).get("mobile", "") or ""
        except Exception:
            pass
    mobile = mobile or (first_str(body, "mobile") or "")
    return {
        "encrypted_data": str(enc),
        "iv": str(iv),
        "code": str(node.get("code") or ""),
        "cloud_id": str(node.get("cloud_id") or ""),
        "mobile": str(mobile),
    }


# --------------------------------------------------------------------------- #
# 签名客户端
# --------------------------------------------------------------------------- #
class QswClient:
    """神州接口客户端：负责登录、签名与业务请求。"""

    def __init__(self) -> None:
        self.jwt = ""
        self.key_info: Dict[str, Any] = {}

    # ---- 会话 ---------------------------------------------------------- #
    def bootstrap_token(self) -> str:
        """POST /state/token → 匿名会话 JWT。"""
        body = self.request("POST", "/state/token", body={}, need_auth=False)
        token = body.get("data")
        if not isinstance(token, str) or not token:
            raise RuntimeError(f"/state/token 未返回 token：{preview(body, 200)}")
        self.jwt = token
        return token

    def adopt_token(self, token: str) -> None:
        self.jwt = (token or "").strip()

    # ---- 签名 ---------------------------------------------------------- #
    def set_crypto_key(self, key_info: Dict[str, Any]) -> None:
        self.key_info = key_info or {}

    def _signature(self, ts: str) -> str:
        info = self.key_info
        return md5(f"{info['encrypt_key']}{info['iv']}{info['version']}{ts}")

    def _headers(self, content_type: Optional[str]) -> Dict[str, str]:
        ts = str(int(time.time() * 1000))
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": random.choice(ACCEPT_LANGUAGE_POOL) if RANDOM_HEADERS else "zh_CN",
            "Connection": random.choice(["keep-alive", "close"]) if RANDOM_HEADERS else "keep-alive",
            "TS": ts,
            "User-Agent": pick_user_agent(),
            "Referer": pick_referer(),
        }
        if content_type:
            headers["Content-Type"] = content_type
        if self.key_info:
            headers["Crypto"] = self._signature(ts)
            headers["CryptoVersion"] = str(self.key_info.get("version", "3"))
        if self.jwt:
            headers["Authorization"] = "jwt " + self.jwt
        return headers

    # ---- 通用请求 ------------------------------------------------------ #
    def request(self, method: str, path: str, body: Any = None, raw_body: bool = False,
                need_auth: bool = True, quiet: bool = False) -> Dict[str, Any]:
        url = BASE_URL + path
        if raw_body:
            data = body if isinstance(body, (bytes, bytearray)) else b""
            content_type = "application/x-www-form-urlencoded"
        elif body is None:
            data = b""
            content_type = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode("utf-8")
            content_type = "application/json;charset=utf-8"

        headers = self._headers(content_type)
        if not need_auth:
            headers.pop("Authorization", None)

        resp = requests.request(method.upper(), url, data=data, headers=headers, timeout=QSW_TIMEOUT)
        text = resp.text
        if DEBUG:
            print(f"    · [{method.upper()} {path}] {resp.status_code} {preview(text, 400)}")
        try:
            payload = resp.json()
        except Exception:
            raise RuntimeError(f"{path} 返回非 JSON（HTTP {resp.status_code}）：{preview(text, 200)}")
        if not quiet and isinstance(payload, dict) and payload.get("flag") not in (0, None):
            payload["_error"] = payload.get("msg") or preview(payload, 160)
        return payload

    # ---- 业务接口 ------------------------------------------------------ #
    def wechat_login(self, wx_code: str) -> Dict[str, Any]:
        return self.request("POST", "/user/wechatminiprogram/login",
                            {"APP_ID": BIZ_APP_ID, "Code": wx_code,
                             "Create_Source": None, "Create_Inviter": None, "Params_Other": None})

    def bind_phone(self, encrypted_data: str, iv: str) -> Dict[str, Any]:
        """POST /user/login → 微信手机号加密包绑定手机号（拿到 User_ID）。

        与小程序「手机号快捷登录」按钮完全一致：{Bind, WeAppEncrypData, WeAppIV}。
        必须走 JSON 提交，否则服务端解析不到 body（会报 PhoneAuthCodeCheck:Phone）。
        """
        return self.request("POST", API_LOGIN, {
            "Bind": True,
            "WeAppEncrypData": encrypted_data,
            "WeAppIV": iv,
            "Params_Other": None,
            "Company_ID": BIZ_COMPANY_ID,
        })

    def whoami(self) -> Optional[Dict[str, Any]]:
        body = self.request("GET", "/state/data", quiet=True)
        user = (body.get("data") or {}).get("User") if isinstance(body.get("data"), dict) else None
        return user if isinstance(user, dict) else None

    def user_state(self) -> Dict[str, Any]:
        return self.request("POST", API_D240925 + "/user/state", raw_body=True)

    def checkin_calendar(self) -> Dict[str, Any]:
        return self.request("POST", API_D240925 + "/checkin/calendar", {"Date_Type": "Week"})

    def checkin_enter(self) -> Dict[str, Any]:
        return self.request("POST", API_D240925 + "/checkin/enter", raw_body=True)

    def weekly_state(self) -> Dict[str, Any]:
        return self.request("POST", API_WEEKLY + "/state", raw_body=True)

    def weekly_lottery(self) -> Dict[str, Any]:
        stamp = int(time.time() * 1000)
        return self.request("POST", API_WEEKLY + "/lottery",
                            {"Timestamp": stamp, "Inviter": 0,
                             "Randomcode": md5(f"{stamp}{random.random()}"),
                             "Channel_Code": f"c{stamp}"})


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #
def bind_phone(account: AccountTarget, client: QswClient) -> Tuple[Optional[Dict[str, Any]], str]:
    """取手机号加密包 → POST /user/login 绑定。返回 (User, 说明)。

    加密包必须与本次 wx.login 同源（同一 YYB ref），否则 session_key 对不上。
    """
    if not BIND_PHONE:
        return client.whoami(), "已跳过手机号绑定（QSW_BIND_PHONE=0）"
    try:
        pkg = yyb_get_phone_package(account)
    except Exception as exc:
        return None, f"获取手机号加密包失败：{exc}"

    if pkg.get("mobile"):
        print(f"    📱 YYB 已解密手机号：{mask_phone(pkg['mobile'])}")

    try:
        resp = client.bind_phone(pkg["encrypted_data"], pkg["iv"])
    except Exception as exc:
        return None, f"绑定手机号请求异常：{exc}"

    if resp.get("flag") != 0:
        return None, f"绑定手机号失败：{resp.get('msg') or preview(resp, 160)}"

    user = client.whoami()
    if user and user.get("User_ID"):
        return user, "手机号绑定成功"
    return user, "绑定接口返回成功，但 User_ID 仍未出现"


def login_account(account: AccountTarget, client: QswClient) -> Tuple[bool, str]:
    """YYB 取码 → 匿名会话 → 微信小程序登录 → （必要时）绑定手机号 → 校验身份。"""
    last_error = ""
    for attempt in range(1, LOGIN_RETRY + 1):
        try:
            wx_code = yyb_get_wx_code(account)
        except Exception as exc:
            last_error = f"取码失败：{exc}"
            print(f"    ⚠️ {last_error}")
            sleep(3)
            continue

        print(f"    🔑 [{attempt}/{LOGIN_RETRY}] wx.login code = {wx_code[:12]}…")
        try:
            client.bootstrap_token()
        except Exception as exc:
            last_error = f"获取匿名会话失败：{exc}"
            print(f"    ⚠️ {last_error}")
            sleep(2)
            continue

        try:
            result = client.wechat_login(wx_code)
        except Exception as exc:
            last_error = f"登录请求异常：{exc}"
            print(f"    ⚠️ {last_error}")
            sleep(3)
            continue

        if result.get("flag") == 0:
            user = client.whoami()
            if not user:
                last_error = "登录接口返回成功，但 /state/data 仍为匿名"
            elif user.get("Company_ID") != BIZ_COMPANY_ID or user.get("APP_ID") != BIZ_APP_ID:
                last_error = "商户/应用 ID 不匹配"
                print(f"    ⚠️ {last_error}")
            else:
                # 只有绑过手机号的 User 才有 User_ID；签到类接口以它为门槛
                if not user.get("User_ID"):
                    print("    📱 会话未绑定手机号，自动绑定…")
                    user, note = bind_phone(account, client)
                    if note:
                        print(f"       {note}")
                if user and user.get("User_ID"):
                    phone = mask_phone(user.get("Current_Phone") or user.get("Phone") or "")
                    account.label_suffix = phone
                    return True, phone
                last_error = "手机号未绑定成功（签到会被 flag:-3 NoPhone 拦住）"
        else:
            last_error = result.get("msg") or preview(result, 160)
            print(f"    ⚠️ 登录返回：{last_error}")

        # code 是一次性的，失败必须换新 code 重试
        sleep(3 + attempt)

    return False, last_error or "登录失败"


# --------------------------------------------------------------------------- #
# 业务逻辑
# --------------------------------------------------------------------------- #
def run_checkin(client: QswClient) -> Dict[str, Any]:
    """日签：先查日历，未签到才调 enter。"""
    out: Dict[str, Any] = {"calendar": "-", "reward": "-", "need_sign": False,
                           "sign_result": "-", "success": False, "error": ""}

    state = client.user_state()
    checkin = (state.get("data") or {}).get("CheckIn") or {}
    oil = (state.get("data") or {}).get("Oil_Tank") or {}
    out["oil"] = f"{oil.get('Current', '-')}/{oil.get('Limit', '-')}"
    out["streak"] = str(checkin.get("Continue_Days", "-"))
    out["total_days"] = str(checkin.get("Total_Days", "-"))

    try:
        cal = client.checkin_calendar()
        data = cal.get("data") or {}
        day_count = data.get("DayCount")
        signed = data.get("DayCount_SignIn")
        out["calendar"] = f"{signed}/{day_count}" if day_count is not None else "-"
        today_signed = bool(data.get("Today_SignIn", checkin.get("Is_Today_CheckIn")))
    except Exception as exc:
        out["error"] = f"查询签到日历失败：{exc}"
        print(f"    ⚠️ {out['error']}")
        today_signed = bool(checkin.get("Is_Today_CheckIn"))

    if today_signed:
        out["success"] = True
        out["sign_result"] = "今日已签到"
        return out

    out["need_sign"] = True
    if DRY_RUN:
        out["success"] = True
        out["sign_result"] = "DRY_RUN：跳过签到"
        return out

    print("    🎯 今日未签到，执行 checkin/enter …")
    try:
        result = client.checkin_enter()
    except Exception as exc:
        out["error"] = f"签到请求异常：{exc}"
        return out

    flag = result.get("flag")
    msg = result.get("msg") or ""
    if flag == 0:
        out["success"] = True
        out["sign_result"] = "签到成功"
        data = result.get("data") or {}
        reward = data.get("Today_Receive_Rewards") or {}
        if reward:
            out["reward"] = " ".join(f"{k}+{v}" for k, v in reward.items())
        print(f"    ✅ 签到成功　奖励：{out['reward']}")
    elif "已签到" in msg:
        out["success"] = True
        out["sign_result"] = f"已签到（{msg}）"
    else:
        out["sign_result"] = msg or preview(result, 160)
        out["error"] = explain_failure(msg)
        print(f"    ❌ 签到失败：{out['sign_result']}　{out['error']}")

    # 重新读取一次状态，拿到最新油量与连签
    try:
        state2 = client.user_state()
        oil2 = (state2.get("data") or {}).get("Oil_Tank") or {}
        checkin2 = (state2.get("data") or {}).get("CheckIn") or {}
        out["oil"] = f"{oil2.get('Current', out.get('oil'))}/{oil2.get('Limit', '-')}"
        out["streak"] = str(checkin2.get("Continue_Days", out.get("streak")))
        out["total_days"] = str(checkin2.get("Total_Days", out.get("total_days")))
    except Exception:
        pass
    return out


def run_weekly(client: QswClient) -> Dict[str, Any]:
    """周周签到活动：state → 未领则 lottery。"""
    out: Dict[str, Any] = {"weekly": "-", "lottery": "-", "error": ""}
    try:
        state = client.weekly_state()
    except Exception as exc:
        out["weekly"] = "查询失败"
        out["error"] = str(exc)
        return out

    if state.get("flag") != 0:
        msg = state.get("msg") or ""
        if "NoPhone" in msg or "NoLogin" in msg:
            out["weekly"] = "未登录"
        else:
            out["weekly"] = msg or "无活动"
        return out

    data = state.get("data") or {}
    received = data.get("ReceiveCount", 0)
    today = bool(data.get("IsReceiveToday"))
    allow = bool(data.get("Allow_Lottery"))
    out["weekly"] = f"已领 {received} 次" + ("（今日已领）" if today else "")

    if DRY_RUN:
        out["lottery"] = "DRY_RUN：跳过"
        return out

    if today and not allow:
        out["lottery"] = "今日已领"
        return out

    try:
        result = client.weekly_lottery()
    except Exception as exc:
        out["lottery"] = f"抽奖异常：{exc}"
        return out

    flag = result.get("flag")
    msg = result.get("msg") or ""
    if flag == 0:
        prize = (result.get("data") or {}).get("Prize") or []
        names = []
        for item in prize if isinstance(prize, list) else []:
            if isinstance(item, dict):
                names.append(str(item.get("Prize_Name") or item.get("Name") or item))
            else:
                names.append(str(item))
        out["lottery"] = "抽奖成功" + (f"：{'、'.join(names)}" if names else "")
        print(f"    🎁 周周抽奖：{out['lottery']}")
    else:
        out["lottery"] = msg or preview(result, 120)
        if "sign error" in msg:
            out["error"] = explain_failure(msg)
    return out


GUARD_HINTS = {
    "No params": "缺少 TS / Crypto 请求头",
    "No sign": "CryptoVersion 不受支持",
    "sign error": "签名校验失败：Crypto 与 encryptKey/iv/version + TS 不匹配",
    "ts timeout": "时间戳超出服务端窗口（检查机器时间/时区）",
    "NoLogin": "会话未登录",
    "NoPhone": "会话未绑定手机号 —— 自动绑定失败，检查 YYB /wxapp/getPhoneNumber 是否可用",
}


def explain_failure(message: str) -> str:
    text = message or ""
    for keyword, hint in GUARD_HINTS.items():
        if keyword in text:
            return hint
    return ""


def run_account(index: int, total: int, account: AccountTarget) -> Dict[str, Any]:
    print()
    print("─" * 78)
    print(f"▶ [{index}/{total}] {account.label}　来源：{account.server}")
    result: Dict[str, Any] = {
        "label": account.label, "server": account.server, "user": "-",
        "oil": "-", "streak": "-", "total_days": "-", "calendar": "-", "reward": "-",
        "weekly": "-", "lottery": "-", "success": False, "need_sign": False, "error": "",
    }

    client = QswClient()

    # ① 签名密钥（写接口必需）
    try:
        key_info = yyb_get_crypto_key(account)
        client.set_crypto_key(key_info)
        print(f"    🔐 密钥就绪 version={key_info['version']}（剩余约 "
              f"{int(key_info.get('expire_in') or 0) // 60} 分钟）")
    except Exception as exc:
        result["error"] = f"获取签名密钥失败：{exc}"
        print(f"    ❌ {result['error']}")
        return result

    # ② 登录
    logged, user_info = login_account(account, client)
    result["user"] = user_info
    result["label"] = account.label
    if not logged:
        result["error"] = f"登录失败：{user_info}"
        print(f"    ❌ {result['error']}")
        return result
    print(f"    ✅ 登录成功　{user_info}")

    # ③ 日签
    try:
        checkin = run_checkin(client)
        result.update({k: checkin.get(k, result.get(k)) for k in
                       ("oil", "streak", "total_days", "calendar", "reward")})
        result["need_sign"] = checkin.get("need_sign", False)
        result["success"] = bool(checkin.get("success"))
        if checkin.get("error"):
            result["error"] = checkin["error"]
        print(f"    📅 签到：{checkin.get('sign_result')}　本周 {result['calendar']}　"
              f"连签 {result['streak']} 天　油量 {result['oil']}")
    except Exception as exc:
        result["error"] = f"签到流程异常：{exc}"
        print(f"    ❌ {result['error']}")

    # ④ 周周活动
    if ENABLE_LOTTERY:
        try:
            weekly = run_weekly(client)
            result["weekly"] = weekly.get("weekly", "-")
            result["lottery"] = weekly.get("lottery", "-")
            if not result["error"] and weekly.get("error"):
                result["error"] = weekly["error"]
            print(f"    🎲 周周：{result['weekly']}　抽奖：{result['lottery']}")
        except Exception as exc:
            result["weekly"] = f"异常：{exc}"

    return result


# --------------------------------------------------------------------------- #
# 通知与报告
# --------------------------------------------------------------------------- #
def _icon(item: Dict[str, Any]) -> str:
    if item.get("success"):
        return "✅"
    if item.get("user") == "-":
        return "❌"
    return "⚠️"


def build_report(results: List[Dict[str, Any]]) -> str:
    lines = [f"🕒 {now_text()}",
             f"🧪 模式：{'DRY_RUN（只查询）' if DRY_RUN else '正常签到'}",
             ""]
    for item in results:
        lines.append(f"{_icon(item)} {item['label']}　{item.get('user', '-')}")
        if item.get("need_sign"):
            lines.append(f"   📅 今日未签到 → 已尝试补签")
        lines.append(f"   📊 本周 {item.get('calendar', '-')}　连签 {item.get('streak', '-')} 天"
                     f"　累计 {item.get('total_days', '-')} 天")
        lines.append(f"   ⛽ 油量 {item.get('oil', '-')}"
                     + (f"　🎁 奖励 {item['reward']}" if item.get("reward") not in ("-", "", None) else ""))
        if item.get("weekly") not in ("-", "", None):
            lines.append(f"   🎲 周周：{item['weekly']}　抽奖：{item.get('lottery', '-')}")
        if item.get("error"):
            lines.append(f"   ⚠️ {item['error']}")
        lines.append("")
    ok = sum(1 for i in results if i.get("success"))
    lines.append(f"🏁 汇总：{ok} 成功 / {len(results) - ok} 失败（共 {len(results)} 个账号）")
    return "\n".join(lines)


def load_notify():
    """兼容青龙各版本的 notify.py 位置（订阅子目录 / 脚本目录 / 根目录）。

    用 importlib 直接按文件加载，避免 `import notify` 受 sys.path 顺序影响。
    """
    candidates = [
        Path(HERE) / "notify.py",
        Path("/ql/data/scripts/notify.py"),
        Path("/ql/scripts/notify.py"),
        Path("/ql/data/notify.py"),
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("qsw_qinglong_notify", path)
            module = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(module)
            for name in ("send", "sendNotify"):
                func = getattr(module, name, None)
                if callable(func):
                    return func
        except Exception as exc:
            print(f"⚠️ [通知] 加载 {path} 失败：{preview(exc, 120)}")
    return None


def send_notify(title: str, content: str) -> None:
    """优先使用青龙内置 notify.py，未配置时回落常见推送通道。"""
    sender = load_notify()
    if sender is not None:
        try:
            sender(title, content)
            print("✅ [通知] 已通过青龙通知模块发送")
            return
        except Exception as exc:
            print(f"⚠️ [通知] 青龙通知发送失败（不影响签到结果）：{preview(exc, 120)}")
    else:
        print("⚠️ [通知] 未找到青龙 notify.py，尝试兜底通道")

    sent = False
    plusplus = os.getenv("PLUSPLUS_TOKEN", "").strip()
    if plusplus:
        try:
            requests.post("https://www.pushplus.plus/send",
                          json={"token": plusplus, "title": title, "content": content,
                                "template": "txt"}, timeout=15)
            print("✅ [通知] PushPlus 发送成功")
            sent = True
        except Exception as exc:
            print(f"❌ [通知] PushPlus 发送失败：{exc}")

    server_push = os.getenv("PUSH_KEY", "").strip() or os.getenv("SERVERPUSHKEY", "").strip()
    if server_push and not sent:
        try:
            requests.post(f"https://sctapi.ftqq.com/{server_push}.send",
                          data={"title": title, "desp": content}, timeout=15)
            print("✅ [通知] Server 酱发送成功")
            sent = True
        except Exception as exc:
            print(f"❌ [通知] Server 酱发送失败：{exc}")

    qywx = os.getenv("QYWX_KEY", "").strip() or os.getenv("QYWX_TOKEN", "").strip()
    if qywx and not sent:
        try:
            requests.post(f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={qywx}",
                          json={"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}},
                          timeout=15)
            print("✅ [通知] 企业微信机器人发送成功")
            sent = True
        except Exception as exc:
            print(f"❌ [通知] 企业微信机器人发送失败：{exc}")

    bark = os.getenv("BARK_PUSH", "").strip()
    if bark and not sent:
        try:
            requests.post(bark.rstrip("/"), json={"title": title, "body": content}, timeout=15)
            print("✅ [通知] Bark 发送成功")
            sent = True
        except Exception as exc:
            print(f"❌ [通知] Bark 发送失败：{exc}")

    if not sent:
        print("ℹ️ [通知] 未配置任何可用推送通道，结果仅输出到日志")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    banner([
        f"🚗 {APP_NAME}",
        f"🕒 启动时间: {now_text()}",
        f"🆔 小程序  : {APP_NO}",
        f"🧪 DRY_RUN : {'开启（只查询）' if DRY_RUN else '关闭'}",
        f"🔐 登录    : YYB 自动取码 → user/wechatminiprogram/login（无需抓包）",
        f"📱 手机号  : " + ("YYB 加密包 → user/login 自动绑定（拿到 User_ID）"
                            if BIND_PHONE else "已关闭自动绑定（QSW_BIND_PHONE=0）"),
        f"✍️ 签名    : MD5(encryptKey + iv + version + TS)，密钥由 YYB 转发微信协议获取",
        f"🎭 请求头  : " + (f"随机（UA {len(UA_POOL)} 套 / Referer {REFERER_VER_RANGE[0]}-{REFERER_VER_RANGE[1]}）"
                           if RANDOM_HEADERS else "固定"),
    ])

    # Step 1: 先收集账号（WECHAT_SERVER /accounts 优先，YYB_SERVER 地址@ref 行兼容回退）
    accounts: List[AccountTarget] = []
    _PROVIDER_CONTEXT.clear()
    for server in SERVERS:
        provider = YybCodeProvider(server)
        for item in provider.list_accounts():
            target = AccountTarget(endpoint=provider.server, ref=item.ref, index=len(accounts) + 1)
            _PROVIDER_CONTEXT[item.ref] = (provider, item)
            accounts.append(target)
    if not accounts:
        try:
            accounts = load_accounts()
        except Exception as exc:
            print(f"❌ [配置] {exc}")
            send_notify(f"❌ {APP_NAME} 配置错误", str(exc))
            return 1

    print(f"✅ 共读取到 {len(accounts)} 个账号")
    for account in accounts:
        print(f"   · {account.label}　←　{account.server}")

    results: List[Dict[str, Any]] = []
    for index, account in enumerate(accounts, 1):
        try:
            results.append(run_account(index, len(accounts), account))
        except Exception as exc:
            print(f"❌ [主程序] {account.label} 执行异常：{exc}")
            results.append({
                "label": account.label, "server": account.server, "user": "-",
                "oil": "-", "streak": "-", "total_days": "-", "calendar": "-",
                "reward": "-", "weekly": "-", "lottery": "-",
                "success": False, "need_sign": False, "error": str(exc),
            })
        if index < len(accounts):
            sleep(random.uniform(3.0, 6.0))

    report = build_report(results)
    print()
    print(report)

    prefix = "🧪 " if DRY_RUN else ""
    send_notify(f"{prefix}{APP_NAME} {now_text()}", report)

    success = sum(1 for item in results if item["success"])
    return 0 if success == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n⏹️ 已手动中断")
        sys.exit(130)
