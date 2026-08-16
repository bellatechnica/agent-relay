"""Relay errors that transports translate into their own error formats."""


class RelayError(Exception):
    """Base class for expected relay failures."""

    status_code = 400
    code = "relay_error"


class AuthenticationError(RelayError):
    status_code = 401
    code = "authentication_failed"


class AuthorizationError(RelayError):
    status_code = 403
    code = "authorization_failed"


class NotFoundError(RelayError):
    status_code = 404
    code = "not_found"


class ConflictError(RelayError):
    status_code = 409
    code = "conflict"


class ValidationError(RelayError):
    status_code = 422
    code = "validation_failed"


class ConfigurationError(RelayError):
    status_code = 500
    code = "configuration_error"
