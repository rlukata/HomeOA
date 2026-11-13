"""Compliance obligations (GOV-10 engine).

Recurring legal obligations defined in the active rule pack (``RulePack.obligations``)
become periods: one calendar item per period with a due date, the paths the board may
take (e.g. audit or waiver vote), yes/no exemption questions with citations, the chosen
path, evidence (an upload or a link, e.g. to an owner vote), and missed-deadline to-dos.

The daily job ``compliance.open_periods`` opens periods and flags missed ones; services
live in ``homeoa.compliance.services``. The calendar feed and reminders build on them.
"""
