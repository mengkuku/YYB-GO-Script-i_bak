#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 中华保
# cron: 27 8 * * *
"""
name: 中华保签到
cron: 27 8 * * *

中华保（中华财险）微信小程序每日签到 + 华宝乐园任务自动完成，基于 YYB-Go-Enhanced 自动取码，
全程无需抓包、无需手动配置任何 token。

青龙环境变量：
  YYB_SERVER        必填，YYB-Go-Enhanced 地址@微信账号标识，多账号每行一条
                    例：yyb-go:8000@1（第 2 个账号写 @2，以此类推）
  YYB_API_KEY       可选，对应服务端 YYB_PROTOCOL_TOKEN，设置后带 Authorization 头
  YYB_REQUEST_TIMEOUT 可选，YYB 接口超时秒数，默认 40
  ZHB_DRY_RUN       可选，=1 只查询签到状态与任务列表，不做任何写操作
  ZHB_ENABLE_SIGN   可选，默认 1；=0 跳过签到
  ZHB_ENABLE_TASK   可选，默认 1；=0 跳过华宝乐园任务
  ZHB_TASK_TYPES    可选，默认 all（全部类型都尝试）；也可用逗号分隔指定，如 1,2,6,11
                    1=浏览产品 2=连续签到 6=订阅通知 11=每日答题
                    4=添加企微管家 5=关注公众号：服务端 completedTask 明确拒绝这两类
                    （返回「不支持的任务类型」），脚本会先调用其官方「去完成」上报接口
                    （toComplete / preFollowPublic）再尝试，最终仍需真人扫码/关注
  ZHB_TASK_DELAY    可选，任务之间间隔秒数，默认 1.2（避免请求过于密集）
  ZHB_AUTO_REGISTER 可选，默认 0。=0 时「未注册」的微信账号直接跳过、日志标注未注册；
                    =1 才会走手机号授权通道自动注册（会真的注册新账号，谨慎开启）
  ZHB_DEBUG         可选，=1 打印每个接口的请求与响应明细

依赖：requests（仓库 requirements.txt 已声明）
通知：优先使用青龙内置 notify.py；未配置时回落到 PushPlus / Server酱 / 企业微信 / Bark。
      通知失败不影响任务结果。
功能：YYB 自动取码 → getOpenId 换取登录 token → 华宝乐园每日签到 →
      自动完成可脚本化的积分任务 → 领取任务积分，并推送签到与积分结果。
      未注册的账号不注册、不写数据，仅在结果中标注「未注册（已跳过）」。
      任务结果按「完成 N 个 / 失败 M 个」统计；未实名账号会被服务端拦下，
      日志中直接写明「未实名，任务全部失败」。积分显示为「基数 → 完成后（+增量）」。

作者：lcmovie https://github.com/lcmovie
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import random
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

try:
    import requests
except ImportError:
    print("❌ 缺少依赖：pip install requests")
    sys.exit(1)

APP_NAME = "中华保签到"

# --------------------------------------------------------------------------- #
# 常量（均来自小程序包静态代码，不随运行变化）
# --------------------------------------------------------------------------- #
APP_NO = "wx16ad5860375f084d"                       # 小程序 AppID
BASE_URL = "https://sfa.cic.cn"                     # 接口域名（api.base = api.NewBase）
API_APP_ID = "4172b5dbae0c11ebb57c0242ac110003"     # 请求头 appId（包内硬编码）
APP_VERSION = "3.13.17"                             # 小程序 globalData.version

API_GET_OPENID = "/miniprogram/api/user/v2/getOpenId"
API_LOGIN_REG = "/miniprogram/api/user/v3/loginAndRegistered"
API_HOME = "/miniprogram/api/huabaopark/v6/getHomePage"
API_COMPLETE_TASK = "/miniprogram/api/huabaopark/v6/completedTask"
API_RECEIVE_TASK = "/miniprogram/api/huabaopark/v6/receiveTaskIntegral"
API_SIGN = "/miniprogram/api/integral/v2/sign"
API_SIGN_INFO = "/miniprogram/api/integral/v2/getSignInfo"
# 「去完成」上报通道：4=添加企微管家（eventId=1002）
API_TO_COMPLETE = "/miniprogram/api/integral/v2/toComplete"
# 关注公众号相关：5=关注公众号（eventId=1001）
API_PRE_FOLLOW = "/miniprogram/api/v6/CommonLottery/preFollowPublic"
API_IS_FOLLOW = "/miniprogram/api/v6/common/isFollowPublic"
# 管家绑定关系查询
API_MANAGER_INFO = "/miniprogram/api/userinfo/v3/managerInfo/get"

# 每日答题题库（小程序把题库放静态 OSS，题目与正确答案都在里面）
QUIZ_BANK_URL = ("https://static-cdn.property.cic.cn/prod-platform-lamp/common/fe-assets/"
                 "2025-08/14c08f48-568b-4dd2-b7ff-aca2250342c7.json")
QUIZ_EPOCH = datetime(2025, 10, 10)   # 题库起始日，索引 = (today - QUIZ_EPOCH).days % 题量

# 签名密钥（复现小程序 utils/secretutil/getSecretKey.js 的静态算法）
_SCRM_E1 = "00031006001313361013031210021302001210121116"
_SCRM_E2 = "00130013111313361013031210021302001210121116"
_SCRM_SALT_SECRET = "c2788241f23dbf618139c802a2c28f4cc28d81428188"
_SCRM_SALT_SECRET_KEY = "c178f2f9b1fdbf618139c802a2c28f4cc28d81428188"

# 任务类型（小程序 taskTypeEnum / taskTypeEnum 对应关系）
TASK_TYPE_NAME = {
    1: "浏览产品", 2: "连续签到", 3: "上传车辆图片", 4: "添加专属管家", 5: "关注公众号",
    6: "活动类订阅通知", 7: "车辆保险续期提醒", 8: "非车辆保险续期提醒", 9: "非车辆保险任务",
    10: "车辆保险报价", 11: "每日答题",
}

HERE = os.path.dirname(os.path.abspath(__file__))

YYB_TIMEOUT = float(os.getenv("YYB_REQUEST_TIMEOUT", "40") or 40)
ZHB_TIMEOUT = float(os.getenv("ZHB_REQUEST_TIMEOUT", "25") or 25)
DRY_RUN = os.getenv("ZHB_DRY_RUN", "0").strip().lower() in ("1", "true", "yes", "on")
ENABLE_SIGN = os.getenv("ZHB_ENABLE_SIGN", "1").strip().lower() not in ("0", "false", "no", "off")
ENABLE_TASK = os.getenv("ZHB_ENABLE_TASK", "1").strip().lower() not in ("0", "false", "no", "off")
AUTO_REGISTER = os.getenv("ZHB_AUTO_REGISTER", "0").strip().lower() in ("1", "true", "yes", "on")
DEBUG = os.getenv("ZHB_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on")
TASK_DELAY = float(os.getenv("ZHB_TASK_DELAY", "1.2") or 1.2)

_DEFAULT_TASK_TYPES = "all"


def parse_task_types():
    """返回允许自动完成的任务类型集合；None 表示「全部尝试」。"""
    raw = (os.getenv("ZHB_TASK_TYPES", _DEFAULT_TASK_TYPES) or _DEFAULT_TASK_TYPES).strip()
    if raw.lower() in ("all", "*", "全部", ""):
        return None
    result = set()
    for piece in re.split(r"[,，\s]+", raw):
        if piece.isdigit():
            result.add(int(piece))
    return result or None


TASK_TYPES = parse_task_types()

# --------------------------------------------------------------------------- #
# 随机请求头
# --------------------------------------------------------------------------- #
UA_POOL = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.53(0x18003528) NetType/WIFI Language/zh_CN",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.55(0x18003728) NetType/WIFI Language/zh_CN",
    "Mozilla/5.0 (Linux; Android 14; V2309A Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/122.0.6261.120 Mobile Safari/537.36 XWEB/1220093 MMWEBSDK/20240301 MicroMessenger/8.0.50.2701(0x2800323D) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64",
    "Mozilla/5.0 (Linux; Android 13; PGT110 Build/TKQ1.220829.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36 XWEB/1160065 MMWEBSDK/20231202 MicroMessenger/8.0.49.2600(0x28003133) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090b19) XWEB/11581",
]
_RUN_UA = ""


def pick_user_agent() -> str:
    global _RUN_UA
    if not _RUN_UA:
        _RUN_UA = random.choice(UA_POOL)
    return _RUN_UA


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_text() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def mask_phone(value: Any) -> str:
    text = str(value or "")
    return f"{text[:3]}****{text[-4:]}" if len(text) == 11 else (text or "-")


def keep_name(value: Any) -> str:
    """姓名脱敏：只保留姓氏。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return text[0] + "*" * (len(text) - 1)


