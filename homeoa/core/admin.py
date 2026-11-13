from django.contrib import admin

from .models import Association


@admin.register(Association)
class AssociationAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name", "timezone", "fiscal_year_start_month")
