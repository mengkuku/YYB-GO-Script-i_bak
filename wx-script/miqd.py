#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 小米社区签到
# cron: 15 8,20 * * *
"""小米社区微信小程序签到（YYB-Go-Enhanced）
作者：lcmovie https://github.com/lcmovie
环境变量：
  YYB_SERVER：每行一个 server@微信账号标识，例如 yyb-go:8000@1
  YYB_API_KEY / YYB_PROTOCOL_TOKEN       启用协议鉴权时配置，二选一
  MI_COMMUNITY_APPID：可选，默认使用小米社区小程序 AppID
  MI_COMMUNITY_NOTIFY：可选，默认 1；设为 0 可关闭青龙通知
  MI_COMMUNITY_REF：可选，仅运行指定微信账号标识，便于单账号测试

依赖：requests、青龙自带 notify.py
"""

import os
import sys
import json
import uuid
import re
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from notify import send as notify_send
    NOTIFY_IMPORT_ERROR = ""
except Exception as exc:
    notify_send = None
    NOTIFY_IMPORT_ERROR = str(exc)


APPID = os.getenv("MI_COMMUNITY_APPID", "wx240a4a764023c444")
BASE = "https://api.vip.miui.com"
ACCOUNT = "https://account.xiaomi.com"
SCRIPT_NAME = "小米社区签到"
NOTIFY_ENABLED = os.getenv("MI_COMMUNITY_NOTIFY", "1").strip().lower() not in {"0", "false", "off", "no"}
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.73(0x18004939) NetType/WIFI Language/zh_CN"
FLOW_STEPS = 8
CACHE_PATH = Path(os.getenv("MI_COMMUNITY_CACHE", "/ql/data/config/mi_community_sessions.json"))
if not CACHE_PATH.parent.exists():
    CACHE_PATH = Path(__file__).resolve().with_name(".mi_community_sessions.json")

# ============ YYB 相关（CodeProvider 模式） ============
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


def yyb_auth_headers() -> Dict[str, str]:
    """YYB 协议鉴权头；未配置令牌时仅含 Content-Type。"""
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
    if api_key:
        headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key
    return headers


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
            response = self.session.get(url, timeout=20)
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
        payload = {"app_id": APPID, "ref": account.ref}
        headers = dict(yyb_auth_headers())
        headers.setdefault("User-Agent", "Mozilla/5.0 MicroMessenger/8.0.50")

        for attempt in range(1, CODE_RETRY_TIMES + 1):
            try:
                response = self.session.post(
                    url, json=payload, headers=headers, timeout=20
                )
                data = json_or_text(response)
                code = extract_code(data)
                if code:
                    return code
                print(f"{account.label} 获取 code 为空 resp={str(data)[:160]}")
            except Exception as exc:
                print(f"{account.label} 获取 code 异常：{str(exc)[:120]}")

            if attempt < CODE_RETRY_TIMES:
                import time as _time
                _time.sleep(RETRY_BASE_DELAY * attempt)

        return None
# =================================================


def flow_start(step, message):
    print(f"  [{step}/{FLOW_STEPS}] ⏳ {message}", flush=True)


def flow_ok(message):
    print(f"        ✅ {message}", flush=True)


def send_notification(lines):
    if not NOTIFY_ENABLED:
        print("🔕 已通过 MI_COMMUNITY_NOTIFY 关闭通知")
        return
    if notify_send is None:
        print(f"⚠️ 青龙通知模块 notify.py 导入失败，已跳过通知：{NOTIFY_IMPORT_ERROR}")
        return
    try:
        notify_send(SCRIPT_NAME, "\n".join(lines))
        print("📨 通知调用完成")
    except Exception as exc:
        print(f"⚠️ 通知发送失败（不影响签到结果）：{exc}")


def load_cached_session(ref):
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    account = data.get(str(ref), {}) if isinstance(data, dict) else {}
    if not isinstance(account, dict):
        return {}
    return {
        name: str(account[name])
        for name in ("passToken", "userId", "cUserId")
        if account.get(name)
    }


def save_cached_session(ref, source):
    values = {
        name: str(source[name])
        for name in ("passToken", "userId", "cUserId")
        if source.get(name)
    }
    if not values.get("passToken") or not values.get("userId"):
        return
    try:
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (FileNotFoundError, ValueError, OSError):
            data = {}
        data[str(ref)] = values
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, CACHE_PATH)
        os.chmod(CACHE_PATH, 0o600)
    except OSError as exc:
        raise RuntimeError(f"小米会话票据保存失败：{exc}") from exc


