"""Transport-level failures raised by the mock service.

Business outcomes (slot_taken, hold_expired, slot_conflict, ...) are returned as result
codes, like a real HTTP API returning a 4xx body. Transport failures (timeouts, 503s,
crashes) are raised, because the caller genuinely does not get a response.
"""


class ToolTransportError(Exception):
    code = "transport_error"


class ToolTimeout(ToolTransportError):
    code = "timeout"


class ServiceUnavailable(ToolTransportError):
    code = "service_unavailable_503"


class ToolInternalError(ToolTransportError):
    code = "internal_error"
