#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 勇闯天涯
# cron: 44 7 * * *
"""
name: 勇闯天涯签到
cron: 44 7 * * *

勇闯天涯 superX 微信小程序「积分签到」每日签到 + 新手/每月任务自动完成，
基于 YYB-Go-Enhanced 自动取码登录，全程无需抓包。

青龙环境变量：
  YYB_SERVER           必填，YYB-Go-Enhanced地址@微信账号标识，多账号每行一条
                       例：yyb-go:8000@1
  YYB_API_KEY / YYB_PROTOCOL_TOKEN       启用协议鉴权时配置，二选一

依赖：requests
通知：优先使用青龙内置 notify.py；未配置时回落到 PushPlus / Server酱 / 企业微信 / Bark。
      通知里固定输出每个账号的「初始积分 → 最终积分」，便于对账。通知失败不影响结果。
功能：查询签到状态与任务清单 -> 每日签到 -> 自动完成可自动的新手/每月任务 -> 复查积分
      -> 汇总通知。多账号串行。

# 作者：lcmovie https://github.com/lcmovie
"""
from __future__ import annotations

import importlib.util
import json
import os
import random
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

APP_NAME = "勇闯天涯签到"
HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
APP_NO = "wx13c6d1dffe3b32ec"          # 勇闯天涯 superX 小程序 AppID
API_BASE = "https://superX.crb.cn/Api"  # 接口统一前缀

# 已实测可自动完成的任务（服务端不校验真实行为）。可通过 YCTY_AUTO_TASKS 覆盖。
DEFAULT_AUTO_TASKS = ("SHARE_APP", "ACCESS_JD", "ACCESS_BEERTOWN")

# 任务 taskType -> 中文名（用于日志/通知；未知类型兜底显示原值）
TASK_NAMES = {
    "SHARE_APP": "分享签到活动",
    "UPDATE_USERINFO": "完善个人信息",
    "SCAN_QR_CODE": "扫描瓶盖码",
    "BUG_GIFT": "参与XBOX兑换",
    "SUB_MSG": "订阅服务消息",
    "ACCESS_JD": "访问京东旗舰店",
    "ACCESS_BEERTOWN": "探索SNOWVERSE",
}

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) XWEB/8555",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148 MicroMessenger/8.0.49(0x1800312b) NetType/WIFI Language/zh_CN",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat XWEB/8555",
]


class SkipAccount(Exception):
    """账号本身不具备参与条件，归入「跳过」而非失败。"""


