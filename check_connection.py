# -*- coding: utf-8 -*-
"""连接测试：验证 API Key 有效且流式识别 2.0 资源已开通（不发送音频，不产生计费）。"""
import sys
import uuid

from websocket import create_connection

from voice_input import configure_api, load_config, needs_setup

cfg = load_config()
if needs_setup(cfg):
    cfg = configure_api(cfg)
if not cfg:
    print("未完成 API 配置，连接测试已取消。")
    sys.exit(1)

headers = {
    "X-Api-Key": cfg["api_key"],
    "X-Api-Resource-Id": cfg["resource_id"],
    "X-Api-Request-Id": str(uuid.uuid4()),
    "X-Api-Sequence": "-1",
}

print("正在连接 %s ..." % cfg["ws_url"])
try:
    ws = create_connection(
        cfg["ws_url"],
        header=["%s: %s" % (k, v) for k, v in headers.items()],
        timeout=15,
    )
    print("连接成功：API Key 有效，流式语音识别 2.0 资源已开通。")
    ws.close()
except Exception as e:
    print("连接失败，请检查 API Key、资源开通状态和网络：%s" % type(e).__name__)