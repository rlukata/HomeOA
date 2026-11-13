from decimal import Decimal

from django.db import models


class MoneyField(models.DecimalField):
    """A currency amount: always ``Decimal``, 14 digits, 2 decimal places."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", 14)
        kwargs.setdefault("decimal_places", 2)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if kwargs.get("max_digits") == 14:
            kwargs.pop("max_digits")
        if kwargs.get("decimal_places") == 2:
            kwargs.pop("decimal_places")
        return name, path, args, kwargs

    def to_python(self, value):
        if isinstance(value, float):
            raise TypeError("MoneyField does not accept float; use Decimal.")
        return super().to_python(value)

    def get_prep_value(self, value):
        if isinstance(value, float):
            raise TypeError("MoneyField does not accept float; use Decimal.")
        return super().get_prep_value(value)


ZERO = Decimal("0.00")
