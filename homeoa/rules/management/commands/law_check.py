"""Run the law check for maintainers and print the review list."""

import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from homeoa.core.context import association_context, association_today
from homeoa.core.models import Association
from homeoa.rules import citations, lawcheck, lawwatch


class Command(BaseCommand):
    help = (
        "Ask the active rule pack's change source which sections it relies on changed, "
        "and print the review list. Nothing is stored unless --save is given."
    )

    def add_arguments(self, parser):
        parser.add_argument("--association", type=int, help="Association id (default: all).")
        parser.add_argument(
            "--since", type=dt.date.fromisoformat, help="YYYY-MM-DD; default: checked-through."
        )
        parser.add_argument(
            "--fake",
            action="store_true",
            help="Use a fake source that reports one sample change; no network.",
        )
        parser.add_argument(
            "--save",
            action="store_true",
            help="Store the result and email the System admins, as the yearly job does.",
        )

    def handle(self, *args, **options):
        if not options["fake"] and not lawwatch.network_allowed():
            raise CommandError("Live law checks are turned off in these settings; use --fake.")
        associations = Association.objects.order_by("pk")
        if options["association"]:
            associations = associations.filter(pk=options["association"])
            if not associations.exists():
                raise CommandError(f"No association {options['association']}.")
        for association in associations:
            with association_context(association):
                self._one(association, options)

    def _one(self, association, options):
        as_of = association_today(association)
        source = None
        if options["fake"]:
            from homeoa import rules

            sections = list(citations.index(rules.active_pack(association)).sections)
            sample = {
                "bill": "SAMPLE 0000",
                "action": "amended",
                "session_law": "sample",
            }
            source = lawwatch.FakeChangeSource({sections[0]: [sample]} if sections else {})
        do = lawcheck.run if options["save"] else lawcheck.check
        result = do(association, as_of, source, options["since"])
        write = self.stdout.write
        write(f"{association.name}: pack {result.pack_id} {result.pack_version}".rstrip())
        write(f"  law checked through: {result.checked_through or 'never'}")
        write(f"  status: {result.get_status_display()}")
        if result.reason:
            write(f"  reason: {result.reason}")
        for finding in result.findings:
            write(f"  RCW {finding['section']}")
            for change in finding["changes"]:
                parts = [change["bill"], change["session_law"], change["action"]]
                parts.append(change["effective"] or "effective date not given")
                write("    " + " · ".join(p for p in parts if p))
            for place in finding["places"]:
                write(f"    cited by {place['kind']} {place['key']}")
        for item in result.not_checked:
            write(f"  not checked automatically: {item['citation']}")
        if not options["save"]:
            write("  (not stored; pass --save to store it)")
