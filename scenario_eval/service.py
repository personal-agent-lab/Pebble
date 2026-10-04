"""仅评测进程装配：真实 SDK 和生产应用，外部依赖显式替换为有状态客户端。"""

import json
import os
from pathlib import Path

from scenario_eval.world import Calendar, Clock, Mailbox


def create_app():
    import server.main as main
    from server.config import get_settings
    from server.tools.gmail.sync import GmailSource

    root = Path(os.environ["PEBBLE_EVAL_RUN_DIR"])
    world = json.loads((root / "scenario" / "world.json").read_text())
    clock = Clock(root / "clock.json")
    mail = Mailbox(root, world["account"], clock, world.get("faults"))
    calendar = Calendar(root, world.get("events", []), clock, world.get("faults"))
    # 生产装配已在外部服务工厂处分离依赖；只在本评测进程替换工厂。
    main._optional_gmail = lambda settings: ((mail, mail, mail), None)
    main._optional_calendar = lambda settings: (calendar, None)
    from server.agent import client as agent_client

    original_gateway = agent_client.QoderGateway

    class ClockGateway(original_gateway):
        def _query_input(self, turn):
            # SDK子进程的系统日期无法推进，显式提供同一受控环境时间。
            return f"当前时间：{clock.now().isoformat()}。\n\n" + super()._query_input(turn)

    agent_client.QoderGateway = ClockGateway
    app = main.create_production_app()
    clock.install()
    original = app.router.lifespan_context

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            # 首次启动只监听新邮件，等真实轮询游标建立后才投递种子来信。
            import asyncio

            cursor = get_settings().data_dir / "gmail_sync.json"
            for _ in range(100):
                if cursor.exists():
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("虚拟邮箱游标初始化失败")
            for incoming in world.get("incoming", []):
                mail.deliver(incoming)
            yield

    app.router.lifespan_context = lifespan
    if isinstance(app.state.mail_source, GmailSource):
        app.state.mail_source.interval = 1
    return app
