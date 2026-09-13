"""测试共用的确认执行入口。"""


def confirm(service, task_id, operation_id, version):
    """将接受确认与执行串起来；生产由 HTTP 接受后交给后台执行。"""
    service.accept_confirmation(task_id, operation_id, version)
    service.execute_accepted(operation_id)
    return service.get_execution(operation_id)
