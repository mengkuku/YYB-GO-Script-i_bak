#!/usr/bin/env python3
# name: 铛铛一下
# cron: 32 16,4 * * *
# -*- coding: utf-8 -*-

"""
铛铛一下旧衣服回收动态 code 版

功能：
  1. 多地址本地服务获取微信 code（从环境变量 YYB_SERVER 读取，换行分隔）
  2. /wechat/login 使用 code 换 token
  3. 每日签到
  4. 抽奖
  5. 查询余额
  6. 满 0.3 自动提现
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  WECHAT_SERVER          推荐：YYB 服务地址（逗号分隔多实例），自动从 /accounts 发现账号
  YYB_SERVER             兼容旧格式：每行 地址@微信账号标识；未配置 WECHAT_SERVER 时使用
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  DD1X_CHANNEL_ID   渠道 ID，默认 154

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import json
import os
import random
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests


APP_NAME = "铛铛一下旧衣服回收小程序"
APPID = "wxe378d2d7636c180e"

# ============ YYB 相关（CodeProvider 模式） ============
# 服务地址优先级：WECHAT_SERVER 环境变量 > YYB_SERVER（地址@ref 行，兼容旧配置）
WECHAT_SERVER = os.getenv("WECHAT_SERVER", "").strip()
SERVERS = [s.strip() for s in WECHAT_SERVER.split(",") if s.strip()]

YYB_REFS: List[str] = []  # 空=自动从 /accounts 获取，非空=只用这里指定的 ref

CODE_RETRY_TIMES = 3      # 获取 code 最大重试次数
RETRY_BASE_DELAY = 3      # 指数退避基数（秒）
# =================================================

CHANNEL_ID = os.getenv("DD1X_CHANNEL_ID", "154")

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

BASE_URL = "https://vues.dd1x.cn"
LOGIN_URL = f"{BASE_URL}/wechat/login"

SIGN_JOIN_URL = f"{BASE_URL}/api/v2/sign_join"
LOTTERY_INFO_URL = f"{BASE_URL}/front/activity/get_lottery_info?id=13&channelId={CHANNEL_ID}"
LOTTERY_RESULT_URL = f"{BASE_URL}/front/activity/get_lottery_result?id=13"
LOTTERY_UPDATE_URL = f"{BASE_URL}/front/activity/update_lottery_result"
ACCOUNT_DETAIL_URL = f"{BASE_URL}/api/h/get_account_detailed"
WITHDRAWAL_TRADE_LIST_URL = f"{BASE_URL}/api/h/get_withdrawal_trade_list"
WITHDRAWAL_URL = f"{BASE_URL}/api/h/withdrawal"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541923) XWEB/19823"
)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def mask(value: Any) -> str:
    value = str(value or "")
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-6:]}"


def json_preview(data: Any, limit: int = 800) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)[:limit]
    except Exception:
        return str(data)[:limit]


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def log_title(total_accounts: int = 0) -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ ♻️ 铛铛一下旧衣服回收动态 code 版             ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {total_accounts:<34}║")
    print("╚" + "═" * 50 + "╝")


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


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


def normalize_server(server: str) -> str:
    """标准化服务地址：去末尾斜杠，补 http:// 前缀"""
    server = str(server or "").strip().rstrip("/")
    if not server.startswith("http://") and not server.startswith("https://"):
        server = "http://" + server
    return server


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

    def get_code_body(self, account: CodeAccount) -> Optional[Dict[str, Any]]:
        """POST /wxapp/getCode，返回完整响应 dict（含 code/openid 等字段），重试耗尽返回 None"""
        if not self.server:
            print("未配置 YYB 服务地址，无法请求 /wxapp/getCode")
            return None

        url = f"{self.server}/wxapp/getCode"
        payload = {"app_id": APPID, "ref": account.ref}
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
                if isinstance(data, dict) and extract_code(data):
                    print(f"{mask(account.label)} 获取 wx code 成功")
                    return data
                print(f"{mask(account.label)} 获取 code 为空 resp={str(data)[:160]}")
            except Exception as exc:
                print(f"{mask(account.label)} 获取 code 异常：{str(exc)[:120]}")

            if attempt < CODE_RETRY_TIMES:
                sleep(RETRY_BASE_DELAY * attempt)

        return None

    def get_code(self, account: CodeAccount) -> Optional[str]:
        """POST /wxapp/getCode 获取微信 code，指数退避重试"""
        body = self.get_code_body(account)
        if not body:
            return None
        return extract_code(body)


