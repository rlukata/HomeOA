import os

from django.core.management.base import BaseCommand, CommandError

from homeoa import audit
from homeoa.core.models import Association

from ... import modules, registry

ALLOWED_SETTINGS = {"homeoa.settings.dev", "homeoa.settings.test", "homeoa.settings.e2e"}


class Command(BaseCommand):
    help = (
        "Development and tests only: turn an optional module on or off, or list the "
        "modules and whether each is on."
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["on", "off", "list"])
        parser.add_argument("module_id", nargs="?")
        target = parser.add_mutually_exclusive_group()
        target.add_argument("--association", help="Association id or short name.")
        target.add_argument("--all", action="store_true", help="Every association.")

    def handle(self, *args, action, module_id, association, all, **options):
        if os.environ.get("DJANGO_SETTINGS_MODULE") not in ALLOWED_SETTINGS:
            raise CommandError("Refusing: only for development, test and E2E settings.")
        associations = self._associations(association, all)
        if action == "list":
            for assoc in associations:
                for manifest in registry.all_modules():
                    state = "on" if modules.is_on(assoc, manifest.id) else "off"
                    self.stdout.write(f"{assoc.pk}\t{manifest.id}\t{state}")
            return
        if not module_id:
            raise CommandError("Name the module to turn on or off.")
        with audit.audit_context(label="command:module"):
            for assoc in associations:
                try:
                    modules.set_module(assoc, module_id, action == "on", user=None)
                except modules.UnknownModule as exc:
                    raise CommandError(str(exc)) from exc
                except modules.ModuleChangeRefused as exc:
                    lines = "\n".join(f"- {r}" for r in exc.reasons)
                    raise CommandError(f"Refused for association {assoc.pk}:\n{lines}") from exc
        self.stdout.write(f"{module_id} {action}")

    def _associations(self, ref, every):
        if every:
            return list(Association.objects.order_by("pk"))
        if ref:
            lookup = {"pk": int(ref)} if ref.isdigit() else {"short_name": ref}
            found = Association.objects.filter(**lookup).first()
            if found is None:
                raise CommandError(f"No association {ref!r}.")
            return [found]
        found = list(Association.objects.order_by("pk")[:2])
        if len(found) != 1:
            raise CommandError("Name an association with --association, or use --all.")
        return found
