"""LinkedIn session ownership and bootstrap exceptions."""

class SessionOwnershipError(Exception):
    """Base exception for provider session ownership violations."""
    pass


class SessionOwnershipConflictError(SessionOwnershipError, ValueError):
    """Raised when attempting an unauthorized downgrade or silent overwrite of a managed session."""
    pass


class ServerBootstrapError(SessionOwnershipError):
    """Raised when server bootstrap fails, times out, or encounters a lease conflict."""
    pass
