"""Owner votes (GOV-07): a vote has questions, each with choices; each unit casts at most
one active ballot. Thresholds and quorum come from the rule pack or a generic default
(see :mod:`homeoa.votes.services`); nothing here holds statute values.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class VoteKind(models.TextChoices):
    MEETING = "meeting", _("Vote at a meeting")
    WRITTEN = "written", _("Written consent / ballot by mail")


class Weighting(models.TextChoices):
    # Values match homeoa.registry.services.VOTE_WEIGHTINGS.
    PER_UNIT = "per_unit", _("One vote per unit")
    ALLOCATED_INTEREST = "allocated_interest", _("By allocated interest")
    CUSTOM = "custom", _("By custom unit weight")


class Threshold(models.TextChoices):
    MAJORITY_CAST = "majority_cast", _("Majority of votes cast")
    MAJORITY_ALL = "majority_all", _("Majority of all votes")
    RULE_PACK = "rule_pack", _("Share of all votes set by the rule pack")


class PackKind(models.TextChoices):
    # Values match homeoa.rules.keys.VOTE_KINDS.
    AUDIT_WAIVER = "audit_waiver", _("Audit waiver")
    BUDGET_REJECTION = "budget_rejection", _("Budget rejection")


class Comparison(models.TextChoices):
    AT_LEAST = "at_least", _("At least the threshold")
    MORE_THAN = "more_than", _("More than the threshold")


class Quorum(models.TextChoices):
    NONE = "none", _("No quorum")
    RULE_PACK = "rule_pack", _("Owner meeting quorum from the rule pack")
    CUSTOM = "custom", _("Custom percentage")


class VoteStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    OPEN = "open", _("Open")
    CLOSED = "closed", _("Closed")


class Channel(models.TextChoices):
    ELECTRONIC = "electronic", _("Electronic")
    PAPER = "paper", _("Paper")
    IN_PERSON = "in_person", _("In person")


class ClosedVoteError(Exception):
    """A closed vote (and its result) cannot change."""


class Vote(AssociationScopedModel):
    title = models.CharField(_("title"), max_length=200)
    description = models.TextField(_("description"), blank=True)
    kind = models.CharField(_("kind"), max_length=10, choices=VoteKind.choices)
    opens_at = models.DateTimeField(_("opens at"))
    closes_at = models.DateTimeField(_("closes at"))
    weighting = models.CharField(_("weighting"), max_length=20, choices=Weighting.choices)
    threshold = models.CharField(
        _("threshold"), max_length=20, choices=Threshold.choices, default=Threshold.MAJORITY_CAST
    )
    pack_kind = models.CharField(
        _("rule-pack vote"), max_length=20, choices=PackKind.choices, blank=True
    )
    comparison = models.CharField(
        _("comparison"), max_length=10, choices=Comparison.choices, blank=True
    )
    quorum = models.CharField(
        _("quorum"), max_length=10, choices=Quorum.choices, default=Quorum.NONE
    )
    quorum_percent = models.DecimalField(
        _("quorum (%)"),
        max_digits=7,
        decimal_places=4,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    secret = models.BooleanField(
        _("secret ballot"),
        default=False,
        help_text=_("The board sees who voted, not how."),
    )
    status = models.CharField(
        _("status"), max_length=10, choices=VoteStatus.choices, default=VoteStatus.DRAFT
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("created by"),
    )
    closed_at = models.DateTimeField(_("closed at"), null=True, blank=True)
    result = models.JSONField(_("result"), null=True, blank=True)
    ballots_hash = models.CharField(_("ballots hash"), max_length=64, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("vote")
        verbose_name_plural = _("votes")
        ordering = ["-opens_at", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(closes_at__gt=models.F("opens_at")),
                name="votes_vote_closes_after_opens",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def clean(self):
        if self.opens_at and self.closes_at and self.closes_at <= self.opens_at:
            raise ValidationError(_("A vote must close after it opens."))
        if self.threshold == Threshold.RULE_PACK and not self.pack_kind:
            raise ValidationError(_("Choose which rule-pack vote sets the threshold."))
        if self.quorum == Quorum.CUSTOM and self.quorum_percent is None:
            raise ValidationError(_("Enter the quorum percentage."))

    def save(self, *args, **kwargs):
        if self.pk is not None:
            stored = Vote.all_associations.filter(pk=self.pk).values_list("status", flat=True)
            if stored.first() == VoteStatus.CLOSED:
                raise ClosedVoteError(_("A closed vote cannot change."))
        super().save(*args, **kwargs)


class Question(AssociationScopedModel):
    vote = models.ForeignKey(
        Vote, on_delete=models.CASCADE, related_name="questions", verbose_name=_("vote")
    )
    text = models.CharField(_("question"), max_length=500)
    position = models.PositiveSmallIntegerField(_("position"), default=0)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("question")
        verbose_name_plural = _("questions")
        ordering = ["position", "pk"]

    def __str__(self) -> str:
        return self.text


class Choice(AssociationScopedModel):
    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="choices", verbose_name=_("question")
    )
    label = models.CharField(_("label"), max_length=200)
    position = models.PositiveSmallIntegerField(_("position"), default=0)
    affirmative = models.BooleanField(
        _("counts toward the threshold"),
        default=False,
        help_text=_("The choice that passes the question (e.g. 'Yes' or 'Waive the audit')."),
    )
    abstain = models.BooleanField(
        _("abstain"),
        default=False,
        help_text=_("Counts toward quorum but not as a vote cast."),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("choice")
        verbose_name_plural = _("choices")
        ordering = ["position", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["question"],
                condition=models.Q(affirmative=True),
                name="votes_choice_one_affirmative",
            ),
            models.CheckConstraint(
                condition=~models.Q(affirmative=True, abstain=True),
                name="votes_choice_not_affirmative_and_abstain",
            ),
        ]

    def __str__(self) -> str:
        return self.label


class Ballot(AssociationScopedModel):
    """One unit's ballot. Who voted (unit, person, channel) is kept here; how they voted
    lives in :class:`BallotAnswer`, which display code hides for secret votes. A later
    ballot for the same unit supersedes this one (``superseded_by``); superseded ballots
    are kept."""

    vote = models.ForeignKey(
        Vote, on_delete=models.PROTECT, related_name="ballots", verbose_name=_("vote")
    )
    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    person = models.ForeignKey(
        "registry.Person", on_delete=models.PROTECT, related_name="+", verbose_name=_("voter")
    )
    channel = models.CharField(_("channel"), max_length=10, choices=Channel.choices)
    cast_at = models.DateTimeField(_("cast at"))
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("entered by"),
    )
    scan_file_key = models.CharField(_("scanned ballot"), max_length=500, blank=True)
    scan_file_name = models.CharField(_("scan file name"), max_length=200, blank=True)
    superseded_by = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supersedes",
        verbose_name=_("superseded by"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("ballot")
        verbose_name_plural = _("ballots")
        ordering = ["cast_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["vote", "unit"],
                condition=models.Q(superseded_by__isnull=True),
                name="votes_ballot_one_active_per_unit",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vote} · {self.unit}"

    def save(self, *args, **kwargs):
        status = Vote.all_associations.filter(pk=self.vote_id).values_list("status", flat=True)
        if status.first() == VoteStatus.CLOSED:
            raise ClosedVoteError(_("This vote is closed."))
        super().save(*args, **kwargs)

    @property
    def active(self) -> bool:
        return self.superseded_by_id is None


class BallotAnswer(AssociationScopedModel):
    ballot = models.ForeignKey(
        Ballot, on_delete=models.PROTECT, related_name="answers", verbose_name=_("ballot")
    )
    question = models.ForeignKey(
        Question, on_delete=models.PROTECT, related_name="+", verbose_name=_("question")
    )
    choice = models.ForeignKey(
        Choice, on_delete=models.PROTECT, related_name="+", verbose_name=_("choice")
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("ballot answer")
        verbose_name_plural = _("ballot answers")
        constraints = [
            models.UniqueConstraint(
                fields=["ballot", "question"], name="votes_answer_one_per_question"
            ),
        ]
