// name: 影视飓风
// cron: 40 6,18 * * *
const axios = require("axios");
const fs = require("fs");
const path = require("path");
// ====================== YYB Go 账号（环境变量 YYB_SERVER = 地址@微信账号标识，多行） ======================
const SERVERS = (process.env.YYB_SERVER || "")
    .split(/\r?\n/)
    .map(s => s.trim())
    .filter(Boolean);
if (!SERVERS.length) {
    console.error("未配置环境变量 YYB_SERVER，请设置后重试（格式：地址@微信账号标识，多行换行）");
    process.exit(1);
}
function parseYybGoEntry(rawValue) {
    const value = String(rawValue || "").trim();
    if (!value) return { server: "", ref: "" };
    const atIndex = value.indexOf("@");
    if (atIndex === -1) {
        return { server: value, ref: "" };  // 纯服务地址：ref 留空，由 /accounts 自动发现
    }
    let server = value.slice(0, atIndex).trim();
    const ref = value.slice(atIndex + 1).trim();
    if (server.startsWith("http://")) server = server.slice(7);
    else if (server.startsWith("https://")) server = server.slice(8);
    server = server.replace(/\/+$/, "");
    if (!server || !ref) return { server, ref: "" };
    return { server, ref };
}

function yybAuthHeaders() {
    const headers = { "Content-Type": "application/json" };
    const token = String(process.env.YYB_API_KEY || "").trim() || String(process.env.YYB_PROTOCOL_TOKEN || "").trim();
    if (token) headers.Authorization = token.includes(" ") ? token : `Bearer ${token}`;
    return headers;
}

// 从 YYB 服务 GET /accounts 自动发现账号 ref（支持多层嵌套/旧 dict 结构）
function extractYybRefs(data) {
    for (let depth = 0; depth < 5 && data && !Array.isArray(data); depth++) {
        if (typeof data === "string") {
            try { data = JSON.parse(data); } catch { return []; }
            continue;
        }
        const next = ["data", "list", "rows", "result", "accounts"]
            .map(k => (data || {})[k]).find(v => Array.isArray(v) && v.length);
        if (next) { data = next; continue; }
        const known = ["data", "list", "rows", "result", "accounts"]
            .find(k => (data || {})[k] !== undefined);
        if (known) { data = data[known]; continue; }
        if (typeof data === "object" && data) {
            // 旧 dict 形态 {openid: {...}}：键名即 ref，注入 item 再展开
            const entries = Object.entries(data);
            if (entries.length && entries.every(([, v]) => v && typeof v === "object" && !Array.isArray(v))) {
                data = entries.map(([k, v]) => ({ ref: k, ...v }));
            } else {
                data = entries.map(([, v]) => v);
            }
            continue;
        }
        return [];
    }
    if (!Array.isArray(data)) return [];
    const refs = [];
    const seen = new Set();
    for (const item of data) {
        let ref = "";
        if (typeof item === "string") ref = item.trim();
        else if (item && typeof item === "object") {
            for (const key of ["ref", "openid", "openId", "wxid", "wx_id", "id", "account"]) {
                if (item[key] !== undefined && item[key] !== null && String(item[key]).trim()) {
                    ref = String(item[key]).trim();
                    break;
                }
            }
        }
        if (ref && !seen.has(ref)) { seen.add(ref); refs.push(ref); }
    }
    return refs;
}

