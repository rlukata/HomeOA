from django.apps import AppConfig


class ScopedConfig(AppConfig):
    name = "tests.scoped"
    label = "scoped"
    default_auto_field = "django.db.models.BigAutoField"
