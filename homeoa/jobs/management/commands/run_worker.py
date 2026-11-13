from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the background job worker (Procrastinate), including the scheduler tick."

    def add_arguments(self, parser):
        parser.add_argument("--concurrency", type=int, default=1)
        parser.add_argument(
            "--queues", default="", help="Comma-separated queues to listen to (default: all)."
        )

    def handle(self, *args, **options):
        argv = ["worker", "--concurrency", str(options["concurrency"])]
        if options["queues"]:
            argv += ["--queues", options["queues"]]
        call_command("procrastinate", *argv)
