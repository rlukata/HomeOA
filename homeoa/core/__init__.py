"""Core: association base, context and small cross-app helpers."""


def residents_enabled(association) -> bool:
    """Whether owners and tenants have access: the association is live and resident access
    is not paused. Off until go-live."""
    from .status import residents_open

    return residents_open(association)
