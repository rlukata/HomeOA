"""Periodic jobs owned by the rules app (autodiscovered by procrastinate)."""

from homeoa.jobs import periodic

from . import lawcheck


# September 1: Washington's new laws usually take effect in the summer before.
@periodic("rules.law_check", cron="0 6 1 9 *", catch_up=False)
def law_check(association, as_of):
    """Ask the active pack's change source which sections it relies on changed."""
    lawcheck.run(association, as_of)