def preview(data: Any, limit: int = 300) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False)
    except Exception:
        text = str(data)
    return text[:limit]


def banner(lines: List[str]) -> None:
    print("=" * 78)
    for line in lines:
        print(line)
    print("=" * 78)


def deep_first(node: Any, key: str) -> Any:
    """在嵌套结构里找第一个非空 key 值。"""
    if isinstance(node, dict):
        if node.get(key) not in (None, ""):
            return node[key]
        for value in node.values():
            found = deep_first(value, key)
            if found not in (None, ""):
                return found
    elif isinstance(node, list):
        for item in node:
            found = deep_first(item, key)
            if found not in (None, ""):
                return found
    return None


# --------------------------------------------------------------------------- #
# 签名密钥复现
# --------------------------------------------------------------------------- #
def _xor85(hex_text: str) -> str:
    return "".join(chr(85 ^ int(hex_text[i:i + 2], 16)) for i in range(0, len(hex_text), 2))


def _interleave(left: str, right: str) -> str:
    return "".join(left[i] + right[i] for i in range(len(left)))


def _b64_to_text(text: str) -> str:
    raw = base64.b64decode(text + "=" * (-len(text) % 4))
    return "".join(chr(b) for b in raw)


SIGN_SECRET = _b64_to_text(_xor85(_interleave(_SCRM_E1, _SCRM_SALT_SECRET)))
SIGN_SECRET_KEY = _b64_to_text(_xor85(_interleave(_SCRM_E2, _SCRM_SALT_SECRET_KEY)))