def parse_proxy_response(text: Any) -> Dict[str, Any] | None:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)

    text = text.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        proxy_obj = None

        if isinstance(data.get("data"), list) and data["data"]:
            proxy_obj = data["data"][0]
        elif isinstance(data.get("data"), dict):
            inner = data["data"]
            # 支持嵌套 list 字段（如 data.list / data.rows）
            for list_key in ("list", "rows", "data", "result", "proxies", "items"):
                nested = inner.get(list_key)
                if isinstance(nested, list) and nested:
                    proxy_obj = nested[0]
                    break
            if proxy_obj is None:
                proxy_obj = inner
        elif data.get("ip") and data.get("port"):
            proxy_obj = data
        elif isinstance(data.get("result"), dict):
            proxy_obj = data["result"]

        if proxy_obj:
            host = proxy_obj.get("ip") or proxy_obj.get("host")
            port = proxy_obj.get("port")
            if host and port:
                return {
                    "host": str(host),
                    "port": int(port),
                    "username": proxy_obj.get("user") or proxy_obj.get("username") or "",
                    "password": proxy_obj.get("pass") or proxy_obj.get("password") or "",
                }
    except Exception:
        pass

    # 兜底：只对非 JSON 文本做 host:port 解析，并防御 int 异常
    if ":" in text and not text.startswith("{"):
        parts = text.split(":")
        if len(parts) >= 2:
            try:
                port = int(parts[1])
            except (ValueError, TypeError):
                return None
            return {
                "host": parts[0],
                "port": port,
                "username": parts[2] if len(parts) > 2 else "",
                "password": parts[3] if len(parts) > 3 else "",
            }

    return None


def build_proxy_dict(proxy_info: Dict[str, Any] | None) -> Dict[str, str] | None:
    if not proxy_info:
        return None

    host = proxy_info["host"]
    port = proxy_info["port"]
    username = proxy_info.get("username", "")
    password = proxy_info.get("password", "")

    auth = ""
    if username and password:
        auth = f"{quote(username)}:{quote(password)}@"

    scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
    proxy_url = f"{scheme}://{auth}{host}:{port}"

    print(f"🛠️ [代理] 生成 {scheme.upper()} 代理 {host}:{port}")

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def validate_proxy(proxies: Dict[str, str] | None) -> Tuple[bool, str]:
    if not proxies:
        return False, ""

    try:
        response = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
        if response.status_code == 200:
            try:
                ip = response.json().get("origin", "未知")
            except Exception:
                ip = "未知"
            print(f"✅ [代理] 验证通过，出口 IP: {ip}")
            return True, ip
    except Exception as exc:
        print(f"⚠️ [代理] 验证失败: {exc}")

    return False, ""


def get_valid_proxy(account_name: str) -> Tuple[Dict[str, str] | None, str]:
    if not PROXY_API:
        print(f"⚠️ [代理] {account_name} 未配置 PROXY_API，使用直连")
        return None, ""

    print(f"🌐 [代理] {account_name} 正在获取品赞代理...")

    for index in range(1, PROXY_RETRY_TIMES + 1):
        try:
            response = direct_session().get(PROXY_API, timeout=15)
            proxy_info = parse_proxy_response(response.text)

            if not proxy_info:
                print(f"⚠️ [代理] 第 {index} 次代理解析失败")
                continue

            print(f"✅ [代理] 提取到 {proxy_info['host']}:{proxy_info['port']}")
            proxies = build_proxy_dict(proxy_info)

            ok, ip = validate_proxy(proxies)
            if ok:
                return proxies, ip

            print(f"⚠️ [代理] 第 {index} 次代理不可用")
        except Exception as exc:
            print(f"⚠️ [代理] 第 {index} 次获取代理异常: {exc}")

        if index < PROXY_RETRY_TIMES:
            sleep(2)

    print("⚠️ [代理] 获取失败，使用直连")
    return None, ""


