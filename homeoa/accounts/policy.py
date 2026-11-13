"""Whether a user must use MFA. The board-role check defaults to ``homeoa.access``."""

from collections.abc import Callable

SETTING_KEY = "require_mfa_for_board"


def _holds_board_role(user, association) -> bool:
    from homeoa.access.services import is_board_member

    return is_board_member(user, association)


_board_role_check: Callable = _holds_board_role


def set_board_role_check(fn: Callable | None) -> None:
    """Override the check (tests); ``None`` restores the access-backed default."""
    global _board_role_check
    _board_role_check = fn or _holds_board_role


def require_mfa_for_board(user, association) -> bool:
    """True when the association requires MFA for board roles and ``user`` holds one."""
    if association is None or not (association.settings or {}).get(SETTING_KEY, False):
        return False
    return bool(_board_role_check(user, association))
