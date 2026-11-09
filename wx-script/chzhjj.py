#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
name: 长虹智慧家居签到
cron: 18 8 * * *
依赖: requests；通知使用青龙自带 notify.py。
YYB_SERVER: 服务地址@账号ID或OpenID，多账号换行；可直接复用现有变量。
YYB_API_KEY / YYB_PROTOCOL_TOKEN: 启用协议鉴权时配置，二选一
CH_AGGR_ID: 可选，手动指定签到活动ID；默认从首页“签到”菜单发现。
CH_NOTIFY: 默认1，设0关闭通知。
CH_IPV4_ONLY: 默认1；规避部分容器AAAA解析失败，设0恢复双栈解析。
作者：lcmovie https://github.com/lcmovie
"""

import base64
import importlib
import json
import os
import re
import sys
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
import urllib3.util.connection

APP_ID = "wx36c3413e8fe39263"
BASE = "https://hongke.changhong.com/gw/applet"
TITLE = "长虹智慧家居签到"

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
            response = self.session.get(url, timeout=35)
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
        payload = {"app_id": APP_ID, "ref": account.ref}
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
                    url, json=payload, headers=headers, timeout=35
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


def configure_network():
    # ql2 实测：AF_UNSPEC/AAAA解析EAI_AGAIN，A记录及IPv4 HTTPS正常。
    # 仅影响当前脚本进程；不硬编码IP、不关闭TLS验证。
    if os.getenv("CH_IPV4_ONLY", "1") != "0":
        urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET


class TaskError(Exception):
    pass


def request(session, method, url, label, **kwargs):
    try:
        response = session.request(method, url, timeout=(10, 35),
                                   allow_redirects=False, **kwargs)
        if response.status_code != 200:
            raise TaskError(f"{label}：HTTP {response.status_code}")
        result = response.json()
        if not isinstance(result, dict):
            raise TaskError(f"{label}：响应格式异常")
        return result
    except (requests.RequestException, ValueError) as exc:
        # 不打印可能含 URL、凭据或个人信息的原始异常及服务端原文。
        detail = str(exc)
        reason = "DNS解析失败" if "NameResolutionError" in detail or "Failed to resolve" in detail else type(exc).__name__
        raise TaskError(f"{label}：网络或JSON异常（{reason}）") from None


def parse_entry(line):
    if "@" not in line:
        raise TaskError("YYB_SERVER 格式应为 服务地址@账号标识")
    server, ref = (part.strip() for part in line.rsplit("@", 1))
    if not server.startswith(("http://", "https://")):
        server = "http://" + server
    u = urlsplit(server)
    if not u.hostname or not ref or u.query or u.fragment or u.username:
        raise TaskError("YYB_SERVER 地址或账号标识无效")
    return server.rstrip("/"), ref


class Changhong:
    def __init__(self, server, ref, provider=None):
        self.server, self.ref = normalize_server(server), ref
        self.provider = provider
        self.yyb = requests.Session()
        self.yyb.trust_env = False
        self.web = requests.Session()
        self.web.headers.update({
            "content-type": "application/json",
            "Referer": f"https://servicewechat.com/{APP_ID}/330/page-frame.html",
            "User-Agent": "Mozilla/5.0 MicroMessenger/8.0.50 MiniProgramEnv/iOS",
        })

    def close(self):
        self.yyb.close()
        self.web.close()

    def yyb_call(self, endpoint, **extra):
        headers = {
            "Content-Type": "application/json"
        }
        api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
        if api_key:
            headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key
        body = request(self.yyb, "POST", self.server + "/wxapp/" + endpoint,
                       "YYB " + endpoint,
                       json={"ref": self.ref, "app_id": APP_ID, **extra}, headers=headers)
        result = (body.get("data") or {}).get("result")
        if body.get("code") != 0 or not isinstance(result, dict):
            raise TaskError(f"YYB {endpoint}失败，请检查账号授权及YYB日志")
        return result

    def api(self, method, path, **kwargs):
        body = request(self.web, method, BASE + path, path, **kwargs)
        if str(body.get("code")) != "200":
            raise TaskError(f"{path}：业务请求失败，请检查小程序登录/授权/活动状态")
        return body.get("data")

    def login(self):
        # 用户资料为辅助字段；不把YYB宿主OpenID误当作长虹小程序OpenID。
        profile, info = {}, {}
        try:
            info = self.yyb_call("operateWxData", payload={
                "api_name": "webapi_getuserinfo", "data": {"lang": "zh_CN"},
                "with_credentials": True, "from_component": True,
                "operate_directly": False,
            })
            profile = info.get("userInfo") or {}
            if not profile and info.get("rawData"):
                profile = json.loads(info["rawData"])
            if not profile and info.get("data"):
                raw = info["data"]
                if isinstance(raw, dict):
                    profile = raw
                else:
                    try:
                        profile = json.loads(raw)
                    except (ValueError, TypeError):
                        profile = json.loads(base64.b64decode(raw))
            if not isinstance(profile, dict):
                profile = {}
        except (TaskError, ValueError, TypeError):
            print("  微信资料不可用，将由长虹服务端通过新code识别账号")
        # getCode 走 provider（重试+退避+鉴权头），失败回落直连 yyb_call
        if self.provider is not None:
            code = self.provider.get_code(CodeAccount(label=self.ref, ref=self.ref)) or ""
        else:
            code = self.yyb_call("getCode").get("code")
        if not isinstance(code, str) or not code.strip():
            raise TaskError("YYB未返回有效的wx.login code")
        common = {
            "jsCode": code, "invitation": "", "system": "iOS 16.0",
            "userName": profile.get("nickName", ""),
            "sex": {1: "男", 2: "女"}.get(profile.get("gender"), "未知"),
            "avatarUrl": profile.get("avatarUrl", ""),
        }
        # HAR 先通过此接口换取长虹小程序身份，再提交手机号授权。
        identity = self.api("POST", "/appletUser/getTokenByJsCode", json={
            **common, "iv": info.get("iv", ""),
        }, headers={"token": "", "smarthome": ""})
        if not isinstance(identity, dict):
            raise TaskError("长虹身份响应格式异常")
        data = identity
        if not data.get("token"):
            if not identity.get("openId") or not identity.get("unionId"):
                raise TaskError("长虹未返回小程序OpenID/UnionID，请检查微信授权")
            phone = self.yyb_call("getPhoneNumber")
            encrypted = phone.get("encryptedData") or phone.get("encrypted_data")
            if not all(isinstance(v, str) and v for v in
                       (phone.get("code"), encrypted, phone.get("iv"))):
                raise TaskError("YYB手机号授权数据不完整，请先在小程序授权手机号")
            # HAR 中第二步沿用第一步jsCode，由业务端处理；不再次向微信兑换。
            data = self.api("POST", "/appletUser/getTokenByCode", json={
                **common, "encryptedData": encrypted, "iv": phone["iv"],
                "code": phone["code"], "openId": identity["openId"],
                "unionId": identity["unionId"],
            }, headers={"token": "", "smarthome": ""})
        if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
            raise TaskError("长虹登录未返回token，需核对授权字段")
        # 实测静默登录返回token时isRegistered=0；手机号登录HAR为1。
        # 不能把该分支标记当作登录成败，必须用token访问业务接口验证。
        self.web.headers.update({"token": data["token"], "smarthome": data["token"]})
        # 实际业务查询验证登录态，不能仅凭拿到code/token判定成功。
        self.api("GET", "/mine/getAppletUser")

    def activity_id(self):
        explicit = os.getenv("CH_AGGR_ID", "").strip()
        if explicit:
            return explicit
        menu = self.api("POST", "/homePage/getShortcutMenuList")
        found = set()

        def walk(value):
            if isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                if "签到" in str(value.get("name", "")):
                    match = re.search(r"activityData=([A-Za-z0-9_-]+)", str(value.get("webUrl", "")))
                    if match:
                        found.add(match.group(1))
                for item in value.values():
                    if isinstance(item, (list, dict)):
                        walk(item)

        walk(menu)
        if len(found) != 1:
            raise TaskError("首页未发现唯一签到活动，请设置 CH_AGGR_ID")
        return found.pop()

    def state(self, aggr_id):
        data = self.api("GET", "/aggr/aggregationInfo", params={"aggrId": aggr_id})
        if not isinstance(data, dict):
            raise TaskError("活动详情格式异常")
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        if not (str(data.get("startTime", ""))[:10] <= today <= str(data.get("endTime", ""))[:10]):
            raise TaskError("活动未开始或已结束，请更新活动入口")
        if str(data.get("status")) != "1" or str(data.get("isCan")) != "1":
            raise TaskError("当前账号无法参与该活动")
        signs = [x["aggrAssemblySignin"] for x in data.get("aggrAssemblyList", [])
                 if isinstance(x, dict) and isinstance(x.get("aggrAssemblySignin"), dict)]
        if len(signs) != 1 or signs[0].get("isSignin") not in (0, 1):
            raise TaskError("签到组件或状态无法识别")
        return signs[0]

    def run(self):
        self.login()
        aggr_id = self.activity_id()
        before = self.state(aggr_id)
        if before["isSignin"] == 1:
            status = "今日已签到"
        else:
            error = None
            try:
                self.api("POST", "/aggr/signin", params={"aggrId": aggr_id})
            except TaskError as exc:
                error = exc
            # 签到POST超时也只查询结果，不盲目重复提交。
            if self.state(aggr_id)["isSignin"] != 1:
                raise TaskError(str(error) if error else "签到后状态未变为已签到")
            status = "签到成功（已复查）"
        try:
            points = self.api("GET", "/homePage/getUserPoint")
            # 实测刚签到时可能暂返0，稍后查询恢复实际余额。
            if before["isSignin"] == 0 and points == 0:
                for delay in (2, 4):
                    time.sleep(delay)
                    points = self.api("GET", "/homePage/getUserPoint")
                    if points != 0:
                        break
            if isinstance(points, (int, float)) and not isinstance(points, bool):
                status += f"，当前积分：{points}"
                if before["isSignin"] == 0 and points == 0:
                    status += "（接口可能尚未更新，请稍后查看）"
            else:
                status += "，积分格式无法识别"
        except TaskError:
            status += "，积分查询失败"
        return status


def notify(message):
    if os.getenv("CH_NOTIFY", "1") == "0":
        return
    for folder in (Path(__file__).resolve().parent, Path("/ql/data/scripts"), Path("/ql/scripts")):
        if str(folder) not in sys.path:
            sys.path.append(str(folder))
    try:
        importlib.import_module("notify").send(TITLE, message)
        print("青龙通知模块调用完成（送达情况以通知渠道为准）")
    except Exception as exc:
        print(f"青龙通知不可用或调用失败（{type(exc).__name__}），不影响签到结果")


def main():
    configure_network()
    # Step 1: 先收集账号（WECHAT_SERVER /accounts 优先，YYB_SERVER 地址@ref 行兼容回退）
    all_accounts: List[Tuple[CodeAccount, YybCodeProvider]] = []
    for server in SERVERS:
        provider = YybCodeProvider(server)
        for account in provider.list_accounts():
            all_accounts.append((account, provider))
    if not all_accounts:
        providers: Dict[str, YybCodeProvider] = {}
        for line in os.getenv("YYB_SERVER", "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                server, ref = parse_entry(line)
            except TaskError:
                continue
            provider = providers.setdefault(server, YybCodeProvider(server))
            all_accounts.append((CodeAccount(label=ref, ref=ref), provider))

    results, failed = [], False
    if not all_accounts:
        results.append("未配置 YYB_SERVER")
        failed = True
    # Step 2: 执行任务
    for index, (account, provider) in enumerate(all_accounts, 1):
        client = None
        try:
            client = Changhong(provider.server, account.ref, provider)
            result = client.run()
        except TaskError as exc:
            result, failed = str(exc), True
        except Exception as exc:
            result, failed = f"处理异常（{type(exc).__name__}）", True
        finally:
            if client:
                client.close()
        results.append(f"账号{index}：{result}")
        print(results[-1])
    if not all_accounts:
        print(results[0])
    notify("\n".join(results))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
