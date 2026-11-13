"""``manage.py grant_role <email> <role> [--title treasurer]``: assign a role (dev/E2E/ops).

Creates the user if missing. Uses the first association (single-HOA UI); with
``--create-association NAME`` one is created when none exists (fresh dev/E2E databases).
Idempotent: does nothing when the user already holds the role today.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from homeoa import audit
from homeoa.access.models import Role, RoleAssignment
from homeoa.access.services import assign_role, ensure_default_roles, today_for
from homeoa.core.models import Association


class Command(BaseCommand):
    help = "Assign a role to a user in the (first) association."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("role", help="role slug, e.g. board, committee, cpa")
        parser.add_argument("--title", default="")
        parser.add_argument("--create-association", metavar="NAME", default="")

    def handle(self, email, role, title, create_association, **options):
        association = Association.objects.order_by("pk").first()
        if association is None and create_association:
            initials = "".join(w[0] for w in create_association.split()[:3]).upper()
            association = Association.objects.create(
                name=create_association, short_name=initials or "HO"
            )
        if association is None:
            raise CommandError("No association exists yet.")
        ensure_default_roles(association)
        role_obj = Role.all_associations.filter(association=association, slug=role).first()
        if role_obj is None:
            raise CommandError(f"Unknown role {role!r}.")
        if not role_obj.assignable:
            raise CommandError(f"Role {role!r} is derived from registry data.")
        user = get_user_model().objects.filter(email__iexact=email.strip()).first()
        if user is None:
            user = get_user_model().objects.create_user(email.strip())
        day = today_for(association)
        active = (
            RoleAssignment.all_associations.filter(
                association=association, user=user, role=role_obj, start__lte=day
            )
            .exclude(end__lt=day)
            .exists()
        )
        if active:
            self.stdout.write(f"{user.email} already holds {role}.")
            return
        with audit.audit_context(label="command:grant_role"):
            assign_role(user, association, role_obj, title=title)
        self.stdout.write(f"Granted {role} to {user.email}.")
