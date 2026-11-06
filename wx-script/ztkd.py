# name: 中通快递
# cron: 1 8,13 * * *

"""
name:  中通快递
入口: 微信小程序
功能: 签到
变量: YYB_SERVER (YYB-Go服务地址@微信账号标识，多账号用换行分割)
    YYB_API_KEY / YYB_PROTOCOL_TOKEN (YYB-Go-Enhanced 开启协议鉴权时填写，二选一)
    PROXY_API_URL (代理api，返回一条txt文本，内容为代理ip:端口)
定时: 一天两次
cron: 1 8,13 * * *
------------更新日志------------
# name: 中通快递
2025/6/25   V1.0    初始化脚本
2025/7/7    V1.1    适配更多协议
2025/7/21   V1.2    适配更多协议
2025/7/22   V1.3    修改协议适配器导入方式
2025/7/28   V1.4    修改头部注释，以便拉库
2026/8/17   V2.0    移除 getCode 依赖，改用 YYB-Go-Enhanced 获取微信 code，更新登录协议
2026/9/23   V2.1    增加签到前后积分查询，并清晰显示积分变化
2026/9/23   V2.2    美化多账号通知，增加账号结果、积分变化和执行汇总
2026/10/11  V2.3    修复服务端开启协议鉴权后取码 401：协议请求补充 Authorization 头
"""

import random
import time
import requests
import os
import sys
import logging
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


def _yyb_auth_headers():
    """YYB-Go-Enhanced 协议接口鉴权头；未配置令牌时返回空字典，兼容未开启鉴权的服务端。"""
    _token = (os.getenv("YYB_API_KEY") or os.getenv("YYB_PROTOCOL_TOKEN") or "").strip()
    headers = {}
    if _token:
        headers["Authorization"] = "Bearer " + _token
    headers.setdefault("Content-Type", "application/json")
    return headers

MULTI_ACCOUNT_PROXY = False # 是否使用多账号代理，默认不使用，True则使用多账号代理
NOTIFY = os.getenv("LY_NOTIFY") or False # 是否推送日志，默认不推送，True则推送

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
    """与 YYB 服务交互：/accounts 发现账号 + /wxapp/getCode 取码。"""

    def __init__(self, server: str, appid: str):
        self.server = normalize_server(server)
        self.appid = appid
        self.session = requests.Session()
        self.session.verify = False

    def list_accounts(self) -> List[CodeAccount]:
        # 优先用手动配置的 YYB_REFS
        refs = [str(x).strip() for x in YYB_REFS if str(x).strip()]
        if refs:
            return [CodeAccount(label=x, ref=x) for x in refs]

        # 自动从 /accounts 获取
        if not self.server:
            return []

        url = f"{self.server}/accounts"
        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            data = json_or_text(response)
            items = extract_accounts_payload(data)
        except Exception:
            return []

        accounts: List[CodeAccount] = []
        seen = set()
        for item in items:
            ref = extract_ref(item)
            if not ref or ref in seen:
                continue
            seen.add(ref)
            accounts.append(CodeAccount(label=extract_label(item, ref), ref=ref))
        return accounts

    def get_code(self, account: CodeAccount) -> Optional[str]:
        """POST /wxapp/getCode 获取微信 code，指数退避重试"""
        if not self.server:
            return None

        url = f"{self.server}/wxapp/getCode"
        payload = {"app_id": self.appid, "ref": account.ref}
        headers = dict(_yyb_auth_headers())
        headers.setdefault("User-Agent", "Mozilla/5.0 MicroMessenger/8.0.50")

        for attempt in range(1, CODE_RETRY_TIMES + 1):
            try:
                response = self.session.post(
                    url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
                )
                data = json_or_text(response)
                code = extract_code(data)
                if code:
                    return code
            except Exception:
                pass

            if attempt < CODE_RETRY_TIMES:
                time.sleep(RETRY_BASE_DELAY * attempt)

        return None
