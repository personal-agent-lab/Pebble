"""测试共用的确认执行入口与长期记忆准备。"""

from server.memory.service import model_view


def confirm(service, task_id, operation_id, version):
    """将接受确认与执行串起来；生产由 HTTP 接受后交给后台执行。"""
    service.accept_confirmation(task_id, operation_id, version)
    service.execute_accepted(operation_id)
    return service.get_execution(operation_id)


def seed_memory(store, target, content):
    """整份写入一块记忆，作为测试起点。"""
    return store.write(target, content, expected_version=store.snapshot()[target]["version"])


def memory_anchor(store, line):
    """模型视图里某一行的锚点。"""
    snapshot = store.snapshot()
    view = model_view({target: snapshot[target]["content"] for target in snapshot})
    for section in view.values():
        for row in section["content"].split("\n"):
            if row.endswith("| " + line):
                return row.split("|")[0]
    raise AssertionError(line)
