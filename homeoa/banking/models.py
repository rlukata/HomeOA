"""Bank accounts, imported bank transactions, import batches and statement balances (FIN-07).

* :class:`BankAccount` ties a real account to a GL cash account and fund. The full number
  is encrypted (OPS-04); only ``last4`` is kept in clear for display.
* :class:`CsvColumnMapping` is the saved CSV layout per bank account, reused on every upload.
* :class:`BankTransaction` is an immutable source record. Only ``status`` (and the
  matching fields) may change; its source fields reject updates.
* :class:`ImportBatch` summarizes one upload or sync.
* :class:`StatementBalance` is a balance the bank reported (OFX LEDGERBAL, feed balances).
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.crypto import EncryptedTextField
from homeoa.core.fields import ZERO, MoneyField
from homeoa.core.models import AssociationScopedModel
from homeoa.ledger.models import Account, Fund
from homeoa.providers.defaults.fileupload import CsvMapping, Sign


class BankAccount(AssociationScopedModel):
    name = models.CharField(_("name"), max_length=100)
    institution = models.CharField(_("bank"), max_length=100, blank=True)
    account_number = EncryptedTextField(_("account number"), blank=True)
    last4 = models.CharField(_("last 4 digits"), max_length=4, blank=True)
    gl_account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="bank_accounts",
        verbose_name=_("GL cash account"),
    )
    fund = models.ForeignKey(
        Fund, on_delete=models.PROTECT, related_name="bank_accounts", verbose_name=_("fund")
    )
    active = models.BooleanField(_("active"), default=True)
    last_synced_at = models.DateTimeField(_("last synced"), null=True, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("bank account")
        verbose_name_plural = _("bank accounts")
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ··{self.last4}" if self.last4 else self.name

    def save(self, *args, **kwargs):
        digits = "".join(c for c in (self.account_number or "") if c.isdigit())
        if digits:
            self.last4 = digits[-4:]
        super().save(*args, **kwargs)

    @property
    def masked_number(self):
        return f"····{self.last4}" if self.last4 else ""


class SignConvention(models.TextChoices):
    POSITIVE_IN = Sign.POSITIVE_IN, _("One amount column, deposits positive")
    POSITIVE_OUT = Sign.POSITIVE_OUT, _("One amount column, withdrawals positive")
    SPLIT = Sign.SPLIT, _("Separate debit and credit columns")


class CsvColumnMapping(AssociationScopedModel):
    bank_account = models.OneToOneField(
        BankAccount, on_delete=models.CASCADE, related_name="csv_mapping"
    )
    date_column = models.CharField(_("date column"), max_length=100)
    description_column = models.CharField(_("description column"), max_length=100, blank=True)
    amount_column = models.CharField(_("amount column"), max_length=100, blank=True)
    debit_column = models.CharField(_("debit (money out) column"), max_length=100, blank=True)
    credit_column = models.CharField(_("credit (money in) column"), max_length=100, blank=True)
    payee_column = models.CharField(_("payee column"), max_length=100, blank=True)
    memo_column = models.CharField(_("memo column"), max_length=100, blank=True)
    reference_column = models.CharField(_("bank reference column"), max_length=100, blank=True)
    balance_column = models.CharField(_("running balance column"), max_length=100, blank=True)
    sign = models.CharField(
        _("sign convention"),
        max_length=16,
        choices=SignConvention.choices,
        default=SignConvention.POSITIVE_IN,
    )
    date_format = models.CharField(
        _("date format"),
        max_length=32,
        blank=True,
        help_text=_("Leave blank to detect. Example: %m/%d/%Y"),
    )
    skip_rows = models.PositiveSmallIntegerField(_("lines before the header"), default=0)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("CSV column mapping")
        verbose_name_plural = _("CSV column mappings")

    def __str__(self):
        return str(_("CSV mapping for %(account)s") % {"account": self.bank_account})

    def to_mapping(self) -> CsvMapping:
        return CsvMapping(
            date=self.date_column,
            description=self.description_column,
            amount=self.amount_column,
            debit=self.debit_column,
            credit=self.credit_column,
            payee=self.payee_column,
            memo=self.memo_column,
            reference=self.reference_column,
            balance=self.balance_column,
            sign=self.sign,
            date_format=self.date_format,
            skip_rows=self.skip_rows,
        )

    @classmethod
    def fields_from(cls, m: CsvMapping) -> dict:
        return {
            "date_column": m.date,
            "description_column": m.description,
            "amount_column": m.amount,
            "debit_column": m.debit,
            "credit_column": m.credit,
            "payee_column": m.payee,
            "memo_column": m.memo,
            "reference_column": m.reference,
            "balance_column": m.balance,
            "sign": m.sign,
            "date_format": m.date_format,
            "skip_rows": m.skip_rows,
        }


class ImportSource(models.TextChoices):
    OFX = "ofx", _("OFX/QFX file")
    CSV = "csv", _("CSV file")
    FEED = "feed", _("Bank feed")


class ImportBatch(AssociationScopedModel):
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="batches")
    source = models.CharField(_("source"), max_length=8, choices=ImportSource.choices)
    filename = models.CharField(_("file name"), max_length=255, blank=True)
    file_sha256 = models.CharField(max_length=64, blank=True)
    provider = models.CharField(_("provider"), max_length=100, blank=True)
    total_rows = models.PositiveIntegerField(_("rows read"), default=0)
    imported_count = models.PositiveIntegerField(_("imported"), default=0)
    duplicate_count = models.PositiveIntegerField(_("duplicates skipped"), default=0)
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    undone_at = models.DateTimeField(_("undone at"), null=True, blank=True)
    undone_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    undo_reason = models.CharField(_("undo reason"), max_length=255, blank=True)

    @property
    def is_undone(self):
        return self.undone_at is not None

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("bank import")
        verbose_name_plural = _("bank imports")
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.get_source_display()} {self.filename} ({self.imported_count})"


class TxnStatus(models.TextChoices):
    UNMATCHED = "unmatched", _("Unmatched")
    MATCHED = "matched", _("Matched")
    CATEGORIZED = "categorized", _("Categorized")
    IGNORED = "ignored", _("Ignored")


class TransactionImmutableError(Exception):
    pass


class BankTransaction(AssociationScopedModel):
    SOURCE_FIELDS = (
        "bank_account_id",
        "date",
        "amount",
        "description",
        "payee",
        "memo",
        "external_id",
        "running_balance",
        "batch_id",
        "sequence",
    )

    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name="transactions"
    )
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="transactions")
    date = models.DateField(_("date"), db_index=True)
    amount = MoneyField(_("amount"), help_text=_("Positive is money in."))
    description = models.CharField(_("description"), max_length=255, blank=True)
    payee = models.CharField(_("payee"), max_length=255, blank=True)
    memo = models.CharField(_("memo"), max_length=255, blank=True)
    external_id = models.CharField(_("bank reference"), max_length=255)
    running_balance = MoneyField(_("running balance"), null=True, blank=True)
    sequence = models.PositiveIntegerField(default=0)  # order within the batch
    status = models.CharField(
        _("status"),
        max_length=12,
        choices=TxnStatus.choices,
        default=TxnStatus.UNMATCHED,
        db_index=True,
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("bank transaction")
        verbose_name_plural = _("bank transactions")
        ordering = ["-date", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["bank_account", "external_id"], name="banking_txn_unique_external_id"
            )
        ]

    def __str__(self):
        return f"{self.date} {self.amount} {self.description}"

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            old = type(self).all_associations.filter(pk=self.pk).values(*self.SOURCE_FIELDS)
            old = old.first()
            if old and any(old[f] != getattr(self, f) for f in self.SOURCE_FIELDS):
                raise TransactionImmutableError("Imported bank transactions can't be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TransactionImmutableError("Imported bank transactions can't be deleted.")

    reconciliation = models.ForeignKey(
        "Reconciliation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cleared",
        verbose_name=_("reconciliation"),
        help_text=_("Set when a completed reconciliation clears this transaction (locks it)."),
    )

    @property
    def is_deposit(self):
        return self.amount > 0

    @property
    def is_locked(self):
        return self.reconciliation_id is not None


class StatementBalance(AssociationScopedModel):
    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.CASCADE, related_name="statement_balances"
    )
    as_of = models.DateField(_("as of"))
    amount = MoneyField(_("balance"))
    available = MoneyField(_("available balance"), null=True, blank=True)
    batch = models.ForeignKey(
        ImportBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="balances"
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("statement balance")
        verbose_name_plural = _("statement balances")
        ordering = ["-as_of", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["bank_account", "as_of"], name="banking_stmtbal_unique_date"
            )
        ]

    def __str__(self):
        return f"{self.bank_account} {self.as_of} {self.amount}"


# ------------------------------------------------------------- matching


class PayerAlias(AssociationScopedModel):
    """A payer name seen on deposits that belongs to a unit (e.g. "RAFAEL ORTIZ" -> unit 11).

    Learned when the treasurer confirms a match; ``text`` is the normalized payer name.
    """

    text = models.CharField(_("payer name"), max_length=255)
    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.CASCADE, related_name="+", verbose_name=_("unit")
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("payer alias")
        verbose_name_plural = _("payer aliases")
        ordering = ["text"]
        constraints = [
            models.UniqueConstraint(fields=["association", "text"], name="banking_alias_unique")
        ]

    def __str__(self):
        return f"{self.text} → {self.unit}"


class CategorizeRule(AssociationScopedModel):
    """Payee/description contains ``contains`` -> suggest categorizing to ``account``."""

    contains = models.CharField(_("description contains"), max_length=100)
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="+", verbose_name=_("account")
    )
    note = models.CharField(_("note"), max_length=200, blank=True)
    active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("categorize rule")
        verbose_name_plural = _("categorize rules")
        ordering = ["contains"]

    def __str__(self):
        return f"{self.contains} → {self.account}"


class MatchKind(models.TextChoices):
    RECEIPT = "receipt", _("Owner payment")
    LINKED = "linked", _("Linked to recorded payment")
    CATEGORIZED = "categorized", _("Categorized")
    INTERFUND = "interfund", _("Reserve transfer")
    BILL = "bill", _("Bill payment")


class Match(AssociationScopedModel):
    """How one bank transaction (or part of it, for a split) was accounted for.

    ``entry`` is the journal entry: posted by matching (receipt, categorized, interfund) or an
    existing recorded receipt (linked, ``posted`` false). Undo reverses posted entries and
    sets ``undone_at``; the row stays as history.
    """

    txn = models.ForeignKey(BankTransaction, on_delete=models.PROTECT, related_name="matches")
    kind = models.CharField(_("kind"), max_length=12, choices=MatchKind.choices)
    amount = MoneyField(_("amount"))
    unit = models.ForeignKey(
        "registry.Unit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("unit"),
    )
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("account"),
    )
    entry = models.ForeignKey(
        "ledger.JournalEntry", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    receipt = models.ForeignKey(
        "payments.Receipt", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    bill = models.ForeignKey(
        "ap.Bill",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bank_matches",
        verbose_name=_("bill"),
    )
    posted = models.BooleanField(default=True)
    reason = models.CharField(_("reason"), max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    undone_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("bank match")
        verbose_name_plural = _("bank matches")
        ordering = ["pk"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount}"


class Reconciliation(AssociationScopedModel):
    """A bank reconciliation for one account and statement date (FIN-07).

    ``difference = ending_balance - (beginning_balance + cleared total)``. Only a zero
    difference can be completed; completing locks the cleared transactions.
    """

    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name="reconciliations"
    )
    statement_date = models.DateField(_("statement date"))
    beginning_balance = MoneyField(_("beginning balance"))
    ending_balance = MoneyField(_("ending balance"))
    cleared_total = MoneyField(_("cleared total"), default=ZERO)
    book_start = models.DateField(
        _("books compared after"),
        null=True,
        blank=True,
        help_text=_("Ledger cash before this date is covered by the first beginning balance."),
    )
    book_note = models.TextField(_("note on book difference"), blank=True)
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("completed by"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("reconciliation")
        verbose_name_plural = _("reconciliations")
        ordering = ["-statement_date", "-pk"]

    def __str__(self):
        return f"{self.bank_account} {self.statement_date}"

    @property
    def difference(self):
        return self.ending_balance - (self.beginning_balance + self.cleared_total)

    @property
    def is_complete(self):
        return self.completed_at is not None


class ReserveApproval(AssociationScopedModel):
    """Board approval for money leaving a reserve bank account (``banking.reserve``).

    Created only through a change request, so two board members stand behind it (the
    proposer and the approver). Approved before the move, it waits with ``txn`` empty
    until a reserve debit of the same amount is matched; approved after the fact, it names
    the bank transaction from the start.
    """

    amount = MoneyField(_("amount"))
    purpose = models.CharField(_("purpose"), max_length=300)
    txn = models.OneToOneField(
        BankTransaction,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reserve_approval",
        verbose_name=_("bank transaction"),
    )
    after_the_fact = models.BooleanField(_("approved after the money moved"), default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("reserve withdrawal approval")
        verbose_name_plural = _("reserve withdrawal approvals")
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.amount} · {self.purpose}"