def make_signature(path_with_query: str, body_str: str, nonce: str, ts: str, token: str) -> str:
    """signature = Base64(HMAC_SHA256(url_path + body + nonce + timestamp + token, secret))"""
    message = f"{path_with_query}{body_str}{nonce}{ts}{token}"
    digest = hmac.new(SIGN_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


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
    if raw:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "@" not in line:
                print(f"⚠️ [配置] 忽略无账号标识的行：{line}")
                continue
            endpoint, ref = (part.strip() for part in line.rsplit("@", 1))
            if not endpoint or not ref:
                print(f"⚠️ [配置] 忽略不完整行：{line}")
                continue
            accounts.append(AccountTarget(endpoint=endpoint, ref=ref, index=len(accounts) + 1))
    if not accounts:
        raise RuntimeError(
            "未读取到有效账号。请配置环境变量 YYB_SERVER，每行一个账号，格式：\n"
            "  YYB地址@账号ID      例：yyb-go:8000@1\n"
            "  多账号就是多行（第 2 个账号写 @2，以此类推）"
        )
    return accounts


# --------------------------------------------------------------------------- #
# YYB CodeProvider 模式
# --------------------------------------------------------------------------- #
# 服务地址优先级：WECHAT_SERVER 环境变量 > YYB_SERVER（地址@ref 行，兼容旧配置）
WECHAT_SERVER = os.getenv("WECHAT_SERVER", "").strip()
SERVERS = [s.strip() for s in WECHAT_SERVER.split(",") if s.strip()]

YYB_REFS: List[str] = []  # 空=自动从 /accounts 获取，非空=只用这里指定的 ref

CODE_RETRY_TIMES = 3      # 获取 code 最大重试次数
RETRY_BASE_DELAY = 3      # 指数退避基数（秒）


def normalize_server(server: str) -> str:
    """标准化服务地址：去末尾斜杠，补 http:// 前缀"""
    return normalize_endpoint(server)


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
        self.server = normalize_endpoint(server)
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


# provider 上下文：main 收集账号时注册
_PROVIDER_CONTEXT: Dict[str, Tuple[YybCodeProvider, CodeAccount]] = {}


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


def yyb_get_wx_code(account: AccountTarget) -> str:
    """取 wx.login code（一次性，拿到后需尽快使用）。provider 可用时走 provider。"""
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
    for candidate in (result.get("code"), data.get("code"), deep_first(body, "code")):
        if isinstance(candidate, str) and len(candidate) >= 16:
            return candidate
    raise RuntimeError(f"YYB 未返回有效 code：{preview(body, 200)}")


def yyb_get_phone_package(account: AccountTarget) -> Dict[str, str]:
    """取手机号授权加密包（仅在账号尚未绑定手机号时使用）。"""
    body = yyb_post(account, "/wxapp/getPhoneNumber", {"ref": account.ref, "app_id": APP_NO})
    result = (body.get("data") or {}).get("result")
    if isinstance(result, dict) and isinstance(result.get("data"), str):
        try:
            inner = json.loads(result["data"])
            if isinstance(inner, dict) and inner.get("encryptedData"):
                result = inner
        except Exception:
            pass
    node = result if isinstance(result, dict) else {}
    enc = node.get("encryptedData") or deep_first(body, "encryptedData") or ""
    iv = node.get("iv") or deep_first(body, "iv") or ""
    if not enc or not iv:
        raise RuntimeError(f"YYB 未返回手机号加密包：{preview(body, 260)}")
    return {"encrypted_data": str(enc), "iv": str(iv), "code": str(node.get("code") or ""),
            "mobile": str((json.loads(node["data"]).get("mobile")
                           if isinstance(node.get("data"), str) and node["data"].startswith("{")
                           else "") or "")}


# --------------------------------------------------------------------------- #
# 中华保客户端
# --------------------------------------------------------------------------- #
class ZhbClient:
    """中华保接口客户端：负责签名、token 维护与业务请求。"""

    def __init__(self) -> None:
        self.token = ""
        self.union_id = ""      # 小程序从 storage 里取，登录响应里带下来
        self.session = requests.Session()
        self.session.trust_env = False

    # ---- 通用请求 ------------------------------------------------------ #
    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                body: Optional[Dict[str, Any]] = None, quiet: bool = False) -> Dict[str, Any]:
        method = method.upper()
        body_str = "" if method == "GET" else json.dumps(
            body or {}, separators=(",", ":"), ensure_ascii=False)
        path_for_sign = path
        if method == "GET" and params:
            query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
            path_for_sign = f"{path}?{query}"

        nonce = str(uuid.uuid4())
        ts = str(int(time.time() * 1000))
        headers = {
            "content-type": "application/json",
            "appId": API_APP_ID,
            "version": APP_VERSION,
            "secretKey": SIGN_SECRET_KEY,
            "nonce": nonce,
            "timestamp": ts,
            "token": self.token,
            "signature": make_signature(path_for_sign, body_str, nonce, ts, self.token),
            "path": "pages/home/home#pages/home/home#1001",
            "User-Agent": pick_user_agent(),
        }
        if method == "GET":
            resp = self.session.get(BASE_URL + path, params=params, headers=headers, timeout=ZHB_TIMEOUT)
        else:
            resp = self.session.post(BASE_URL + path, data=body_str.encode("utf-8"),
                                     headers=headers, timeout=ZHB_TIMEOUT)
        # token 可能出现在响应头，也可能在响应体 data.token（getOpenId）
        if resp.headers.get("token"):
            self.token = resp.headers["token"]
        if DEBUG:
            print(f"    · [{method} {path}] {resp.status_code} {preview(resp.text, 400)}")
        try:
            payload = resp.json()
        except Exception:
            raise RuntimeError(f"{path} 返回非 JSON（HTTP {resp.status_code}）：{preview(resp.text, 200)}")
        if not quiet and isinstance(payload, dict) and payload.get("code") not in ("200", 200, None):
            payload["_error"] = payload.get("msg") or preview(payload, 160)
        return payload

    # ---- 业务接口 ------------------------------------------------------ #
    def get_openid(self, wx_code: str) -> Dict[str, Any]:
        return self.request("GET", API_GET_OPENID, params={"code": wx_code})

    def login_registered(self, code: str, enc: str, iv: str) -> Dict[str, Any]:
        return self.request("POST", API_LOGIN_REG,
                            body={"code": code, "encryptedData": enc, "iv": iv, "invitationCode": ""})

    def home_page(self) -> Dict[str, Any]:
        return self.request("GET", API_HOME)

    def sign_info(self) -> Dict[str, Any]:
        return self.request("GET", API_SIGN_INFO)

    def do_sign(self) -> Dict[str, Any]:
        return self.request("POST", API_SIGN,
                            body={"description": "签到", "integralDate": today_text(), "type": 0})

    def complete_task(self, task_type: int, point_strategy_id: Any,
                      answer_passed: Optional[bool] = None) -> Dict[str, Any]:
        """上报任务完成。

        第 11 类（每日答题）服务端不校验答案，只需把客户端本地比对的结论
        answerPassed 一并提交即可；其余类型不带该字段。
        """
        body: Dict[str, Any] = {"integralTaskTypeCd": task_type,
                                "pointStrategyId": point_strategy_id}
        if answer_passed is not None:
            body["answerPassed"] = bool(answer_passed)
        return self.request("POST", API_COMPLETE_TASK, quiet=True, body=body)

    def receive_task(self, task_id: Any) -> Dict[str, Any]:
        return self.request("POST", API_RECEIVE_TASK, quiet=True, body={"id": task_id})

    # ---- 4/5 类任务的官方「去完成」上报通道 ---------------------------- #
    def to_complete(self) -> Dict[str, Any]:
        """「去完成」埋点上报（eventId=1002，添加企微管家）。

        小程序源码里这条请求不带任何参数，路径就是 /integral/v2/toComplete，
        带 eventId 反而会让签名路径和客户端不一致，这里严格对齐客户端。
        """
        return self.request("GET", API_TO_COMPLETE, quiet=True)

    def pre_follow_public(self, union_id: str) -> Dict[str, Any]:
        """关注公众号前置上报。"""
        return self.request("GET", API_PRE_FOLLOW, params={"unionId": union_id}, quiet=True)

    def is_follow_public(self, union_id: str) -> Dict[str, Any]:
        """查询公众号关注状态（服务端真实状态，用于判定任务能否完成）。"""
        return self.request("GET", API_IS_FOLLOW, params={"unionId": union_id}, quiet=True)

    def manager_info(self, id_card_no: str) -> Dict[str, Any]:
        """查询专属管家绑定关系；已绑定才可能完成任务 4。"""
        return self.request("GET", API_MANAGER_INFO, params={"IDCardNo": id_card_no}, quiet=True)


