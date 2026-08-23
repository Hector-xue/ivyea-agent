"""飞书长连接接收端 —— 卡片按钮回调与飞书对话的入口。

**为什么在 agent 包里**：出站（发卡片）在 agent 本体，入站却曾经是一个独立目录，
不随任何 release 发出去。后果是拿到开源包的人：卡片收得到、按钮点了没反应、
飞书里也没法对话 —— R2 与 R6 两条需求对他们等于不存在，而界面上按钮还看得见、
点得动。一个别人用不了的功能，开源出去没有意义。

职责刻意很窄：**接住事件、过三道闸、转发给 agent、把结果卡片同步回填**。
所有业务判断（审批状态机、写开关、幅度硬闸、真实写入、回滚）都在 agent 侧，
它不做任何决定，也拿不到领星凭据。

走长连接，因此**这台机器不需要向公网开放任何端口**。

运行：``ivyea relay run``（前台）或 ``ivyea relay install``（写 systemd 单元）。
依赖飞书官方 SDK，随可选依赖装：``pip install "ivyea-agent[feishu]"``。
"""
from __future__ import annotations

#: 缺 SDK 时给出的**可执行**提示。只说"缺依赖"等于让人自己去猜包名。
SDK_HINT = ('缺少飞书官方 SDK。安装：pip install "ivyea-agent[feishu]"')


def sdk_available() -> bool:
    """SDK 在不在。**不 import 整个 relay**——那会在缺依赖时直接抛，
    而调用方（doctor / 配置向导）要的只是一个能显示的状态。"""
    import importlib.util

    return importlib.util.find_spec("lark_oapi") is not None


#: systemd 单元模板。占位符在 ``install_service`` 里填。
#: KillMode/OOMPolicy 是 ttyd/tmux 那次连坐的教训：别让本服务的 OOM 牵连同 cgroup
#: 的其它进程。
SERVICE_TEMPLATE = """[Unit]
Description=Ivyea Feishu Relay (card callbacks + chat -> ivyea-agent)
After=network-online.target ivyea-agent.service
Wants=ivyea-agent.service

[Service]
Type=simple
User={user}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONUTF8=1
ExecStart={python} -m ivyea_agent.feishu_relay
Restart=always
RestartSec=5
KillMode=process
OOMPolicy=continue

[Install]
WantedBy=multi-user.target
"""

SERVICE_NAME = "ivyea-feishu-relay.service"


def render_service(python: str = "", user: str = "root") -> str:
    import sys

    return SERVICE_TEMPLATE.format(python=python or sys.executable, user=user)
