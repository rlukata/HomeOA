"""Default Importer: the generic CSV column mapper (built in ``homeoa.imports``)."""

from homeoa.imports.econdo import MappedCsvImporter


class CsvMapper(MappedCsvImporter):
    """Units, owners, tenants, opening balances, history, chart of accounts, trial balance."""