// YYB_SERVER 中只填地址（无 @ref）的行：自动从 /accounts 发现账号，展开成 地址@ref
async function expandBareYybServers() {
    const expanded = [];
    for (const line of SERVERS) {
        const parsed = parseYybGoEntry(line);
        if (parsed.server && parsed.ref) { expanded.push(line); continue; }
        if (!parsed.server) { console.log("YYB_SERVER 格式应为 地址@微信账号标识，当前值: " + line); continue; }
        const url = "http://" + parsed.server.replace(/^https?:\/\//, "") + "/accounts";
        try {
            const { data } = await axios.get(url, { timeout: 15000, proxy: false, headers: yybAuthHeaders() });
            const refs = extractYybRefs(data);
            if (!refs.length) {
                console.log(`⚠️ ${parsed.server} /accounts 未解析到账号（请确认 YYB 服务在线且已授权）`);
                continue;
            }
            console.log(`✅ ${parsed.server} 自动发现 ${refs.length} 个账号`);
            for (const ref of refs) expanded.push(`${parsed.server}@${ref}`);
        } catch (e) {
            console.log(`⚠️ 读取 ${parsed.server}/accounts 失败: ${e.message || e}`);
        }
    }
    return expanded;
}
async function getCode(server) {
    const { server: parsedServer, ref } = parseYybGoEntry(server);
    if (!parsedServer || !ref) return null;
    const url = "http://" + parsedServer + "/wxapp/getCode";
    try {
        const { data } = await axios.post(url, { ref, app_id: MINI_APP_ID }, { timeout: 20000, proxy: false, headers: yybAuthHeaders() });
        const code = data && data.data && data.data.result && data.data.result.code;
        if (!data || data.code !== 0 || !code) {
            console.log(parsedServer + " 获取code失败: " + JSON.stringify(data));
            return null;
        }
        console.log(parsedServer + " 获取code成功");
        return code;
    } catch (e) {
        console.log(parsedServer + " 获取code异常: " + e.message);
        return null;
    }
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
let userIdx = 1;

const MINI_APP_ID = "wx92782ef90ebc836d";
const CLIENT_ID = "4d65249d377b2c3ed8";
const CLIENT_SECRET = "1cdc05151d64f3a4a6ebd0e9de64422a";
const GRANT_TYPE = "yz_union";
const CLIENT_BIZ = "weapp_wsc";
const KDT_ID = "149536603";
const USER_VERSION = "2.226.7.101";
const PAGE_VERSION = "17";
const API_BASE = "https://h5.youzan.com";
const TOKEN_CACHE_FILE = path.join(__dirname, "token_caches", "yingshijufeng_token_cache.json");
try { fs.mkdirSync(path.dirname(TOKEN_CACHE_FILE), { recursive: true }); } catch (e) {}
const USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) MicroMessenger/3.9.12 MiniProgramEnv/Windows WindowsWechat/WMPF";

function readTokenCache() {
    try {
        if (!fs.existsSync(TOKEN_CACHE_FILE)) return {};
        return JSON.parse(fs.readFileSync(TOKEN_CACHE_FILE, "utf8")) || {};
    } catch (e) {
        return {};
    }
}

function writeTokenCache(cache) {
    try {
        fs.writeFileSync(TOKEN_CACHE_FILE, JSON.stringify(cache, null, 2), "utf8");
    } catch (e) {
        console.log(`写入token缓存失败: ${e.message || e}`);
    }
}

function maskPhone(phone = "") {
    return String(phone).replace(/^(\d{3})\d{4}(\d{4})$/, "$1****$2");
}

function pickToken(data = {}) {
    return data.accessToken || data.access_token || "";
}

function isTokenError(message) {
    return /access_token|token|登录|授权|invalid session|session/i.test(String(message || ""));
}

class Task {
    constructor(openid) {
        this.server = openid;
        const _yyb = parseYybGoEntry(this.server);
        this.ref = _yyb.ref;
        this.openid = _yyb.ref;
        this.index = userIdx++;
        this.openid = String(openid || "").trim();
        this.token = "";
        this.sessionId = "";
        this.cookie = "";
        this.kdtId = KDT_ID;
        this.userInfo = {};
    }

    async run() {
        const cached = this.getCachedToken();
        if (cached) {
            this.applyToken(cached);
            console.log(`账号[${this.index}] 使用缓存token`);
            if (!(await this.checkToken())) {
                this.removeCachedToken();
                console.log(`账号[${this.index}] 缓存token失效，重新登录`);
            }
        }

        if (!this.token) {
            await this.loginByWxCode();
            if (!this.token) return;
        }

        await this.showCheckinPage();
        await this.doCheckin();
        await this.getPoints();
    }

    getCachedToken() {
        const cache = readTokenCache();
        return cache[this.openid] || null;
    }

    saveCachedToken() {
        if (!this.token) return;
        const cache = readTokenCache();
        cache[this.openid] = {
            accessToken: this.token,
            sessionId: this.sessionId,
            kdtId: this.kdtId,
            cookie: this.cookie,
            mobile: this.userInfo.mobile || "",
            nickName: this.userInfo.nick_name || this.userInfo.nickName || "",
            updatedAt: new Date().toISOString(),
        };
        writeTokenCache(cache);
    }

    removeCachedToken() {
        const cache = readTokenCache();
        if (cache[this.openid]) {
            delete cache[this.openid];
            writeTokenCache(cache);
        }
        this.token = "";
        this.sessionId = "";
        this.cookie = "";
    }

    applyToken(data = {}) {
        this.token = pickToken(data);
        this.sessionId = data.sessionId || data.session_id || "";
        this.kdtId = String(data.kdtId || data.kdt_id || KDT_ID);
        this.cookie = data.cookie || "";
    }

    getHeaders(extra = {}) {
        const headers = {
            "User-Agent": USER_AGENT,
            "Referer": `https://servicewechat.com/${MINI_APP_ID}/${PAGE_VERSION}/page-frame.html`,
            "Accept": "*/*",
            "Extra-Data": JSON.stringify({
                sid: this.sessionId || "",
                version: USER_VERSION,
                clientType: "weapp-miniprogram",
                client: "weapp",
                bizEnv: "wsc",
            }),
            ...extra,
        };
        if (this.cookie) headers.Cookie = this.cookie;
        return headers;
    }

    getBaseParams(params = {}) {
        return {
            app_id: MINI_APP_ID,
            kdt_id: this.kdtId,
            access_token: this.token,
            ...params,
        };
    }

    async request({ method = "GET", path: apiPath, params = {}, data = {}, skipToken = false }) {
        const options = {
            method,
            url: `${API_BASE}${apiPath.startsWith("/") ? apiPath : `/${apiPath}`}`,
            headers: this.getHeaders(method === "POST" ? { "Content-Type": "application/json" } : {}),
            timeout: 15000,
            validateStatus: () => true,
        };
        options.params = skipToken ? params : this.getBaseParams(params);
        if (method !== "GET") options.data = data;

        const { data: result, status, headers } = await axios.request(options);
        if (headers["set-cookie"]) {
            this.cookie = headers["set-cookie"].map((item) => item.split(";")[0]).join("; ");
        }
        if (status !== 200) throw new Error(`HTTP ${status}: ${JSON.stringify(result)}`);
        if (!result || result.code !== 0) throw new Error(result?.msg || JSON.stringify(result));
        return result.data;
    }

    async getLoginCode() {
        return await getCode(this.server);
    }

    async authorize(code, data) {
        return this.request({
            method: "POST",
            path: "/wscshop/weapp/authorize.json",
            skipToken: true,
            data: { ...data, code },
        });
    }

    async loginByWxCode() {
        try {
            const code = await this.getLoginCode();
            let data;
            try {
                data = await this.authorize(code, {
                    appId: MINI_APP_ID,
                    clientId: CLIENT_ID,
                    clientSecret: CLIENT_SECRET,
                    grantType: GRANT_TYPE,
                });
            } catch (e) {
                data = await this.authorize(code, {
                    appId: MINI_APP_ID,
                    clientBiz: CLIENT_BIZ,
                });
            }
            this.applyToken(data);
            this.userInfo = data || {};
            this.saveCachedToken();
            console.log(`账号[${this.index}] 登录成功: ${data.nick_name || data.nickName || ""} ${maskPhone(data.mobile) || ""}`);
        } catch (e) {
            console.log(`账号[${this.index}] 登录失败: ${e.message || e}`);
        }
    }

    async checkToken() {
        try {
            await this.request({ path: "/wscump/checkin/show_checkin_page_v2.json" });
            return true;
        } catch (e) {
            return false;
        }
    }

    async showCheckinPage() {
        try {
            const data = await this.request({ path: "/wscump/checkin/show_checkin_page_v2.json" });
            this.checkinId = data?.checkinId;
            this.isShow = !!data?.isShow;
            console.log(`账号[${this.index}] 签到活动: checkinId=${this.checkinId || "未获取"} isShow=${this.isShow}`);
        } catch (e) {
            console.log(`账号[${this.index}] 获取签到活动失败: ${e.message || e}`);
            if (isTokenError(e.message || e)) this.removeCachedToken();
        }
    }

    async doCheckin() {
        if (!this.checkinId) {
            console.log(`账号[${this.index}] 未获取到 checkinId，跳过签到`);
            return;
        }
        try {
            const data = await this.request({
                path: "/wscump/checkin/checkinV2.json",
                params: { checkinId: this.checkinId },
            });
            const awards = (data?.list || []).map((item) => item?.infos?.title).filter(Boolean).join(", ");
            console.log(`账号[${this.index}] 签到成功: ${data?.desc || ""}${awards ? ` ${awards}` : ""}`);
        } catch (e) {
            const message = String(e.message || e);
            if (/已达最大参与次数|已签到|重复签到/.test(message)) {
                console.log(`账号[${this.index}] 今日已签到`);
                return;
            }
            console.log(`账号[${this.index}] 签到失败: ${message}`);
            if (isTokenError(message)) this.removeCachedToken();
        }
    }

    async getPoints() {
        try {
            const data = await this.request({ path: "/wscump/integral/user_points.json" });
            console.log(`账号[${this.index}] 当前积分: ${data?.current_points ?? data?.real_points ?? "未知"}`);
        } catch (e) {
            console.log(`账号[${this.index}] 查询积分失败: ${e.message || e}`);
        }
    }
}

!(async () => {
    const accounts = await expandBareYybServers();
    if (!accounts.length) {
        console.error("YYB_SERVER 中没有可用账号（支持 地址@微信账号标识 每行一条，或只填服务地址自动发现全部账号）");
        process.exit(1);
    }
    for (const openid of accounts) {
        await new Task(openid).run();
    }
})()
    .catch((e) => console.log(e.message || e))