# --------------------------------------------------------------------------- #
# 每日答题：题库是公开静态 JSON，题目与正确答案都写在里面
# --------------------------------------------------------------------------- #
_QUIZ_CACHE: Optional[List[Dict[str, Any]]] = None


def load_quiz_bank() -> List[Dict[str, Any]]:
    """拉取题库（带进程内缓存）。失败返回空列表，调用方降级处理。"""
    global _QUIZ_CACHE
    if _QUIZ_CACHE is not None:
        return _QUIZ_CACHE
    try:
        resp = requests.get(QUIZ_BANK_URL, timeout=ZHB_TIMEOUT,
                            headers={"User-Agent": pick_user_agent()})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"    ⚠️ 题库拉取失败（不影响答题，直接按通过上报）：{preview(exc, 100)}")
        _QUIZ_CACHE = []
        return _QUIZ_CACHE

    items = data if isinstance(data, list) else None
    if items is None and isinstance(data, dict):
        for key in ("data", "list", "questions", "result"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    _QUIZ_CACHE = [x for x in (items or []) if isinstance(x, dict)]
    return _QUIZ_CACHE


def today_quiz() -> Dict[str, Any]:
    """算出今天轮到的题目与正确答案。

    索引口径与小程序一致：题目按日轮换，(今天 - 起始日) 的天数对题量取模。
    题库实测结构（90 题，根节点就是数组）：
      {questionId, question, questionAnalysis,
       options:[{optionId, optionName, optionEnum}], correctAnswerId:[optionId]}
    """
    bank = load_quiz_bank()
    if not bank:
        return {}
    index = (datetime.now().date() - QUIZ_EPOCH.date()).days % len(bank)
    quiz = bank[index]
    options = quiz.get("options") or quiz.get("answerList") or []
    raw_correct = quiz.get("correctAnswerId")
    if isinstance(raw_correct, (list, tuple)):
        correct_ids = [str(x) for x in raw_correct]
    elif raw_correct in (None, ""):
        correct_ids = []
    else:
        correct_ids = [str(raw_correct)]

    correct_texts: List[str] = []
    option_texts: List[str] = []
    for opt in options:
        oid = str(opt.get("optionId") or opt.get("id") or "")
        text = str(opt.get("optionName") or opt.get("content") or "")
        option_texts.append(text)
        if oid in correct_ids:
            correct_texts.append(text)

    return {
        "index": index,
        "total": len(bank),
        "question": str(quiz.get("question") or quiz.get("content") or ""),
        "correct_id": ",".join(correct_ids),
        "correct_text": "、".join(correct_texts),
        "options": option_texts,
    }



# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #
LOGIN_OK = "ok"        # 登录成功
LOGIN_SKIP = "skip"    # 该微信在中华保侧没有账号 → 跳过，不注册
LOGIN_FAIL = "fail"    # 其它错误

SKIP_NOTE = "未注册（该微信尚未在中华保注册，已跳过）"


def login_account(account: AccountTarget, client: ZhbClient) -> Tuple[str, str]:
    """YYB 取码 → getOpenId 换 token。

    返回 (状态, 说明)，状态取值为 LOGIN_OK / LOGIN_SKIP / LOGIN_FAIL。
    判定依据：getOpenId 只在「该 openid 已有中华保账号」时才回 token；
    不回 token 即视为未注册，默认直接跳过（不产生任何注册/绑定写操作）。
    """
    try:
        wx_code = yyb_get_wx_code(account)
    except Exception as exc:
        return LOGIN_FAIL, f"YYB 取码失败：{exc}"

    print(f"    🔑 wx.login code = {wx_code[:12]}…")
    try:
        resp = client.get_openid(wx_code)
    except Exception as exc:
        return LOGIN_FAIL, f"getOpenId 请求异常：{exc}"

    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    if resp.get("code") != "200" or not data:
        return LOGIN_FAIL, f"getOpenId 失败：{resp.get('msg') or preview(resp, 160)}"

    token = data.get("token") or client.token
    if token:
        client.token = token

    if not client.token:
        # getOpenId 不返回 token ⇒ 该微信 openid 在中华保没有账号
        if not AUTO_REGISTER:
            print(f"    ⏭️ {SKIP_NOTE}")
            return LOGIN_SKIP, SKIP_NOTE
        print("    📱 未注册，按 ZHB_AUTO_REGISTER=1 走手机号授权通道注册…")
        try:
            pkg = yyb_get_phone_package(account)
            # 注意：这里必须用 wx.login 的 code（新取一个），
            # 不能用 getPhoneNumber 返回的手机号授权 code，否则服务端报 sessionKey为空
            bind_code = yyb_get_wx_code(account)
            login_resp = client.login_registered(bind_code, pkg["encrypted_data"], pkg["iv"])
        except Exception as exc:
            return LOGIN_FAIL, f"手机号授权通道失败：{exc}"
        bind_data = login_resp.get("data") if isinstance(login_resp.get("data"), dict) else {}
        client.token = bind_data.get("token") or client.token
        if not client.token:
            return LOGIN_FAIL, f"注册未返回 token：{login_resp.get('msg') or preview(login_resp, 160)}"
        data = {
            "phone": bind_data.get("custPhone") or pkg.get("mobile"),
            "idName": bind_data.get("idName"),
            "token": client.token,
        }
        print(f"    📱 注册并登录成功：{mask_phone(data.get('phone'))}"
              f"（实名状态 {bind_data.get('realName')}）")

    phone = mask_phone(data.get("phone")) if data.get("phone") else ""
    name = keep_name(data.get("idName"))
    # 关注公众号要 unionId：客户端是从 storage 取的，登录响应里就带着
    client.union_id = str(data.get("unionId") or data.get("unionid")
                          or data.get("union_id") or client.union_id or "")
    if phone or name:
        account.label_suffix = " ".join(x for x in (phone, name) if x)
    return LOGIN_OK, phone or "已登录"


# --------------------------------------------------------------------------- #
# 业务：签到 + 任务
# --------------------------------------------------------------------------- #
def fetch_state(client: ZhbClient) -> Dict[str, Any]:
    info = client.sign_info()
    home = client.home_page()
    return {
        "sign": info.get("data") if isinstance(info.get("data"), dict) else {},
        "home": home.get("data") if isinstance(home.get("data"), dict) else {},
        "sign_code": info.get("code"),
        "home_code": home.get("code"),
    }


def do_sign(client: ZhbClient, state: Dict[str, Any]) -> Dict[str, Any]:
    """签到。返回 {'ok':bool,'already':bool,'note':str}"""
    sign = state["sign"]
    if sign.get("todaySign") in (1, "1", True):
        return {"ok": True, "already": True, "note": "今日已签到",
                "continuous": sign.get("continuousNum"), "total": sign.get("totalIntegral")}

    if DRY_RUN:
        return {"ok": True, "already": False, "note": "DRY_RUN：跳过签到",
                "continuous": sign.get("continuousNum"), "total": sign.get("totalIntegral")}

    resp = client.do_sign()
    if resp.get("code") == "200":
        after = client.sign_info().get("data") or {}
        return {"ok": True, "already": False, "note": "签到成功",
                "continuous": after.get("continuousNum"), "total": after.get("totalIntegral")}
    msg = resp.get("msg") or preview(resp, 160)
    if "重复" in str(msg) or "已签" in str(msg):
        return {"ok": True, "already": True, "note": f"今日已签到（{msg}）",
                "continuous": sign.get("continuousNum"), "total": sign.get("totalIntegral")}
    return {"ok": False, "already": False, "note": f"签到失败：{msg}",
            "continuous": sign.get("continuousNum"), "total": sign.get("totalIntegral")}


def _task_brief(task: Dict[str, Any]) -> str:
    ttype = task.get("taskType")
    return f"{TASK_TYPE_NAME.get(ttype, ttype)}·{task.get('taskName')}"


# 服务端因「未实名」拦下任务时的返回文案（各地风控措辞略有差异，做包含匹配）
_REALNAME_HINTS = ("实名", "认证", "身份证", "身份信息")
# 服务端明确表示「这类任务不走这个接口」的文案 → 归入需真人，不算失败
_UNSUPPORTED_HINTS = ("不支持的任务类型", "任务类型不存在", "不支持")


def _is_realname_block(msg: Any) -> bool:
    text = str(msg or "")
    return any(hint in text for hint in _REALNAME_HINTS)


def _is_unsupported(msg: Any) -> bool:
    text = str(msg or "")
    return any(hint in text for hint in _UNSUPPORTED_HINTS)


def _ctx_value(home: Dict[str, Any], *keys: str) -> str:
    """从首页数据里挖出 unionId / 身份证号等上下文值。"""
    for key in keys:
        value = deep_first(home, key)
        if value not in (None, ""):
            return str(value)
    return ""


def _manual_pre_report(client: ZhbClient, ttype: int, home: Dict[str, Any]) -> str:
    """类型 4/5 的官方「去完成」上报。

    小程序里点任务按钮就是走这两个接口，服务端据此记录动作；
    但真正判定「已完成」还依赖企微侧绑定回写 / 公众号关注状态回查，
    所以这里只做上报，能过就过，过不了交给 completedTask 的结论。
    """
    try:
        if ttype == 4:
            resp = client.to_complete()
            if str(resp.get("code")) == "200":
                return "已上报 toComplete，需在微信内扫码加企微后由服务端回写才算完成"
            return f"toComplete 返回 {resp.get('msg') or resp.get('code')}，需真人扫码加企微"
        if ttype == 5:
            union_id = client.union_id or _ctx_value(home, "unionId", "unionid")
            if not union_id:
                return "缺 unionId，需在微信内真正关注公众号"
            status = client.is_follow_public(union_id)
            followed = status.get("data")       # true / false / None(查询失败)
            resp = client.pre_follow_public(union_id)
            if str(resp.get("code")) == "200":
                return (f"已上报 preFollowPublic（服务端关注状态 {followed}），"
                        f"需在微信内真正关注公众号后回查才会变更")
            return f"preFollowPublic 返回 {resp.get('msg') or resp.get('code')}，需真人关注公众号"
    except Exception as exc:
        return f"上报异常：{preview(exc, 100)}"
    return ""


def run_tasks(client: ZhbClient, home: Dict[str, Any]) -> Dict[str, Any]:
    """完成可脚本化的任务并领取积分。

    统计口径（供日志与通知使用）：
      completed        本次成功标记为完成的任务
      failed           服务端拒绝执行的任务（含被「未实名」拦下的）
      realname_blocked 其中因未实名被拦下的数量
      already_done     今天此前已完成/已领取、本次无需再动的任务
      unsupported      需真人操作 / 服务端不支持脚本完成的任务
      received         成功领取积分的任务；gained = 本次领到的积分数
      integral_before / integral_after  任务前后的积分基数与结果
    """
    task_list = home.get("showTaskList") or []
    result: Dict[str, Any] = {
        "completed": [], "failed": [], "received": [], "skipped": [], "unsupported": [],
        "already_done": [], "gained": 0, "realname_blocked": 0, "module_off": False,
        "integral_before": home.get("totalIntegral"), "integral_after": None,
    }

    if not ENABLE_TASK:
        result["module_off"] = True
        result["skipped"].append("已关闭任务模块（ZHB_ENABLE_TASK=0）")
        return result

    def allowed(ttype: Any) -> bool:
        """TASK_TYPES 为 None 表示全部类型都尝试。"""
        return TASK_TYPES is None or ttype in TASK_TYPES

    def mark_failed(task: Dict[str, Any], resp: Dict[str, Any], note: str = "") -> None:
        msg = str(resp.get("msg") or resp.get("_error") or preview(resp, 80) or "未知原因")
        if _is_unsupported(msg):
            # 服务端明确说这类任务不走 completedTask → 不是失败，是需要真人
            result["unsupported"].append({
                "name": _task_brief(task),
                "reason": msg,
                "note": note or "服务端不支持脚本直接完成",
            })
            return
        if note:
            msg = f"{msg}（{note}）"
        result["failed"].append({"name": _task_brief(task), "reason": msg})
        if _is_realname_block(msg):
            result["realname_blocked"] += 1

    # 1) 先领掉历史上已经完成但未领取的
    for task in task_list:
        if task.get("status") == 2 and task.get("id"):
            if DRY_RUN:
                result["received"].append(f"{_task_brief(task)}（DRY_RUN 未领取）")
                continue
            resp = client.receive_task(task["id"])
            if str(resp.get("code")) == "200":
                result["received"].append(_task_brief(task))
                result["gained"] += int(task.get("integralNum") or 0)
            time.sleep(TASK_DELAY)

    # 2) 完成未做的任务
    quiz_printed = False
    for task in task_list:
        status = task.get("status")
        ttype = task.get("taskType")
        if status == 2:
            continue                      # 已在第 1 步领取，跳过
        if status != 0:
            # 今天此前就已完成/已领取的任务，计入「今日已完成」
            if status == 3:
                result["already_done"].append(_task_brief(task))
            continue
        if not allowed(ttype):
            result["unsupported"].append({
                "name": _task_brief(task), "reason": "不在 ZHB_TASK_TYPES 白名单内",
                "note": "可通过环境变量放开",
            })
            continue
        if DRY_RUN:
            result["completed"].append(f"{_task_brief(task)}（DRY_RUN 未执行）")
            continue

        # 11=每日答题：题库可算，答案确定，直接按「答对」上报
        answer_passed: Optional[bool] = None
        note = ""
        if ttype == 11:
            quiz = today_quiz()
            if quiz:
                if not quiz_printed:
                    print(f"       📖 今日题目（题库第 {quiz['index'] + 1}/{quiz['total']} 题）："
                          f"{quiz['question']}")
                    print(f"          正确答案：{quiz['correct_text'] or quiz['correct_id']}")
                    quiz_printed = True
                answer_passed = True
            else:
                answer_passed = True      # 题库拿不到也照常上报通过
        elif ttype in (4, 5):
            note = _manual_pre_report(client, ttype, home)

        resp = client.complete_task(ttype, task.get("pointStrategyId"), answer_passed=answer_passed)
        if str(resp.get("code")) == "200":
            result["completed"].append(_task_brief(task))
        else:
            mark_failed(task, resp, note)
        time.sleep(TASK_DELAY)

    # 3) 签到会带出「连续签到 N 天」任务
    sign_cont = home.get("signContinuous")
    if isinstance(sign_cont, dict) and sign_cont.get("taskType") and not DRY_RUN:
        done = sign_cont.get("finishNum") or 0
        target = sign_cont.get("targetNum") or 0
        if sign_cont.get("status") in (0, None) and target and done >= target \
                and allowed(sign_cont.get("taskType")):
            resp = client.complete_task(sign_cont.get("taskType"), sign_cont.get("pointStrategyId"))
            if str(resp.get("code")) == "200":
                result["completed"].append(_task_brief(sign_cont))
            else:
                mark_failed(sign_cont, resp)
            time.sleep(TASK_DELAY)

    # 4) 再领一轮新完成的
    if not DRY_RUN:
        home2 = client.home_page().get("data") or {}
        for task in home2.get("showTaskList") or []:
            if task.get("status") == 2 and task.get("id"):
                resp = client.receive_task(task["id"])
                if str(resp.get("code")) == "200":
                    result["received"].append(_task_brief(task))
                    result["gained"] += int(task.get("integralNum") or 0)
                time.sleep(TASK_DELAY)
        result["integral_after"] = home2.get("totalIntegral")

    return result



def describe_tasks(task_result: Dict[str, Any]) -> Tuple[str, bool]:
    """把任务结果压成一行摘要。返回 (文案, 是否因未实名被全拦)。"""
    done = len(task_result.get("completed") or [])
    failed = len(task_result.get("failed") or [])
    already = len(task_result.get("already_done") or [])
    blocked = int(task_result.get("realname_blocked") or 0)

    if task_result.get("module_off"):
        return "已关闭任务模块（ZHB_ENABLE_TASK=0）", False

    # 一个都没完成、且失败原因清一色是「未实名」→ 单独措辞
    realname_all = failed > 0 and done == 0 and blocked >= failed

    parts: List[str] = []
    if realname_all:
        parts.append(f"未实名，任务全部失败（成功 0 个 / 失败 {failed} 个）")
    else:
        # 实名账号一律给出「完成 / 失败」两个数
        parts.append(f"完成 {done} 个，失败 {failed} 个")
        if already:
            parts.append(f"今日此前已完成 {already} 个")
    if task_result.get("received"):
        parts.append(f"领取 {len(task_result['received'])} 个（+{task_result['gained']} 积分）")
    if task_result.get("unsupported"):
        parts.append(f"需真人操作 {len(task_result['unsupported'])} 个")
    if task_result.get("skipped"):
        parts.append(f"跳过 {len(task_result['skipped'])} 个")
    return "，".join(parts), realname_all


# --------------------------------------------------------------------------- #
# 单账号流程
# --------------------------------------------------------------------------- #
def run_account(index: int, total: int, account: AccountTarget) -> Dict[str, Any]:
    print()
    banner([f"🚗 [{index}/{total}] {account.label}", f"🌐 {account.server}"])
    item: Dict[str, Any] = {"label": account.label, "server": account.server, "success": False,
                            "skipped": False, "user": "-", "sign": "-", "continuous": "-",
                            "total": "-", "tasks": "-", "gained": 0, "error": "",
                            "integral_before": None, "integral_after": None,
                            "realname_blocked": 0, "realname_all": False}
    client = ZhbClient()

    state, note = login_account(account, client)
    if state == LOGIN_SKIP:
        # 未注册：不注册、不写数据，日志与通知里明确标注
        account.label_suffix = "未注册"
        item.update({"skipped": True, "success": True, "label": account.label,
                     "user": "未注册", "sign": "未注册（已跳过）", "tasks": "未注册（已跳过）",
                     "continuous": "-", "total": "-"})
        return item
    if state != LOGIN_OK:
        item["error"] = note
        print(f"    ❌ 登录失败：{note}")
        return item
    item["label"] = account.label
    item["user"] = account.label_suffix or note

    # 签到
    state = fetch_state(client)
    if state.get("sign_code") not in ("200", 200) and state.get("home_code") not in ("200", 200):
        item["error"] = f"业务接口异常：{state.get('home_code')}"
        print(f"    ❌ {item['error']}")
        return item
    # 本次运行的积分基数（签到前）
    item["integral_before"] = state["sign"].get("totalIntegral")

    if ENABLE_SIGN:
        sign_result = do_sign(client, state)
        item["continuous"] = sign_result.get("continuous")
        item["total"] = sign_result.get("total")
        icon = "✅" if sign_result["ok"] else "❌"
        print(f"    {icon} 签到：{sign_result['note']}"
              f"｜连签 {sign_result.get('continuous')} 天｜积分 {sign_result.get('total')}")
        item["sign"] = sign_result["note"]
        if not sign_result["ok"]:
            item["error"] = sign_result["note"]
    else:
        item["sign"] = "已跳过（ZHB_ENABLE_SIGN=0）"

    # 任务
    if ENABLE_TASK:
        latest = client.home_page().get("data") or {}
        if item.get("integral_before") is None:
            item["integral_before"] = latest.get("totalIntegral")
        task_result = run_tasks(client, latest)
        item["gained"] = task_result.get("gained", 0)
        item["realname_blocked"] = task_result.get("realname_blocked", 0)
        if task_result.get("integral_after") is not None:
            item["total"] = task_result["integral_after"]
        item["tasks"], realname_all = describe_tasks(task_result)
        item["realname_all"] = realname_all
        print(f"    📋 任务：{item['tasks']}")
        for line in task_result["received"]:
            print(f"       + 已领取 {line}")
        for line in task_result["completed"]:
            print(f"       · 已完成 {line}")
        for node in task_result["failed"]:
            print(f"       ❌ 失败 {node['name']}：{node['reason']}")
        for line in task_result["already_done"]:
            print(f"       ✔️ 今日此前已完成 {line}")
        for node in task_result["unsupported"]:
            print(f"       ~ 需真人操作 {node['name']}：{node['note']}")
        for line in task_result["skipped"]:
            print(f"       ⚠️ {line}")

    final = client.sign_info().get("data") or {}
    item["total"] = final.get("totalIntegral", item["total"])
    item["integral_after"] = item["total"]
    item["continuous"] = final.get("continuousNum", item["continuous"])
    item["success"] = not item["error"]
    print(f"    💰 积分：{fmt_integral(item)}｜连签 {item['continuous']} 天")
    return item


# --------------------------------------------------------------------------- #
# 汇总与通知
# --------------------------------------------------------------------------- #
def _icon(item: Dict[str, Any]) -> str:
    if item.get("skipped"):
        return "⏭️"
    return "✅" if item.get("success") else "❌"


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fmt_integral(item: Dict[str, Any]) -> str:
    """把积分显示成「基数 → 完成后的积分（+增量）」。"""
    before = _as_int(item.get("integral_before"))
    after = _as_int(item.get("integral_after"))
    if after is None:
        after = _as_int(item.get("total"))
    if before is None or after is None:
        return str(after if after is not None else "-")
    delta = after - before
    return f"{before} → {after}（{'+' if delta >= 0 else ''}{delta}）"


def build_report(results: List[Dict[str, Any]]) -> str:
    lines = [f"🚗 {APP_NAME}｜{now_text()}", ""]
    for item in results:
        lines.append(f"{_icon(item)} {item['label']}")
        if item.get("skipped"):
            lines.append(f"    ⏭️ {item['sign']}")
            lines.append("")
            continue
        if item.get("error"):
            lines.append(f"    ❌ {item['error']}")
        lines.append(f"    📱 签到：{item['sign']}｜连签 {item['continuous']} 天")
        lines.append(f"    📋 任务：{item['tasks']}")
        lines.append(f"    💰 积分：{fmt_integral(item)}")
        lines.append("")
    ok = sum(1 for i in results if i.get("success") and not i.get("skipped"))
    skipped = sum(1 for i in results if i.get("skipped"))
    failed = len(results) - ok - skipped
    tail = f"🏁 汇总：{ok} 成功"
    if skipped:
        tail += f" / {skipped} 未注册已跳过"
    tail += f" / {failed} 失败（共 {len(results)} 个账号）"
    lines.append(tail)
    return "\n".join(lines)


def load_notify():
    """兼容青龙各版本的 notify.py 位置（脚本目录 / 订阅目录 / 根目录）。"""
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
            spec = importlib.util.spec_from_file_location("zhb_qinglong_notify", path)
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
    sender = load_notify()
    if sender is not None:
        try:
            sender(title, content)
            print("✅ [通知] 已通过青龙通知模块发送")
            return
        except Exception as exc:
            print(f"⚠️ [通知] 青龙通知发送失败（不影响结果）：{preview(exc, 120)}")
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
        f"🔐 登录    : YYB 取码 → getOpenId 换 token（无需抓包）",
        f"🚫 未注册  : " + ("自动注册（ZHB_AUTO_REGISTER=1，会创建新账号）"
                           if AUTO_REGISTER else "跳过并在日志标注未注册"),
        f"✍️ 签到    : " + ("开启" if ENABLE_SIGN else "关闭（ZHB_ENABLE_SIGN=0）"),
        f"📋 任务    : " + (f"开启，可自动完成类型 {'全部' if TASK_TYPES is None else sorted(TASK_TYPES)}"
                           if ENABLE_TASK else "关闭（ZHB_ENABLE_TASK=0）"),
        f"🔑 签名    : HMAC-SHA256(路径+body+nonce+时间戳+token)，密钥由小程序包静态算法复现",
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
                "label": account.label, "server": account.server, "success": False,
                "skipped": False, "user": "-", "sign": "-", "continuous": "-", "total": "-",
                "tasks": "-", "gained": 0, "error": str(exc),
                "integral_before": None, "integral_after": None,
                "realname_blocked": 0, "realname_all": False,
            })
        if index < len(accounts):
            time.sleep(random.uniform(3.0, 6.0))

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