class ApiError(Exception):
    """接口返回了非预期结果。"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def env_flag(name: str, default: str = "1") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name, "") or "").strip() or default)
    except Exception:
        return default


DRY_RUN = env_flag("YCTY_DRY_RUN", "0")
ENABLE_SIGN = env_flag("YCTY_ENABLE_SIGN", "1")
ENABLE_TASK = env_flag("YCTY_ENABLE_TASK", "1")
LOGIN_RETRY = max(1, env_int("YCTY_LOGIN_RETRY", 3))
TIMEOUT = env_int("YCTY_REQUEST_TIMEOUT", 30)
RANDOM_HEADERS = env_flag("YCTY_RANDOM_HEADERS", "1")
DEBUG = env_flag("YCTY_DEBUG", "0")

_auto_tasks_raw = (os.getenv("YCTY_AUTO_TASKS", "") or "").strip()
AUTO_TASKS = (
    tuple(t.strip() for t in _auto_tasks_raw.split(",") if t.strip())
    if _auto_tasks_raw
    else DEFAULT_AUTO_TASKS
)

_CURRENT_UA = random.choice(UAS)


def log(*args: Any) -> None:
    print(*args, flush=True)


def dbg(*args: Any) -> None:
    if DEBUG:
        print("[DEBUG]", *args, flush=True)


def clean(value: Any, limit: int = 160) -> str:
    """清洗服务端返回的文本字段（去引号/空白/零宽字符），并截断。"""
    if value is None:
        return ""
    text = str(value)
    text = text.strip("\"' \t\r\n\u200b\u200c\u200d\ufeff")
    return text[:limit]


def preview(obj: Any, limit: int = 300) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)[:limit]
    except Exception:
        return str(obj)[:limit]


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def headers_json() -> Dict[str, str]:
    ua = random.choice(UAS) if RANDOM_HEADERS else _CURRENT_UA
    return {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/json",
    }


def parse_ref_list(raw: str) -> List[Tuple[str, str]]:
    """解析 YYB_SERVER：每行 `地址@账号ID`。"""
    out: List[Tuple[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "@" not in line:
            out.append((line, "1"))
            continue
        host, _, ref = line.rpartition("@")
        host = host.strip()
        if not host.startswith("http"):
            host = "http://" + host
        out.append((host, ref.strip() or "1"))
    return out


def task_label(task_type: str) -> str:
    return TASK_NAMES.get(task_type, task_type)


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
            return [CodeAccount(x, x) for x in refs]

        # 自动从 /accounts 获取
        if not self.server:
            print("未配置 YYB 服务地址，且 YYB_REFS 为空")
            return []

        url = f"{self.server}/accounts"
        try:
            response = self.session.get(url, timeout=TIMEOUT)
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
        headers = {"User-Agent": _CURRENT_UA, "Content-Type": "application/json"}
        api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
        if api_key:
            headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key

        for attempt in range(1, CODE_RETRY_TIMES + 1):
            try:
                response = self.session.post(
                    url, json=payload, headers=headers, timeout=TIMEOUT
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


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Client:
    """对 superX.crb.cn 的薄封装：登录 + 带 sessionKey 的业务请求。"""

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update(headers_json())
        self.session_key = ""
        self.open_id = ""

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        query = dict(params or {})
        if self.session_key:
            query["sessionKey"] = self.session_key
        url = API_BASE + "/" + path
        r = self.s.get(url, params=query, timeout=TIMEOUT)
        dbg("GET", url, "->", r.status_code, preview(r.text, 300))
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} @ {path}")
        try:
            return r.json()
        except Exception:
            raise ApiError(f"非 JSON 响应 @ {path}: {clean(r.text)}")

    def _post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = API_BASE + "/" + path
        if self.session_key:
            url += ("&" if "?" in url else "?") + "sessionKey=" + self.session_key
        r = self.s.post(url, json=(body or {}), timeout=TIMEOUT)
        dbg("POST", url, "->", r.status_code, preview(r.text, 300))
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} @ {path}")
        try:
            return r.json()
        except Exception:
            raise ApiError(f"非 JSON 响应 @ {path}: {clean(r.text)}")

    # ---------------- 业务封装 ---------------- #
    def login(self, code: str) -> Dict[str, Any]:
        return self._post("b3/OAuthProgram/Login", {"code": code})

    def user_info(self) -> Dict[str, Any]:
        return (self._get("b1/GetUserInfo").get("data") or {})

    def sign_info(self) -> Dict[str, Any]:
        return (self._post("sign/getSignInfo").get("data") or {})

    def add_sign(self) -> Dict[str, Any]:
        return self._post("sign/addSign", {})

    def do_task(self, task_type: str) -> Dict[str, Any]:
        return self._post("sign/doTask", {"taskType": task_type})


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #
# provider 上下文：main 收集账号时注册，login_once 优先走 provider 取码
_PROVIDER_CONTEXT: Dict[str, Tuple[YybCodeProvider, CodeAccount]] = {}


def yyb_get_code(host: str, ref: str) -> Tuple[str, str]:
    """YYB 取 wx.login code → (code, openid)。provider 可用时走 provider。"""
    context = _PROVIDER_CONTEXT.get(str(ref))
    if context is not None:
        provider, account = context
        code = provider.get_code(account)
        if not code:
            raise ApiError(f"YYB 未返回 code（provider 重试耗尽）")
        dbg("code =", code[:24], "...")
        return code, ""
    url = normalize_server(host) + "/wxapp/getCode"
    headers = {"User-Agent": _CURRENT_UA, "Content-Type": "application/json"}
    api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
    if api_key:
        headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key
    r = requests.post(url, json={"app_id": APP_NO, "ref": str(ref)}, headers=headers, timeout=TIMEOUT)
    dbg("YYB getCode", r.status_code, preview(r.text, 300))
    if r.status_code != 200:
        raise ApiError(f"YYB 取码失败 HTTP {r.status_code}: {clean(r.text)}")
    data = r.json()
    payload = data.get("data") or {}
    if not (payload.get("result") or {}).get("code"):
        raise ApiError(f"YYB 未返回 code: {clean(r.text)}")
    return clean((payload.get("result") or {}).get("code")), clean(payload.get("openid"))


def login_once(host: str, ref: str) -> Client:
    code, openid = yyb_get_code(host, ref)
    client = Client()
    client.open_id = openid
    dbg("code =", code[:24], "...")
    resp = client.login(code)
    data = resp.get("data") or {}
    sk = clean(data.get("sessionKey"))
    if resp.get("code") != 0 or not sk:
        raise ApiError(f"Login 未返回 sessionKey: {preview(resp)}")
    client.session_key = sk
    return client


def login(host: str, ref: str) -> Client:
    last: Optional[Exception] = None
    for attempt in range(1, LOGIN_RETRY + 1):
        try:
            return login_once(host, ref)
        except Exception as exc:
            last = exc
            log(f"   ⚠️ 第 {attempt}/{LOGIN_RETRY} 次登录失败：{clean(exc, 200)}")
            if attempt < LOGIN_RETRY:
                time.sleep(1.5 * attempt)
    raise ApiError(f"登录失败：{clean(last, 200)}")


# --------------------------------------------------------------------------- #
# 业务
# --------------------------------------------------------------------------- #
def run_account(host: str, ref: str, label: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"label": label, "ref": ref}
    log(f"\n{'=' * 62}\n账号 {label}\n{'=' * 62}")

    try:
        client = login(host, ref)
    except SkipAccount as exc:
        log(f"   ⏭️ 跳过：{exc}")
        result.update(skipped=True, sign=f"跳过：{exc}")
        return result
    except Exception as exc:
        log(f"   ❌ 登录失败：{clean(exc, 220)}")
        result.update(error=f"登录失败：{clean(exc, 220)}")
        return result

    # ---------------- 初始状态 ---------------- #
    try:
        user = client.user_info()
    except Exception as exc:
        log(f"   ❌ 获取用户信息失败：{clean(exc, 200)}")
        result.update(error=f"获取用户信息失败：{clean(exc, 200)}")
        return result

    name = clean(user.get("name")) or clean(user.get("nickName")) or f"账号{ref}"
    before_score = user.get("score")
    before_xmedal = user.get("xmedal")
    result["user"] = name
    result["score_before"] = before_score
    log(f"   👤 {name}　积分 {before_score}　X勋章 {before_xmedal}")

    try:
        info = client.sign_info()
    except Exception as exc:
        log(f"   ❌ 获取签到信息失败：{clean(exc, 200)}")
        result.update(error=f"获取签到信息失败：{clean(exc, 200)}")
        return result

    signed = bool(info.get("signed"))
    sign_day = info.get("signDayNum", 0)
    tasks = info.get("taskInfoList") or []
    ladder = info.get("signDaysV1") or []
    result["signed_before"] = signed
    result["sign_day"] = sign_day
    log(f"   📅 今日{'已' if signed else '未'}签到　连签 {sign_day} 天")

    # ---------------- 每日签到 ---------------- #
    sign_msg: str
    sign_ok = False
    if DRY_RUN:
        sign_msg = "干跑模式，未执行签到"
        sign_ok = True                      # 干跑只查询，视为成功
    elif not ENABLE_SIGN:
        sign_msg = "已关闭签到（YCTY_ENABLE_SIGN=0）"
        sign_ok = True                      # 主动关闭，不算失败
    elif signed:
        sign_msg = "今天已经签到"
        sign_ok = True
    else:
        try:
            resp = client.add_sign()
            if resp.get("code") == 0:
                sign_msg = "签到成功"
                sign_ok = True
            else:
                sign_msg = f"签到返回异常：{preview(resp)}"
        except Exception as exc:
            sign_msg = f"签到异常：{clean(exc, 200)}"
    result["sign"] = sign_msg
    result["sign_ok"] = sign_ok
    log(f"   ✍️ 签到：{sign_msg}")

    # ---------------- 新手/每月任务 ---------------- #
    task_lines: List[str] = []
    done_auto: List[str] = []
    remain_manual: List[str] = []
    if DRY_RUN:
        # 干跑只读展示任务状态，不执行任何写操作
        for task in tasks:
            tt = clean(task.get("taskType"))
            is_done = bool(task.get("isDone"))
            if is_done:
                task_lines.append(f"✅ {task_label(tt)} 已完成")
            elif tt in AUTO_TASKS:
                task_lines.append(f"🟢 {task_label(tt)} 可自动完成（干跑未执行）")
            else:
                task_lines.append(f"⏳ {task_label(tt)} 需手动完成")
                remain_manual.append(tt)
    elif not ENABLE_TASK:
        task_lines.append("已关闭任务（YCTY_ENABLE_TASK=0）")
    else:
        for task in tasks:
            tt = clean(task.get("taskType"))
            is_done = bool(task.get("isDone"))
            if is_done:
                continue
            if tt in AUTO_TASKS:
                try:
                    resp = client.do_task(tt)
                    if resp.get("code") == 0:
                        done_auto.append(tt)
                        task_lines.append(f"✅ {task_label(tt)} +{task.get('score', '')}分")
                    else:
                        remain_manual.append(tt)
                        task_lines.append(f"⚠️ {task_label(tt)} 返回未完成：{clean(resp.get('message'))}")
                except Exception as exc:
                    remain_manual.append(tt)
                    task_lines.append(f"⚠️ {task_label(tt)} 异常：{clean(exc, 120)}")
            else:
                remain_manual.append(tt)
                task_lines.append(f"⏳ {task_label(tt)} 需手动完成")
    if not task_lines:
        task_lines.append("无待完成任务（全部已完成）")
    for line in task_lines:
        log(f"   🎯 {line}")
    result["task_done"] = done_auto
    result["task_manual"] = remain_manual

    # ---------------- 复查 ---------------- #
    try:
        info_after = client.sign_info()
    except Exception:
        info_after = {}
    try:
        user_after = client.user_info()
    except Exception:
        user_after = {}

    after_score = user_after.get("score", before_score)
    after_xmedal = user_after.get("xmedal", before_xmedal)
    sign_day_after = info_after.get("signDayNum", sign_day)
    result["score_after"] = after_score
    result["sign_day_after"] = sign_day_after

    delta = None
    try:
        if before_score is not None and after_score is not None:
            delta = int(after_score) - int(before_score)
    except (TypeError, ValueError):
        delta = None

    log(f"   💎 积分：{before_score} → {after_score}"
        f"{f'（本日 {delta:+}）' if delta is not None else ''}"
        f"　X勋章 {before_xmedal} → {after_xmedal}　连签 {sign_day_after} 天")

    # 连签阶梯提示
    if ladder:
        nxt = [l for l in ladder if int(l.get("day", 0)) > int(sign_day_after)]
        if nxt:
            l = nxt[0]
            log(f"   🎯 连签 {l.get('day')} 天可得 {l.get('title')}（当前 {sign_day_after} 天）")

    result["success"] = result.get("sign_ok", False)
    return result


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #
def load_notify():
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
            spec = importlib.util.spec_from_file_location("ycty_qinglong_notify", path)
            module = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(module)
            for name in ("send", "sendNotify"):
                func = getattr(module, name, None)
                if callable(func):
                    return func
        except Exception as exc:
            print(f"⚠️ [通知] 加载 {path} 失败：{clean(exc, 120)}")
    return None


QL_PUSH_ENVS = (
    "BARK_PUSH", "DD_BOT_TOKEN", "FSKEY", "GOBOT_URL", "IGOT_PUSH_KEY", "PUSH_KEY",
    "DEER_KEY", "CHAT_URL", "PUSH_PLUS_TOKEN", "WE_PLUS_BOT_TOKEN", "QMSG_KEY",
    "QYWX_KEY", "QYWX_AM", "TG_BOT_TOKEN", "SMTP_SERVER", "PUSHME_KEY",
    "WEBHOOK_URL", "NTFY_TOPIC", "WXPUSHER_APP_TOKEN", "OPENILINK_APP_TOKEN",
)


def send_notify(title: str, content: str) -> None:
    panel_channel = next((k for k in QL_PUSH_ENVS if (os.getenv(k) or "").strip()), "")
    sender = load_notify()
    if sender is not None:
        if panel_channel:
            try:
                sender(title, content)
                log(f"✅ [通知] 已通过青龙通知模块发送（通道 {panel_channel}）")
                return
            except Exception as exc:
                log(f"⚠️ [通知] 青龙通知发送失败（不影响结果）：{clean(exc, 120)}")
        else:
            log("ℹ️ [通知] 青龙面板未配置推送变量，改用脚本自带通道")
    else:
        log("⚠️ [通知] 未找到青龙 notify.py，使用脚本自带通道")

    sent = False
    plusplus = (os.getenv("PUSH_PLUS_TOKEN", "") or os.getenv("PLUSPLUS_TOKEN", "") or "").strip()
    if plusplus:
        try:
            r = requests.post("https://www.pushplus.plus/send",
                              json={"token": plusplus, "title": title, "content": content,
                                    "template": "txt"}, timeout=20)
            ok = r.status_code == 200 and (r.json().get("code") == 200)
            sent = sent or ok
            log(f"{'✅' if ok else '❌'} [通知] PushPlus 发送{'成功' if ok else '失败：' + clean(r.text, 120)}")
        except Exception as exc:
            log(f"❌ [通知] PushPlus 发送失败：{clean(exc, 120)}")
    server_push = (os.getenv("PUSH_KEY", "") or os.getenv("SERVERPUSHKEY", "") or "").strip()
    if server_push and not sent:
        try:
            requests.post(f"https://sctapi.ftqq.com/{server_push}.send",
                          data={"title": title, "desp": content}, timeout=15)
            sent = True
            log("✅ [通知] Server 酱发送成功")
        except Exception as exc:
            log(f"❌ [通知] Server 酱发送失败：{clean(exc, 120)}")
    qywx = (os.getenv("QYWX_KEY", "") or os.getenv("QYWX_TOKEN", "") or "").strip()
    if qywx and not sent:
        try:
            requests.post(f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={qywx}",
                          json={"msgtype": "text",
                                "text": {"content": f"{title}\n\n{content}"}}, timeout=15)
            sent = True
            log("✅ [通知] 企业微信机器人发送成功")
        except Exception as exc:
            log(f"❌ [通知] 企业微信机器人发送失败：{clean(exc, 120)}")
    bark = (os.getenv("BARK_PUSH", "") or "").strip()
    if bark and not sent:
        try:
            requests.post(bark.rstrip("/"), json={"title": title, "body": content}, timeout=15)
            sent = True
            log("✅ [通知] Bark 发送成功")
        except Exception as exc:
            log(f"❌ [通知] Bark 发送失败：{clean(exc, 120)}")
    if not sent:
        log("ℹ️ [通知] 未配置任何可用推送通道，结果仅输出到日志")


def _num(value: Any) -> str:
    try:
        f = float(value)
        return str(int(f)) if f == int(f) else f"{f:.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


def build_report(results: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    ok = sum(1 for r in results if r.get("success") and not r.get("skipped") and not r.get("error"))
    skip = sum(1 for r in results if r.get("skipped"))
    fail = sum(1 for r in results if r.get("error") and not r.get("skipped"))
    lines.append(f"成功 {ok} / 跳过 {skip} / 失败 {fail}")
    lines.append("")
    for r in results:
        name = r.get("user") or r.get("label") or ""
        if r.get("skipped"):
            lines.append(f"⏭️ {name}：{r.get('sign', '跳过')}")
            continue
        if r.get("error") and not r.get("success"):
            lines.append(f"❌ {name}：{r.get('error', '失败')}")
            continue
        b, a = r.get("score_before"), r.get("score_after")
        delta = ""
        try:
            if b is not None and a is not None:
                d = int(a) - int(b)
                delta = f"（{'+' if d >= 0 else ''}{d}）"
        except (TypeError, ValueError):
            pass
        head = f"✅ {name}"
        if b is not None and a is not None:
            head += f"　积分 {_num(b)} → {_num(a)}{delta}"
        head += f"　连签 {r.get('sign_day_after', '-')} 天"
        lines.append(head)
        if r.get("task_done"):
            labels = "、".join(task_label(t) for t in r["task_done"])
            lines.append(f"   自动完成任务：{labels}")
        if r.get("task_manual"):
            labels = "、".join(task_label(t) for t in r["task_manual"])
            lines.append(f"   需手动任务：{labels}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    log(f"===== {APP_NAME}｜{now_text()} =====")
    if DRY_RUN:
        log("🧪 干跑模式（YCTY_DRY_RUN=1），只查询不签到")
    log(f"   自动任务白名单：{'、'.join(task_label(t) for t in AUTO_TASKS)}")

    # Step 1: 先收集账号（WECHAT_SERVER /accounts 优先，YYB_SERVER 地址@ref 行兼容回退）
    all_accounts: List[Tuple[CodeAccount, YybCodeProvider]] = []
    for server in SERVERS:
        provider = YybCodeProvider(server)
        for account in provider.list_accounts():
            all_accounts.append((account, provider))
    if not all_accounts:
        providers: Dict[str, YybCodeProvider] = {}
        for host, ref in parse_ref_list(os.getenv("YYB_SERVER", "")):
            normalized = normalize_server(host)
            provider = providers.setdefault(normalized, YybCodeProvider(normalized))
            if ref == "1" and "@" not in os.getenv("YYB_SERVER", ""):
                # 纯服务地址行（parse_ref_list 默认 ref="1"）：GET /accounts 自动发现
                found = provider.list_accounts()
                if not found:
                    log(f"⚠️ {normalized} /accounts 未解析到账号（请确认 YYB 服务在线且已授权）")
                for account in found:
                    all_accounts.append((account, provider))
                continue
            all_accounts.append((CodeAccount(ref, ref), provider))
    if not all_accounts:
        log("❌ 未配置环境变量 YYB_SERVER（格式：yyb-go:8000@1，多账号每行一条）")
        return 1
    log(f"共 {len(all_accounts)} 个账号")

    # Step 2: 执行任务
    results: List[Dict[str, Any]] = []
    for idx, (account, provider) in enumerate(all_accounts, 1):
        _PROVIDER_CONTEXT[account.ref] = (provider, account)
        try:
            results.append(run_account(provider.server, account.ref, f"[{idx}] {account.ref}"))
        except Exception as exc:
            log(f"   ❌ 账号 {account.ref} 异常：{clean(exc, 220)}")
            results.append({"label": f"[{idx}] {account.ref}", "error": f"异常：{clean(exc, 220)}"})

    report = build_report(results)
    log("\n" + "=" * 62)
    log(report)
    log("=" * 62)

    try:
        send_notify(APP_NAME, report)
    except Exception as exc:
        log(f"⚠️ 通知发送异常（不影响结果）：{clean(exc, 160)}")

    failed = [r for r in results if not r.get("skipped") and not r.get("success")]
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n⏹️ 已手动中断")
        sys.exit(130)
