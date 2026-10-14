"""Domain errors raised by services. The API layer maps them to HTTP responses and
remote transports serialize them by ``code``."""


class ServiceError(Exception):
    code = "service_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFoundError(ServiceError):
    """The resource does not exist *or* the caller may not see it (never reveal which)."""

    code = "not_found"


class PermissionDeniedError(ServiceError):
    """The caller can see the resource but may not modify it."""

    code = "permission_denied"


class ConflictError(ServiceError):
    code = "conflict"


class InvalidRequestError(ServiceError):
    code = "invalid_request"


class AuthenticationError(ServiceError):
    code = "unauthenticated"


ERRORS_BY_CODE = {
    cls.code: cls
    for cls in (ServiceError, NotFoundError, PermissionDeniedError, ConflictError, InvalidRequestError, AuthenticationError)
}
