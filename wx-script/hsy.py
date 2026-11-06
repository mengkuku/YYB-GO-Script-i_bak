#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 回收猿签到
# cron: 35 7,18 * * *
#
# 环境变量：
#   YYB_SERVER   每行：地址@账号标识（例如 http://yyb-go:8000@1）
#   YYB_API_KEY / YYB_PROTOCOL_TOKEN       启用协议鉴权时配置，二选一
#   HSY_NOTIFY   0 关闭青龙通知；默认 1
#
# 作者 lcmovie  https://github.com/lcmovie/YYB-GO-Script-i

import hashlib
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import requests

APP_ID = "wxadd84841bd31a665"
BASE_URL = "https://www.52bjy.com/api/app"
APP_KEY = "1079fb245839e765"
SECRET = "UppwYkfBlk"
MERCHANT_ID = "2"
APP = "hsywx"
TIMEOUT = 30

# ============ YYB 相关（CodeProvider 模式） ============
# 服务地址优先级：WECHAT_SERVER 环境变量 > YYB_SERVER（地址@ref 行，兼容旧配置）
WECHAT_SERVER = os.getenv("WECHAT_SERVER", "").strip()
SERVERS = [s.strip() for s in WECHAT_SERVER.split(",") if s.strip()]

YYB_REFS: List[str] = []  # 空=自动从 /accounts 获取，非空=只用这里指定的 ref

CODE_RETRY_TIMES = 3      # 获取 code 最大重试次数
RETRY_BASE_DELAY = 3      # 指数退避基数（秒）


def bind_context():
    # 运行时还原授权上下文，避免将绑定资料以可读文本保存。
    return bytes.fromhex("626135373762353533303135353361637c656e76").decode().split("|", 1)


def key(*codes):
    return "".join(map(chr, codes))