def entries():
    """兼容旧 YYB_SERVER（每行 地址@ref）；返回 [(server, ref), ...]。"""
    result = []
    selected_ref = os.getenv("MI_COMMUNITY_REF", "").strip()
    for raw in os.getenv("YYB_SERVER", "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if "@" not in raw:
            print("❌ YYB_SERVER 格式应为 地址@微信账号标识，已跳过一行")
            continue
        server, ref = raw.rsplit("@", 1)
        if server and ref and (not selected_ref or ref == selected_ref):
            result.append((server.rstrip("/"), ref))
    return result


def get_code(provider, account):
    host = provider.server
    ref = account.ref
    # 用户信息操作可能较慢，必须先完成，再最后获取短时有效的一次性 code。
    flow_start(1, "从 YYB-Go 获取微信授权信息")
    headers = yyb_auth_headers()
    user_info = requests.post(
        f"{host}/wxapp/operateWxData",
        json={
            "ref": ref,
            "app_id": APPID,
            "payload": {
                "api_name": "webapi_getuserinfo",
                "data": {"lang": "zh_CN", "version": "3.10.3"},
                "from_component": True,
                "operate_directly": False,
                "with_credentials": True,
            },
        },
        timeout=20,
        headers=headers
    )
    user_info.raise_for_status()
    user_body = user_info.json()
    user_result = user_body.get("data", {}).get("result")
    if user_body.get("code") != 0 or not isinstance(user_result, dict):
        raise RuntimeError(f"YYB 获取微信用户信息失败（响应码：{user_body.get('code')}）")
    if os.getenv("MI_COMMUNITY_DEBUG") == "1":
        print("调试：YYB用户信息字段=" + ",".join(
            f"{key}:{type(value).__name__}:{len(value) if isinstance(value, (str, list, dict)) else '-'}"
            for key, value in sorted(user_result.items())
        ))
    if not user_result.get("rawData") and isinstance(user_result.get("data"), str):
        try:
            try:
                decoded = json.loads(user_result["data"])
            except json.JSONDecodeError:
                decoded = json.loads(base64.b64decode(user_result["data"]).decode("utf-8"))
            profile = decoded.get("data", decoded)
            if isinstance(profile, str):
                profile = json.loads(profile)
            if isinstance(profile, dict):
                user_result["userInfo"] = profile
                user_result["rawData"] = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
                user_result.setdefault("errMsg", "getUserInfo:ok")
        except (ValueError, TypeError, UnicodeDecodeError):
            pass
    if user_result.get("cloud_id") and not user_result.get("cloudID"):
        user_result["cloudID"] = user_result["cloud_id"]
    if not user_result.get("rawData"):
        profile = user_result.get("userInfo") if isinstance(user_result.get("userInfo"), dict) else {}
        user_result["userInfo"] = profile
        user_result["rawData"] = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
        user_result.setdefault("errMsg", "getUserInfo:ok")
    required_user_fields = {"encryptedData", "iv", "rawData", "signature"}
    missing_user_fields = required_user_fields.difference(user_result)
    if missing_user_fields:
        raise RuntimeError(f"YYB 微信用户信息缺少字段：{','.join(sorted(missing_user_fields))}")
    user_result = {
        key: user_result[key]
        for key in ("cloudID", "encryptedData", "iv", "signature", "userInfo", "rawData", "errMsg")
        if key in user_result
    }
    flow_ok("微信授权信息获取成功")
    flow_start(2, "从 YYB-Go 获取一次性微信 code")
    code = provider.get_code(account)
    if not code:
        raise RuntimeError("YYB 获取 code 失败（重试后仍未取得）")
    flow_ok("一次性微信 code 获取成功")
    return code, user_result


def login_and_sign(code, wx_user_info, ref):
    session = requests.Session()
    session.mount(
        "https://",
        HTTPAdapter(max_retries=Retry(total=3, connect=3, read=3, backoff_factor=1, allowed_methods=None)),
    )
    session.headers.update({"User-Agent": UA, "Referer": f"https://servicewechat.com/{APPID}/73/page-frame.html"})
    session.cookies.set("deviceId", f"wp_{uuid.uuid4()}", domain="account.xiaomi.com", path="/")
    flow_start(3, "使用微信 code 登录小米账号")
    login = session.post(
        f"{ACCOUNT}/pass/sns/wxapp/v2/code",
        data={"code": code, "appid": APPID, "sid": "wx_vip", "userInfo": "true", "_locale": "zh_CN"},
        timeout=20,
    )
    login.raise_for_status()
    body = login.json()
    if body.get("code") != 0:
        raise RuntimeError(f"小米登录失败（响应码：{body.get('code')}）")
    wx_token = body.get("data", {}).get("wxSToken")
    if not wx_token:
        raise RuntimeError("小米登录响应缺少 wxSToken")
    flow_ok("微信 code 校验成功，已取得临时登录凭据")
    session.cookies.set("wxSToken", wx_token, domain="account.xiaomi.com", path="/")
    session.cookies.set(
        "userInfo",
        quote(json.dumps(wx_user_info, ensure_ascii=False, separators=(",", ":")), safe=""),
        domain="account.xiaomi.com",
        path="/",
    )
    flow_start(4, "建立小米账号会话")
    token_login = session.post(
        f"{ACCOUNT}/pass/sns/wxapp/v3/tokenLogin",
        data={"sid": "wx_vip", "appid": APPID, "callback": "", "authType": "1", "wxSToken": wx_token, "_locale": "zh_CN"},
        timeout=20,
        allow_redirects=False,
    )
    token_login.raise_for_status()
    token_body = {}
    session_tokens = {}
    if token_login.is_redirect:
        # 已绑定且此前登录过的账号，tokenLogin 可能通过 302 直接进入
        # serviceLogin。响应中的 Set-Cookie 已由 Session 自动保存，因此
        # 这里只校验重定向目标，不能把这个正常分支一律判为失败。
        redirect_url = urljoin(token_login.url, token_login.headers.get("Location", ""))
        redirect_target = urlparse(redirect_url)
        if (
            redirect_target.scheme != "https"
            or redirect_target.hostname != "account.xiaomi.com"
            or redirect_target.path.rstrip("/") != "/pass/serviceLogin"
        ):
            raise RuntimeError(f"小米会话接口发生未知重定向（HTTP {token_login.status_code}）")
        session_tokens = load_cached_session(ref)
        if not session_tokens.get("passToken") or not session_tokens.get("userId"):
            raise RuntimeError("小米账号已有登录状态，但本地缺少首次登录票据，请重新授权后再执行")
        flow_ok("小米账号已有登录状态，已加载本地会话票据")
    else:
        try:
            token_text = token_login.text.removeprefix("&&&START&&&")
            token_body = requests.models.complexjson.loads(token_text)
        except ValueError as exc:
            title_match = re.search(r"<title[^>]*>(.*?)</title>", token_login.text, re.I | re.S)
            html_title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else "无标题"
            raise RuntimeError(
                f"小米会话接口返回非 JSON（HTTP {token_login.status_code}，"
                f"类型 {token_login.headers.get('Content-Type', '未知')}，长度 {len(token_login.content)}，"
                f"页面 {html_title[:40]}）"
            ) from exc
        if not isinstance(token_body, dict) or not token_body.get("passToken"):
            raise RuntimeError("小米会话建立失败")
        session_tokens = token_body
        save_cached_session(ref, token_body)
        flow_ok("小米账号会话建立成功")

    # 小程序会把 tokenLogin 返回或本地缓存的账号票据写入 Cookie 后再取 STS。
    for name in ("passToken", "userId", "cUserId"):
        if session_tokens.get(name):
            session.cookies.set(name, str(session_tokens[name]), domain="account.xiaomi.com", path="/")
    flow_start(5, "获取小米社区 serviceLogin 跳转地址")
    service_login = session.get(
        f"{ACCOUNT}/pass/serviceLogin",
        params={"sid": "wx_vip", "_json": "true", "_locale": "zh_CN"},
        timeout=20,
    )
    service_login.raise_for_status()
    service_text = service_login.text.removeprefix("&&&START&&&")
    try:
        service_body = requests.models.complexjson.loads(service_text)
    except ValueError as exc:
        raise RuntimeError("小米 serviceLogin 返回非 JSON") from exc
    sts_url = service_body.get("location")
    if service_body.get("code") != 0 or not sts_url:
        raise RuntimeError(f"小米 serviceLogin 失败（响应码：{service_body.get('code')}）")
    save_cached_session(ref, service_body)
    flow_ok("serviceLogin 校验成功")
    flow_start(6, "执行 STS 登录并建立小米社区会话")
    sts = session.get(sts_url, timeout=20)
    sts.raise_for_status()
    if sts.json().get("S") != "OK":
        raise RuntimeError("小米 STS 登录失败")

    ph = next((cookie.value for cookie in session.cookies if cookie.name == "wx_vip_ph"), None)
    if not ph:
        raise RuntimeError("小米 STS 未返回 wx_vip_ph")
    flow_ok("STS 登录成功，社区会话已建立")

    headers = {"Origin": "https://servicewechat.com", "Content-Type": "application/x-www-form-urlencoded"}
    params = {"wx_vip_ph": ph}
    flow_start(7, "查询今日签到状态")
    status = session.get(f"{BASE}/mtop/planet/wechat/checkin/mypagedata", headers=headers, params=params, timeout=20)
    status.raise_for_status()
    status_body = status.json()
    if status_body.get("code") == 401:
        raise RuntimeError("小米登录态无效")
    buttons = status_body.get("entity", {}).get("data", [])
    already = any(item.get("title") == "每日签到" and item.get("buttons", [{}])[0].get("button") == "已签到" for item in buttons)
    if already:
        flow_ok("查询成功：今日已经签到，无需重复执行")
        flow_start(8, "检查是否需要提交签到任务")
        flow_ok("今日已签到，已跳过重复提交")
        return "今日已签到"

    flow_ok("查询成功：今日尚未签到")
    flow_start(8, "提交 WECHAT_CHECKIN_TASK 签到任务")
    sign = session.post(
        f"{BASE}/mtop/planet/wechat/member/addCommunityGrowUpPointByActionV2",
        headers=headers,
        params=params,
        data={"action": "WECHAT_CHECKIN_TASK"},
        timeout=20,
    )
    sign.raise_for_status()
    sign_body = sign.json()
    if sign_body.get("message") != "success":
        raise RuntimeError(f"签到失败（响应码：{sign_body.get('code')}）")
    entity = sign_body.get("entity", {})
    result = f"签到成功，{entity.get('title', '获得成长值')}"
    flow_ok(result)
    return result


def main():
    # Step 1: 先收集账号（WECHAT_SERVER /accounts 优先，YYB_SERVER 地址@ref 行兼容回退）
    all_accounts: List[Tuple[CodeAccount, YybCodeProvider]] = []
    for server in SERVERS:
        provider = YybCodeProvider(server)
        for account in provider.list_accounts():
            all_accounts.append((account, provider))
    if not all_accounts:
        providers: Dict[str, YybCodeProvider] = {}
        selected_ref = os.getenv("MI_COMMUNITY_REF", "").strip()
        for server, ref in entries():
            if selected_ref and ref != selected_ref:
                continue
            normalized = normalize_server(server)
            provider = providers.setdefault(normalized, YybCodeProvider(normalized))
            all_accounts.append((CodeAccount(label=ref, ref=ref), provider))
        # 只填地址（无 @ref）的行：自动从 /accounts 发现账号
        if not all_accounts:
            for line in (os.getenv("YYB_SERVER", "") or "").splitlines():
                line = line.strip()
                if not line or "@" in line:
                    continue
                normalized = normalize_server(line)
                if not normalized:
                    continue
                provider = providers.setdefault(normalized, YybCodeProvider(normalized))
                found = provider.list_accounts()
                if not found:
                    print(f"⚠️ {normalized} /accounts 未解析到账号（请确认 YYB 服务在线且已授权）")
                    continue
                for account in found:
                    if selected_ref and account.ref != selected_ref:
                        continue
                    all_accounts.append((account, provider))

    if not all_accounts:
        message = "❌ 未配置 YYB_SERVER"
        print(message)
        send_notification([message])
        return 1
    failed = 0
    results = []
    print(f"\n{'=' * 54}")
    print(f"{SCRIPT_NAME}开始，共读取到 {len(all_accounts)} 个 YYB 账号")
    print(f"{'=' * 54}")
    # Step 2: 执行任务
    for index, (account, provider) in enumerate(all_accounts, 1):
        print(f"\n{'─' * 54}")
        print(f"账号 {index}/{len(all_accounts)}｜YYB 标识：{account.ref}")
        print(f"{'─' * 54}")
        try:
            code, wx_user_info = get_code(provider, account)
            result = login_and_sign(code, wx_user_info, account.ref)
            line = f"账号 {account.ref}：✅ {result}"
            print(f"  🏁 账号 {account.ref} 流程完成：{result}")
            results.append(line)
        except Exception as exc:  # 单账号失败不影响其他账号
            failed += 1
            print(f"  🛑 账号 {account.ref} 流程终止：{exc}")
            results.append(f"账号 {account.ref}：❌ {exc}")
    print(f"\n{'=' * 54}")
    print(f"执行汇总：成功 {len(all_accounts) - failed}，失败 {failed}，共 {len(all_accounts)} 个账号")
    print(f"{'=' * 54}")
    results.extend(["", f"汇总：成功 {len(all_accounts) - failed}，失败 {failed}，共 {len(all_accounts)} 个账号"])
    send_notification(results)
    return 1 if failed == len(all_accounts) else 0


if __name__ == "__main__":
    sys.exit(main())