def request_with_proxy(
    method: str,
    url: str,
    *,
    proxies: Dict[str, str] | None = None,
    server: str = "",
    **kwargs,
) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            print(f"⚠️ [代理] {server} 代理请求失败: {exc}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
            print("🔁 [兜底] 切换直连重试")

    session = direct_session()
    return session.request(method, url, **kwargs)


def send_pushplus(title: str, content: str) -> None:
    if not PLUSPLUS_TOKEN:
        print("⚠️ [PushPlus] 未配置 PLUSPLUS_TOKEN，跳过推送")
        return

    try:
        requests.post(
            "https://www.pushplus.plus/send",
            json={
                "token": PLUSPLUS_TOKEN,
                "title": title,
                "content": content,
                "template": "txt",
            },
            timeout=10,
        )
        print("✅ [PushPlus] 推送成功")
    except Exception as exc:
        print(f"❌ [PushPlus] 推送失败: {exc}")


def parse_yyb_go_entry(raw_value):
    """解析旧版 YYB_SERVER 行（地址@ref），返回 (server, ref)；无 @ 视为纯服务地址。"""
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return None, None

    if "@" not in raw_value:
        server = normalize_server(raw_value)
        return server, None

    server, ref = raw_value.rsplit("@", 1)
    server = normalize_server(server.strip())
    ref = ref.strip()

    if not server or not ref:
        print(f"❌ 配置错误：YYB_SERVER 缺少地址或微信账号标识，当前值：{raw_value}")
        return None, None

    return server, ref


def load_legacy_accounts() -> List[Tuple[CodeAccount, YybCodeProvider]]:
    """从旧版 YYB_SERVER（每行 地址@ref）构建账号列表，兼容存量青龙配置。"""
    pairs: List[Tuple[CodeAccount, YybCodeProvider]] = []
    providers: Dict[str, YybCodeProvider] = {}
    for line in os.getenv("YYB_SERVER", "").splitlines():
        line = line.strip()
        if not line:
            continue
        server, ref = parse_yyb_go_entry(line)
        if not server or not ref:
            continue
        provider = providers.setdefault(server, YybCodeProvider(server))
        pairs.append((CodeAccount(label=ref, ref=ref), provider))
    return pairs

def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/824/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["token"] = token
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("accessToken"),
        data.get("access_token"),
        data.get("jwt"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("token"),
            inner.get("accessToken"),
            inner.get("access_token"),
            inner.get("jwt"),
        ])

        user = inner.get("user")
        if isinstance(user, dict):
            candidates.extend([
                user.get("token"),
                user.get("accessToken"),
                user.get("access_token"),
                user.get("jwt"),
            ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        response = request_with_proxy(
            "GET",
            LOGIN_URL,
            headers=common_headers(),
            params={
                "code": code,
                "channelId": CHANNEL_ID,
            },
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        token = extract_token(data)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token),
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token),
        json=payload,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


def normalize_trade_list(resp: Any) -> List[Dict[str, Any]]:
    if isinstance(resp, list):
        return [item for item in resp if isinstance(item, dict)]

    if not isinstance(resp, dict):
        return []

    data = resp.get("data")

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if isinstance(data, dict):
        for key in ("list", "records", "tradeList", "withdrawalDetailPojoList"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    return []


def build_withdraw_payload(trade_resp: Any) -> Tuple[Dict[str, Any] | None, str]:
    trade_list = normalize_trade_list(trade_resp)

    if not trade_list:
        return None, "没有可提现订单数据"

    available = []
    for item in trade_list:
        if item.get("disabled") is True:
            continue

        money = to_float(item.get("money"))
        if money <= 0:
            continue

        available.append(item)

    if not available:
        return None, "没有可提现订单"

    total_money = round(sum(to_float(item.get("money")) for item in available), 2)

    if total_money < 0.3:
        return None, f"可提现金额 {total_money:.2f} 元，未满 0.3 元"

    return {
        "totalMoney": f"{total_money:.2f}",
        "type": 1,
        "withdrawalDetailPojoList": available,
    }, f"可提现订单 {len(available)} 个，合计 {total_money:.2f} 元"


def run_account(account: CodeAccount, provider: YybCodeProvider) -> Dict[str, Any]:
    server = provider.server
    result = {
        "server": account.label,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "lotteryMsg": "-",
        "balance": "-",
        "withdrawMsg": "-",
        "error": "",
    }

    proxies, proxy_ip = get_valid_proxy(account.label)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    code = provider.get_code(account)
    if not code:
        result["error"] = "获取 code 失败"
        return result

    token, raw_login = login_by_code(server, code, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        sign_resp = api_get(server, SIGN_JOIN_URL, token, proxies)
        if sign_resp.get("code") == 0:
            sign_name = sign_resp.get("data", {}).get("name", "签到成功")
            result["signMsg"] = f"每日签到: {sign_name}"
            print(f"✅ [签到] {result['signMsg']}")
        else:
            result["signMsg"] = sign_resp.get("msg") or sign_resp.get("message") or "签到失败"
            print(f"⚠️ [签到] {result['signMsg']}")

        lottery_info = api_get(server, LOTTERY_INFO_URL, token, proxies)
        lottery_data = lottery_info.get("data")
        if not isinstance(lottery_data, dict):
            lottery_data = {}
        member_count = int(lottery_data.get("member_count", 0) or 0)
        print(f"🎰 [抽奖] 当前可抽奖 {member_count} 次")

        prize_list: List[str] = []
        for draw_index in range(1, member_count + 1):
            wait_time = random.randint(2, 5)
            print(f"⏳ [抽奖] 第 {draw_index} 次抽奖前等待 {wait_time}s")
            sleep(wait_time)

            draw_resp = api_get(server, LOTTERY_RESULT_URL, token, proxies)
            if draw_resp.get("code") != 0:
                msg = draw_resp.get("msg") or draw_resp.get("message") or "抽奖失败"
                prize_list.append(f"第{draw_index}次失败: {msg}")
                print(f"❌ [抽奖] {msg}")
                continue

            data = draw_resp.get("data") or {}
            prize_name = data.get("prizeName") or data.get("goodName") or "未知奖品"
            record_id = data.get("record_id") or data.get("recordId") or data.get("id")
            prize_list.append(prize_name)
            print(f"✅ [抽奖] 第 {draw_index} 次获得: {prize_name}")

            if record_id:
                update_url = f"{LOTTERY_UPDATE_URL}?id={quote(str(record_id))}"
                update_resp = api_get(server, update_url, token, proxies)
                if update_resp.get("code") == 0:
                    print("✅ [抽奖] 奖品结果确认成功")
                else:
                    print(f"⚠️ [抽奖] 奖品结果确认失败: {json_preview(update_resp, 300)}")

        result["lotteryMsg"] = "、".join(prize_list) if prize_list else f"{member_count} 次机会"

        account_resp = api_get(server, ACCOUNT_DETAIL_URL, token, proxies)
        account_data = account_resp.get("data")
        if not isinstance(account_data, dict):
            account_data = {}
        total_raw = account_data.get("total", 0)
        total = to_float(total_raw)

        result["balance"] = str(total_raw)
        print(f"💰 [余额] 当前总金额: {total_raw} 元")

        if total < 0.3:
            result["withdrawMsg"] = "余额不足 0.3 元，跳过提现"
            print(f"⚠️ [提现] {result['withdrawMsg']}")
            result["success"] = True
            return result

        trade_resp = api_get(server, WITHDRAWAL_TRADE_LIST_URL, token, proxies)
        payload, withdraw_prepare_msg = build_withdraw_payload(trade_resp)
        print(f"💸 [提现] {withdraw_prepare_msg}")

        if not payload:
            result["withdrawMsg"] = withdraw_prepare_msg
            result["success"] = True
            return result

        withdraw_resp = api_post(server, WITHDRAWAL_URL, token, proxies, payload)
        result["withdrawMsg"] = (
            withdraw_resp.get("msg")
            or withdraw_resp.get("message")
            or json_preview(withdraw_resp)
        )
        print(f"💸 [提现] {result['withdrawMsg']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""♻️ 铛铛一下旧衣服回收多账号任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🌍 来源：{res["server"]}
🌐 代理：{res["proxyStatus"]}
📡 出口IP：{res["proxyIp"]}
🔐 Token：{res["token"]}
📝 签到：{res["signMsg"]}
🎰 抽奖：{res["lotteryMsg"]}
💰 余额：{res["balance"]} 元
💸 提现：{res["withdrawMsg"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    if not SERVERS and not os.getenv("YYB_SERVER", "").strip():
        print("❌ 未配置 code 服务，请设置 WECHAT_SERVER 或 YYB_SERVER 环境变量")
        return

    # Step 1: 先加载所有账号，再打标题（保证账号数量正确）
    all_accounts: List[Tuple[CodeAccount, YybCodeProvider]] = []
    for server in SERVERS:
        provider = YybCodeProvider(server)
        for account in provider.list_accounts():
            all_accounts.append((account, provider))
    if not all_accounts:
        # 兼容旧 YYB_SERVER（地址@ref 行）配置
        all_accounts = load_legacy_accounts()

    log_title(len(all_accounts))

    if not all_accounts:
        print("❌ 未获取到任何账号")
        return

    # Step 2: 执行任务
    results: List[Dict[str, Any]] = []

    for index, (account, provider) in enumerate(all_accounts, 1):
        print()
        print("┌" + "─" * 50 + "┐")
        print(f"│ 🧩 账号 {index} / {len(all_accounts):<37}│")
        print(f"│ 🌍 来源 {account.label:<40}│")
        print("└" + "─" * 50 + "┘")
        try:
            result = run_account(account, provider)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {account.label} 执行异常: {exc}")
            results.append({
                "server": account.label,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "signMsg": "-",
                "lotteryMsg": "-",
                "balance": "-",
                "withdrawMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(all_accounts):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 铛铛一下任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("♻️ 铛铛一下多账号任务完成", build_notify(results))


if __name__ == "__main__":
    main()
