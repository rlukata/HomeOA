import os

from django.core.management.base import CommandError

from homeoa.core.models import Association
from homeoa.core.sandbox import SANDBOX_MODE_KEY  # set by seed_demo


def require_test_mode() -> None:
    """Test-only commands run only with HOMEOA_E2E=1 or an association in sandbox mode."""
    if os.environ.get("HOMEOA_E2E") == "1":
        return
    assoc = Association.objects.order_by("pk").first()
    if assoc and (assoc.settings or {}).get(SANDBOX_MODE_KEY):
        return
    raise CommandError("Refusing: test-only command. Set HOMEOA_E2E=1 or use sandbox mode.")
