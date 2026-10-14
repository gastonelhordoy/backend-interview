"""Domain errors raised by services. The API layer maps them to HTTP responses by
``status_code``, and remote transports serialize them by ``code``."""


class ServiceError(Exception):
    code = "service_error"
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFoundError(ServiceError):
    """The resource does not exist *or* the caller may not see it (never reveal which)."""

    code = "not_found"
    status_code = 404


class PermissionDeniedError(ServiceError):
    """The caller can see the resource but may not modify it."""

    code = "permission_denied"
    status_code = 403


class ConflictError(ServiceError):
    code = "conflict"
    status_code = 409


class InvalidRequestError(ServiceError):
    code = "invalid_request"
    status_code = 422


class AuthenticationError(ServiceError):
    code = "unauthenticated"
    status_code = 401


class ServiceUnavailableError(ServiceError):
    """A remote service could not be reached or answered with something unexpected."""

    code = "unavailable"
    status_code = 503


ERRORS_BY_CODE = {
    cls.code: cls
    for cls in (ServiceError, NotFoundError, PermissionDeniedError, ConflictError, InvalidRequestError,
                AuthenticationError, ServiceUnavailableError)
}