# =================================================

class AutoTask:
    def __init__(self, site_name):
        """
        初始化自动任务类
        :param site_name: 站点名称，用于日志显示
        """
        self.site_name = site_name
        self.proxy_url = os.getenv("PROXY_API_URL") # 代理api，返回一条txt文本，内容为代理ip:端口
        self.wx_appid = "wx7ddec43d9d27276a" # 微信小程序id
        self.log_msgs = []
        self.account_results = []
        self.host = "hdgateway.zto.com"
        self.user_agent = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.75(0x18004b21) NetType/WIFI Language/zh_CN"

    def log(self, msg, level="info"):
        formatted = f"[{level.upper()}] {msg}"
        print(formatted)
        self.log_msgs.append(formatted)

    def get_wx_code(self, provider: "YybCodeProvider", account: "CodeAccount") -> Optional[str]:
        try:
            code = provider.get_code(account)
            if not code:
                self.log("[获取 code]失败，重试后仍未取得", level="error")
                return None
            self.log("[获取 code]成功")
            return code
        except Exception as e:
            self.log(f"获取 code 失败: {e}", level="error")
            return None

    def get_proxy(self):
        """
        获取代理
        :return: 代理
        """
        if not self.proxy_url:
            self.log("[获取代理]没有找到环境变量PROXY_API_URL，不使用代理", level="warning")
            return None
        url = self.proxy_url
        response = requests.get(url)
        proxy = response.text
        self.log(f"[获取代理]: {proxy}")
        return proxy

    def check_proxy(self, proxy, session):
        """
        检查代理
        :param proxy: 代理
        :param session: session
        :return: 是否可用
        """
        try:
            url = f"http://{self.host}/getApolloConfig"
            session.headers["X-Token"] = ""
            payload = {"keys":["serverTime"]}
            response = session.post(url, json=payload, timeout=5)
            if response.status_code == 200:
                self.log(f"[检查代理]: {proxy} 应该可用")
                return True
            else:
                self.log(f"[检查代理]: {response.text}")
                return False
        except Exception as e:
            return False


    def check_env(self):
        """解析旧版 YYB_SERVER（每行 地址@ref），返回 [(server, ref), ...]；兼容保留。"""
        try:
            yyb_server = os.getenv("YYB_SERVER", "")
            if not yyb_server.strip():
                self.log("[检查环境变量]没有找到 YYB_SERVER，请按 地址@微信账号标识 配置", level="error")
                return

            for line_no, raw in enumerate(yyb_server.splitlines(), 1):
                raw = raw.strip()
                if not raw:
                    continue
                if "@" not in raw:
                    self.log(f"[检查环境变量]YYB_SERVER 第{line_no}行格式错误，已跳过", level="error")
                    continue
                server, ref = raw.rsplit("@", 1)
                if not server.strip() or not ref.strip():
                    self.log(f"[检查环境变量]YYB_SERVER 第{line_no}行地址或账号标识为空，已跳过", level="error")
                    continue
                yield server.strip(), ref.strip()
        except Exception as e:
            self.log(f"[检查环境变量]发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            raise

    def wxlogin(self, session, code):
        """
        登录
        :param session: session
        :param code: 微信code
        :return: 登录结果
        """
        try:
            url = f"https://{self.host}/auth_wechatMini_authByCode"
            payload = {
                "code": code
            }
            response = session.post(url, json=payload, timeout=20)
            response.raise_for_status()
            response_json = response.json()
            if response_json['status'] == True:
                self.log(f"[登录]: {response_json['message']}")
                token = (response_json.get('result') or {}).get('token')
                if not token:
                    self.log("[登录]响应缺少 token", level="error")
                    return False
                session.headers["X-Token"] = token
                return True
            else:
                self.log(f"[登录]发生错误: {response_json['message']}", level="error")
                return False
        except requests.RequestException as e:
            self.log(f"[登录]发生网络错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False
        except Exception as e:
            self.log(f"[登录]发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False


    def sign_in(self, session):
        """
        签到
        :param session: session
        :return: 签到结果
        """
        try:
            url = f"https://membergateway.zto.com/member/activity/signIn"
            payload = {
                "signType": "TODAY_SIGN",
                "signDate": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "supplementaryScene": "null"
            }
            response = session.post(url, json=payload, timeout=20)
            response.raise_for_status()
            response_json = response.json()
            if response_json['status'] == True:
                self.log("[签到]: 成功")
                return True, "签到成功"
            else:
                message = response_json.get("message") or "签到失败"
                self.log(f"[签到]: {message}", level="warning")
                return False, message
        except Exception as e:
            self.log(f"[签到]发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False, "请求异常"

    def get_points(self, session):
        """查询当前会员积分，失败时返回 None。"""
        try:
            response = session.post(
                "https://membergateway.zto.com/member/getMemberPoints",
                json={},
                timeout=20,
            )
            response.raise_for_status()
            response_json = response.json()
            points = (response_json.get("data") or {}).get("totalPoint")
            if response_json.get("success") is True and isinstance(points, (int, float)):
                return int(points)
            self.log("[积分]查询失败：响应中没有有效积分", level="warning")
        except Exception as e:
            self.log(f"[积分]查询失败: {e}", level="warning")
        return None

    def log_points_change(self, before, after):
        """用一条清晰日志显示签到前后的积分。"""
        if before is None or after is None:
            before_text = "查询失败" if before is None else str(before)
            after_text = "查询失败" if after is None else str(after)
            self.log(f"[积分]: 初始积分 {before_text} → 完成后积分 {after_text}", level="warning")
            return
        self.log(f"[积分]: 初始积分 {before} → 完成后积分 {after}（变化 {after - before:+d}）")

    def build_notification(self, elapsed_seconds):
        """生成适合青龙通知展示的账号明细和执行汇总。"""
        normal_count = sum(1 for item in self.account_results if item["normal"])
        abnormal_count = len(self.account_results) - normal_count
        total_delta = sum(
            item["final_points"] - item["initial_points"]
            for item in self.account_results
            if item["initial_points"] is not None and item["final_points"] is not None
        )
        title = (
            f"📦 {self.site_name}签到｜正常 {normal_count}｜"
            f"异常 {abnormal_count}｜积分 {total_delta:+d}"
        )
        sections = [f"📦 {self.site_name}签到", ""]
        for item in self.account_results:
            sections.extend(["━━━━━━━━━━━━━━", f"👤 账号 {item['index']}"])
            login_icon = "✅" if item["login"] == "认证成功" else "❌"
            sections.append(f"{login_icon} 登录：{item['login']}")
            if item["sign"] == "签到成功":
                sign_icon = "✅"
            elif item["sign"] == "今日已签到":
                sign_icon = "ℹ️"
            else:
                sign_icon = "❌"
            sections.append(f"{sign_icon} 签到：{item['sign']}")
            before = item["initial_points"]
            after = item["final_points"]
            if before is None or after is None:
                sections.append("⚠️ 积分：查询失败")
            else:
                sections.append(f"💰 积分：{before} → {after}（{after - before:+d}）")
            sections.append("")
        sections.extend([
            "━━━━━━━━━━━━━━",
            "📊 执行汇总",
            f"账号总数：{len(self.account_results)}",
            f"✅ 正常：{normal_count}",
            f"⚠️ 异常：{abnormal_count}",
            f"💰 本次增加：{total_delta} 积分",
            f"⏱️ 执行耗时：{elapsed_seconds} 秒",
        ])
        return title, "\n".join(sections)

    def run(self):
        """
        运行任务
        """
        started_at = time.monotonic()
        notify = None
        try:
            # 如果notify模块不存在，从远程下载至本地
            if not os.path.exists("notify.py"):
                url = "https://raw.githubusercontent.com/whyour/qinglong/refs/heads/develop/sample/notify.py"
                response = requests.get(url)
                with open("notify.py", "w", encoding="utf-8") as f:
                    f.write(response.text)
                import notify
            else:
                import notify

            self.log(f"【{self.site_name}】开始执行任务")

            # Step 1: 先收集账号（WECHAT_SERVER /accounts 优先，YYB_SERVER 地址@ref 行兼容回退）
            all_accounts: List[Tuple[CodeAccount, YybCodeProvider]] = []
            for server in SERVERS:
                provider = YybCodeProvider(server, self.wx_appid)
                for account in provider.list_accounts():
                    all_accounts.append((account, provider))
            if not all_accounts:
                providers: Dict[str, YybCodeProvider] = {}
                for server, ref in self.check_env():
                    normalized = normalize_server(server)
                    provider = providers.setdefault(normalized, YybCodeProvider(normalized, self.wx_appid))
                    all_accounts.append((CodeAccount(label=ref, ref=ref), provider))

            # Step 2: 执行任务
            for index, (account, provider) in enumerate(all_accounts, 1):
                self.log("")
                self.log(f"------ 【账号{index}】开始执行任务 ------")

                if MULTI_ACCOUNT_PROXY:
                    proxy = self.get_proxy()
                    if proxy:
                        session = requests.Session()
                        session.proxies.update({"http": f"http://{proxy}", "https": f"http://{proxy}"})
                        # 检查代理，不可用重新获取
                        while not self.check_proxy(proxy, session):
                            proxy = self.get_proxy()
                            session.proxies.update({"http": f"http://{proxy}", "https": f"http://{proxy}"})
                    else:
                        session = requests.Session()
                else:
                    session = requests.Session()

                session.headers.update({
                    "Content-Type": "application/json",
                    "User-Agent": self.user_agent,
                    "Referer": f"https://servicewechat.com/{self.wx_appid}/693/page-frame.html",
                    "x-sv-v": "0.22.0",
                    "x-version": "V8.160.1",
                    "x-clientCode": "wechatMiniZtoHelper",
                })

                # 执行微信授权
                code = self.get_wx_code(provider, account)
                if not code:
                    self.account_results.append({
                        "index": index,
                        "login": "获取微信 code 失败",
                        "sign": "未执行",
                        "initial_points": None,
                        "final_points": None,
                        "normal": False,
                    })
                    self.log(f"------ 【账号{index}】执行任务完成 ------")
                    continue

                login_result = self.wxlogin(session, code)
                time.sleep(random.randint(1, 3))
                if not login_result:
                    self.account_results.append({
                        "index": index,
                        "login": "认证失败",
                        "sign": "未执行",
                        "initial_points": None,
                        "final_points": None,
                        "normal": False,
                    })
                    self.log(f"------ 【账号{index}】执行任务完成 ------")
                    continue

                initial_points = self.get_points(session)
                sign_success, sign_message = self.sign_in(session)
                time.sleep(random.randint(1, 3))
                final_points = self.get_points(session)
                self.log_points_change(initial_points, final_points)
                self.account_results.append({
                    "index": index,
                    "login": "认证成功",
                    "sign": sign_message,
                    "initial_points": initial_points,
                    "final_points": final_points,
                    "normal": sign_success or sign_message == "今日已签到",
                })

                self.log(f"------ 【账号{index}】执行任务完成 ------")
        except Exception as e:
            self.log(f"【{self.site_name}】执行过程中发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
        finally:
            elapsed_seconds = round(time.monotonic() - started_at)
            title, content = self.build_notification(elapsed_seconds)
            print(f"\n{content}")
            if notify is None:
                self.log("[通知]通知模块加载失败，未发送通知", level="warning")
            else:
                try:
                    notify.send(title, content)
                except Exception as e:
                    self.log(f"[通知]发送失败: {e}", level="warning")

if __name__ == "__main__":
    auto_task = AutoTask("中通快递")
    auto_task.run()
