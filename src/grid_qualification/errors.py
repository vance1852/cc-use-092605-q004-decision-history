"""审批时间线使用的可观察错误。"""


class ServiceError(RuntimeError):
    code = "service_error"
    status = 400


class Conflict(ServiceError):
    code = "conflict"
    status = 409


class InvalidState(ServiceError):
    code = "invalid_state"
    status = 409