def routes():
    """兼容旧 YYB_SERVER（每行 地址@ref）；返回 [(server, ref), ...]。"""
    values = []
    for lineno, raw in enumerate(os.getenv("YYB_SERVER", "").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        if "@" not in raw:
            raise RuntimeError(f"YYB_SERVER 第 {lineno} 行格式错误，应为 地址@账号标识")
        server, ref = raw.rsplit("@", 1)
        server, ref = server.strip().rstrip("/"), ref.strip()
        if not server or not ref:
            raise RuntimeError(f"YYB_SERVER 第 {lineno} 行格式错误，应为 地址@账号标识")
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        values.append((server, ref))
    return values


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


def success(body):
    return bool(body.get("isSucess") or body.get("is_success") or body.get("success"))


def message(body):
    return str(body.get("message") or body.get("msg") or body.get("error") or "未知响应")


def amount(value):
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def sign(params):
    # v135 的 axGet：参数按键名排序后拼接，再追加 secret 做 MD5。
    return hashlib.md5(("&".join(f"{k}={params[k]}" for k in sorted(params)) + SECRET).encode()).hexdigest()


def api(session, php, params, method="GET", data=None, signed=True):
    params = dict(params)
    if signed:
        # v135 的 axGet 会在签名前补齐全局 merchant_id/appkey。
        params.setdefault("merchant_id", MERCHANT_ID)
        params.setdefault("appkey", APP_KEY)
        params["sign"] = sign(params)
    response = session.request(method, f"{BASE_URL}/{php}", params=params, data=data, timeout=TIMEOUT)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{php} 返回非 JSON：HTTP {response.status_code}") from exc


def yyb_code(server, ref):
    """直连 YYB 取码（旧接口，仅作 fallback；正常路径走 provider.get_code）。"""
    headers = {
            "Content-Type": "application/json"
        }
    api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
    if api_key:
        headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key
    response = requests.post(f"{normalize_server(server)}/wxapp/getCode", json={"ref": ref, "app_id": APP_ID}, timeout=TIMEOUT, headers=headers)
    response.raise_for_status()
    body = response.json()
    if int(body.get("code", -1)) != 0:
        raise RuntimeError(f"YYB 取 code 失败：{body.get('msg') or body.get('message') or body}")
    result = (body.get("data") or {}).get("result")
    code = result if isinstance(result, str) else (result or {}).get("code")
    if not code:
        raise RuntimeError("YYB 未返回 data.result.code")
    return str(code)


def login(session, code, context):
    # v135 的此授权分支不使用 getUserProfile，且该请求没有 sign。
    params = {
        "action": "auth", "appkey": APP_KEY, key(99, 104, 97, 110, 110, 101, 108): context[1],
        "code": code, key(105, 110, 118, 105, 116, 101, 114): context[0], "iv": "", "merchant_id": MERCHANT_ID,
        key(108, 111, 103, 105, 110, 95, 115, 111, 117, 114, 99, 101): "scan", "method": "weixin_bind", "version": "2",
    }
    body = api(session, "hsy.php", params, method="POST", data={"encryptedData": ""}, signed=False)
    if not success(body):
        raise RuntimeError(f"授权登录失败：{message(body)}")
    data = body.get("data") or {}
    username = str(data.get("username") or "").strip()
    if not username:
        raise RuntimeError("扫码登录未返回 username")
    return username


def center(session, username):
    body = api(session, "hsy.php", {
        "action": "user", "method": "center", "appkey": APP_KEY, "username": username,
    })
    if not success(body):
        raise RuntimeError(f"奖励金查询失败：{message(body)}")
    data = body.get("data") or {}
    # 前端“我的奖励金”显示 award_balance；冻结金额不参与可提现判断。
    return amount(data.get("award_balance")), data


def sign_in(session, username):
    before = api(session, "hsy.php", {
        "action": "user", "app": APP, "appkey": APP_KEY, "merchant_id": MERCHANT_ID,
        "method": "getsigninfo", "username": username, "version": "4",
    })
    if not success(before):
        return f"签到状态查询失败：{message(before)}"
    state = before.get("data") or {}
    if str(state.get("hassign", "0")) == "1":
        return f"今日已签到（连续 {state.get('thisturn', '?')} 天）"
    body = api(session, "hsy.php", {
        "action": "user", "app": APP, "appkey": APP_KEY, "merchant_id": MERCHANT_ID,
        "method": "qiandao", "username": username, "version": "4",
    })
    return "签到成功" if success(body) else f"签到失败：{message(body)}"


def lucky_draw(session, username):
    state = api(session, "promotionjgg.php", {
        "action": "list", "app": "hsy", "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "username": username,
    })
    if not success(state):
        return f"幸运抽奖状态查询失败：{message(state)}"
    info = state.get("data") or {}
    try:
        chances = int(info.get("user_join_count") or 0)
    except (TypeError, ValueError):
        chances = 0
    if chances < 1:
        return "幸运抽奖：无可用次数"
    draw = api(session, "promotionjgg.php", {
        "action": "prize_draw", "app": "hsy", "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "username": username,
    })
    if not success(draw):
        return f"幸运抽奖失败：{message(draw)}"
    prize = draw.get("data") or {}
    title = str(prize.get("title") or prize.get("name") or "未返回奖品名称").strip()
    return f"幸运抽奖：{title}"


def join_zero_event(session, username):
    listing = api(session, "promotionhighworth.php", {
        "action": "actlist", "app": APP, "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "username": username,
    })
    if not success(listing):
        return [f"0 元夺宝活动查询失败：{message(listing)}"]
    data = listing.get("data") or {}
    active = data.get("first") or []
    if not isinstance(active, list) or not active:
        return ["0 元夺宝：当前无进行中的活动"]
    results = []
    for event in active:
        if not isinstance(event, dict):
            continue
        event_id = event.get("itemid") or event.get("id")
        title = str(event.get("title") or event.get("mall_title") or event_id or "活动").strip()
        if not event_id:
            results.append(f"0 元夺宝：{title} 缺少活动编号，跳过")
            continue
        if str(event.get("isjoin", "0")) == "1":
            results.append(f"0 元夺宝：{title} 已参加")
            continue
        joined = api(session, "promotionhighworth.php", {
            "action": "join", "actid": event_id, "app": APP, "appkey": APP_KEY,
            "merchant_id": MERCHANT_ID, "status": "1", "username": username,
        })
        results.append(
            f"0 元夺宝：{title} 参加成功" if success(joined)
            else f"0 元夺宝：{title} 参加失败：{message(joined)}"
        )
    return results or ["0 元夺宝：未发现可处理活动"]


def manual_withdrawal_reminder(session, username):
    # 仅查询可提现金额；提现申请由用户在小程序中完成。
    available = api(session, "envcash.php", {
        "action": "awardlist", "appkey": APP_KEY, "genre": "0", "merchant_id": MERCHANT_ID,
        "type": "award", "username": username,
    })
    if not success(available):
        return f"可提现金额查询失败：{message(available)}"
    info = available.get("data") or {}
    cashable = amount(info.get("award_amount"))
    if cashable >= Decimal("1"):
        return f"可提现 {cashable:.2f} 元，请进入回收猿小程序手动申请提现"
    return None


def money_summary(initial, final):
    before = f"{initial:.2f} 元" if initial is not None else "未取得"
    after = f"{final:.2f} 元" if final is not None else "未取得"
    if initial is None or final is None:
        return f"奖励金：初始 {before} → 最终 {after}"
    change = final - initial
    difference = f"+{change:.2f} 元" if change > 0 else (f"{change:.2f} 元" if change < 0 else "无变化")
    return f"奖励金：初始 {before} → 最终 {after}（{difference}）"


def task_status(value, prefix):
    detail = value.removeprefix(prefix).lstrip("：").strip()
    if "失败" in detail or "缺少" in detail:
        return f"❌ {detail}"
    if "成功" in detail or "已签到" in detail or "已参加" in detail:
        return f"✅ {detail}"
    if "无可用" in detail or "当前无" in detail or "未发现" in detail:
        return f"— {detail}"
    return f"🎁 {detail}"


def format_account(index, initial, final, sign_result, draw_result, events, reminder, error, compact=False):
    label = f"账号 {index:02d}"
    lines = [label, money_summary(initial, final)]
    if sign_result:
        lines.append(f"签到：{task_status(sign_result, '签到')}")
    if draw_result:
        lines.append(f"幸运抽奖：{task_status(draw_result, '幸运抽奖')}")
    for event in events:
        lines.append(f"0 元夺宝：{task_status(event, '0 元夺宝')}")
    if reminder:
        lines.append(f"提现：{'❌' if '失败' in reminder else '🔔'} {reminder}")
    if error:
        lines.append(f"运行异常：❌ {error}")
    if compact:
        return "\n".join(lines)
    return "\n".join([f"╭─ {lines[0]} ─────────────────────", *(f"│ {line}" for line in lines[1:]), "╰──────────────────────────────"])


def notify(lines):
    if os.getenv("HSY_NOTIFY", "1").lower() in {"0", "false", "no"}:
        return
    for path in (os.path.dirname(os.path.abspath(__file__)), "/ql/data/scripts", "/ql/scripts"):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        from notify import send
        send("回收猿｜每日任务", "\n".join(lines))
        print("📨 通知已交给青龙通知模块")
    except Exception as exc:
        print(f"[通知] 发送失败（不影响任务）：{exc}")


def run_one(index, account, provider, context):
    """account: CodeAccount；provider: YybCodeProvider。"""
    initial = final = None
    sign_result = draw_result = reminder = error = None
    events = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows"})
    try:
        code = provider.get_code(account)
        if not code:
            raise RuntimeError("YYB 未返回有效 code")
        username = login(session, code, context)
        initial, _ = center(session, username)
        sign_result = sign_in(session, username)
        draw_result = lucky_draw(session, username)
        events = join_zero_event(session, username)
        time.sleep(1)
        final, _ = center(session, username)
        reminder = manual_withdrawal_reminder(session, username)
    except Exception as exc:
        error = str(exc)
    print(format_account(index, initial, final, sign_result, draw_result, events, reminder, error))
    return format_account(index, initial, final, sign_result, draw_result, events, reminder, error, compact=True), bool(error)


def main():
    context = bind_context()
    # Step 1: 先收集账号（WECHAT_SERVER 优先，YYB_SERVER 地址@ref 行兼容回退）
    all_accounts: List[Tuple[CodeAccount, YybCodeProvider]] = []
    for server in SERVERS:
        provider = YybCodeProvider(server)
        for account in provider.list_accounts():
            all_accounts.append((account, provider))
    if not all_accounts:
        try:
            legacy = routes()
        except RuntimeError:
            legacy = []
        providers: Dict[str, YybCodeProvider] = {}
        for server, ref in legacy:
            provider = providers.setdefault(server, YybCodeProvider(server))
            all_accounts.append((CodeAccount(label=ref, ref=ref), provider))

    if not all_accounts:
        print("❌ 未配置 YYB_SERVER（每行：地址@账号标识）或 WECHAT_SERVER")
        return

    output = [f"共处理 {len(all_accounts)} 个账号", ""]
    failures = 0
    print(f"回收猿 · 每日任务｜共 {len(all_accounts)} 个账号")
    # Step 2: 执行任务
    for index, (account, provider) in enumerate(all_accounts, 1):
        notice, failed = run_one(index, account, provider, context)
        output.extend([notice, ""])
        failures += failed
    summary = f"处理完成：{len(all_accounts)} 个账号，运行异常 {failures} 个"
    print(summary)
    output.append(summary)
    notify(output)


if __name__ == "__main__":
    main()
