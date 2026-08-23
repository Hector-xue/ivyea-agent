"""feishu-ivyea-relay 配置 —— 凭据不落代码。

两个来源，**agent 的 ``~/.ivyea`` 优先，环境变量兜底**：

- ``~/.ivyea/settings.json`` + ``~/.ivyea/.env``：IvyeaOps 系统配置页写的那一份。
  同一个飞书应用同时给 agent 发卡片、给 relay 收回调，配置理应只有一处。
- 环境变量（systemd 的 EnvironmentFile，0600）：没有 agent 配置时的兜底，
  以及独立部署 / 本机调试时的手动路径。

**为什么是 agent 优先而不是 env 优先**（与一般"env 覆盖文件"的习惯相反）：
env 优先会让界面变成假开关——用户在 IvyeaOps 上把某个人从审批白名单里删掉，
保存成功、界面上也没了，relay 却还认着 env 里的旧名单继续放行。
撤权失败比配错更危险，所以让"能在界面上改的那一份"说了算；
env 只在界面从没配过（键不存在）时生效。
"""
import json
import os

def _ivyea_dir() -> str:
    """与 agent 的 ``config.IVYEA_DIR`` 同一套解析（含 ``IVYEA_HOME`` 覆盖）。

    每次现算而不是模块级常量：测试要用 ``IVYEA_HOME`` 指向临时目录，
    常量会把真实的 ``~/.ivyea`` 焊死进来——那意味着单测读的是这台机器上
    真实的审批白名单，既不可复现，也随时会因为你在界面上改了配置而变红。
    """
    return os.environ.get("IVYEA_HOME") or os.path.join(os.path.expanduser("~"), ".ivyea")


def _agent_settings() -> dict:
    """每次都重读：白名单要能改完立刻生效，不能要求重启 relay。

    读失败一律当"没配"，绝不抛——relay 崩掉等于飞书那头彻底没人接。
    """
    try:
        with open(os.path.join(_ivyea_dir(), "settings.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _agent_env(key: str) -> str:
    try:
        with open(os.path.join(_ivyea_dir(), ".env"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _as_ids(raw) -> set:
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").replace(" ", ",").split(",")
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(x).strip() for x in raw if str(x).strip()}


def _ids(name: str) -> set:
    return {x.strip() for x in os.environ.get(name, "").split(",") if x.strip()}


def allowed_sender_ids() -> set:
    """闸 1 的名单（动态）。agent 配置里有这个键就以它为准，哪怕是空的。

    空列表是**有意义的配置**（"谁都不许点"），不是"没配"，所以判定用键在不在，
    不用值真不真。
    """
    settings = _agent_settings()
    if "feishu_allowed_senders" in settings:
        return _as_ids(settings.get("feishu_allowed_senders"))
    return _ids("ALLOWED_SENDER_IDS")


def allowed_chat_ids() -> set:
    settings = _agent_settings()
    if "feishu_allowed_chats" in settings:
        return _as_ids(settings.get("feishu_allowed_chats"))
    return _ids("ALLOWED_CHAT_IDS")


#: 凭据同样是 agent 优先。长连接要在启动时就建立，这三个值取一次即可
#: ——换应用本来就得重启 relay（长连接是按 app_id 建的）。
FEISHU_APP_ID = (_agent_env("IVYEA_FEISHU_APP_ID")
                 or str(_agent_settings().get("feishu_app_id") or "")
                 or os.environ.get("FEISHU_APP_ID", ""))
FEISHU_APP_SECRET = (_agent_env("IVYEA_FEISHU_APP_SECRET")
                     or os.environ.get("FEISHU_APP_SECRET", ""))
FEISHU_DOMAIN = (str(_agent_settings().get("feishu_domain") or "")
                 or os.environ.get("FEISHU_DOMAIN", "feishu"))   # feishu | lark

#: 事件订阅若配了加密/校验，填这两个；长连接模式通常留空。
ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")
VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")

#: 闸 1：允许点按钮的人。**留空 = 不允许任何人**——这是安全默认。
#: 空集合当"放行所有"是典型的危险默认：配错一次就等于把改钱的权限开给整个群。
#: 这两个常量只是**启动时的快照**（日志和 missing() 用）；判定一律走上面两个
#: 函数，否则界面上改完白名单要重启 relay 才生效。
ALLOWED_SENDER_IDS = allowed_sender_ids()

#: 允许的会话；留空表示不限制会话（会话一致性由 agent 侧按 approval 记录校验）。
ALLOWED_CHAT_IDS = allowed_chat_ids()

#: agent serve（只监听回环）
AGENT_URL = os.environ.get("IVYEA_AGENT_URL", "http://127.0.0.1:8765")
AGENT_TIMEOUT = float(os.environ.get("IVYEA_AGENT_TIMEOUT", "20"))

#: 闸 3：卡片回调去重窗口（秒）。飞书会重投，重投不能变成重复执行。
CARD_TOKEN_TTL = float(os.environ.get("CARD_TOKEN_TTL", str(15 * 60)))
CARD_TOKEN_MAX = int(os.environ.get("CARD_TOKEN_MAX", "2000"))

#: 会话映射与去重表落哪。**默认跟着 ~/.ivyea 走**：以前落在模块目录下的 .state，
#: 装进 site-packages 之后那里通常不可写（root 装、普通用户跑就直接崩）。
STATE_DIR = os.environ.get("RELAY_STATE_DIR", os.path.join(_ivyea_dir(), "relay-state"))
LOG_LEVEL = os.environ.get("RELAY_LOG_LEVEL", "INFO")


def missing() -> list:
    """**启动的硬门槛只有凭据。**

    白名单为空以前也算"配置缺失"、直接拒绝启动。现在白名单能在 IvyeaOps 界面上
    随时改（不重启即生效），再拿它当启动条件就成了死结：新用户填完凭据想先起
    relay 试试对话，却因为还没指定审批人而起不来。
    安全性没有下降——真正的把关在 ``gates.sender_allowed``，那里空名单依旧
    拒绝所有人；这里只是把它降级为一条醒目的启动警告。
    """
    out = []
    if not FEISHU_APP_ID:
        out.append("FEISHU_APP_ID")
    if not FEISHU_APP_SECRET:
        out.append("FEISHU_APP_SECRET")
    return out


def warnings() -> list:
    if not ALLOWED_SENDER_IDS:
        return [("审批白名单为空：卡片会照发，但没有人点得动按钮"
                 "（在 IvyeaOps 系统配置 → 飞书里指定，或设 ALLOWED_SENDER_IDS）")]
    return []


# ── P5 对话入口 ─────────────────────────────────────────────────────────────
#: 是否只读。默认 True —— 飞书里随口一句话不该触发写操作；
#: 真正要改钱的动作走审批卡片那条路（有幅度闸/快照/回滚）。
CHAT_PLAN_MODE = os.environ.get("CHAT_PLAN_MODE", "1") not in ("0", "false", "False")
#: agent 一轮可能跑几十秒（多工具步），超时要给够
CHAT_TIMEOUT = float(os.environ.get("CHAT_TIMEOUT", "600"))
#: 触发前缀；留空表示单聊里任何消息都转发
CHAT_PREFIX = os.environ.get("CHAT_PREFIX", "")
SESSIONS_FILE = os.path.join(STATE_DIR, "sessions.json")
SEEN_FILE = os.path.join(STATE_DIR, "seen_messages.json")
SEEN_MAX = int(os.environ.get("SEEN_MAX", "500"))
