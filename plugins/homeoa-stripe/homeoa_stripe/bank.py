"""Linking an owner's bank account: Stripe SetupIntent with real verification.

Flow (``views.bank_*``):

1. ``start(unit, owner)``: a Stripe customer (reused from the owner's earlier mandates)
   and a SetupIntent with ``verification_method=automatic``: Financial Connections
   (instant) first, microdeposits as the fallback.
2. The browser collects the account: with the real client, Stripe.js
   ``collectBankAccountForSetup``; with the fake, ``fake.FakeStripe.collect_bank_account``
   (instant, manual entry or failure).
3. ``confirm(...)``: the owner accepts the authorization text online; the SetupIntent is
   confirmed with an **online** customer acceptance (IP address and browser). Instant
   verification gives a chargeable mandate at once; microdeposits give a recorded but
   not yet chargeable mandate.
4. ``verify(mandate, amounts=... | descriptor_code=...)``: Stripe checks the
   microdeposits and the mandate becomes chargeable.

Nothing here moves money.
"""

from __future__ import annotations

from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.plugins.services import installation_for

from .client import get_client
from .mandates import MandateError, record_mandate
from .models import StripeMandate, Verification

PLUGIN_ID = "stripe"


def client_for(association):
    return get_client(installation_for(association, PLUGIN_ID))


def _customer_for(owner, client) -> str:
    prior = (
        StripeMandate.all_associations.filter(association_id=owner.association_id, owner=owner)
        .exclude(stripe_customer_id="")
        .order_by("-accepted_at")
        .values_list("stripe_customer_id", flat=True)
        .first()
    )
    if prior:
        return prior
    customer = client.create_customer(
        name=owner.full_name,
        email=owner.email or "",
        metadata={"association_id": owner.association_id, "person_id": owner.pk},
    )
    return customer["id"]


def start(unit, owner, *, client=None) -> dict:
    """Create the SetupIntent. Returns ``{"setup_id", "client_secret", "customer"}``."""
    client = client or client_for(unit.association)
    try:
        customer = _customer_for(owner, client)
        setup = client.create_setup_intent(
            customer=customer,
            metadata={"association_id": unit.association_id, "unit": unit.number},
        )
    except Exception as exc:
        raise MandateError(
            _("Stripe could not start linking a bank account: %(e)s") % {"e": exc}
        ) from exc
    return {
        "setup_id": setup["id"],
        "client_secret": setup["client_secret"],
        "customer": customer,
    }


def _pm(setup: dict) -> dict:
    pm = setup.get("payment_method")
    return pm if isinstance(pm, dict) else {}


def collected_account(setup_id: str, *, association, client=None) -> dict:
    """``{"last4", "bank_name"}`` of the account collected for ``setup_id`` ({} if none)."""
    client = client or client_for(association)
    try:
        setup = client.retrieve_setup_intent(setup_id)
    except Exception:
        return {}
    bank = _pm(setup).get("us_bank_account") or {}
    if not bank.get("last4"):
        return {}
    return {"last4": str(bank["last4"]), "bank_name": str(bank.get("bank_name") or "")}


def confirm(
    unit, owner, setup_id: str, *, user=None, ip_address: str, user_agent: str, client=None
) -> StripeMandate:
    """The owner accepted the authorization: confirm online and record the mandate."""
    client = client or client_for(unit.association)
    try:
        setup = client.confirm_setup_intent(
            setup_id, ip_address=ip_address or "", user_agent=user_agent or ""
        )
    except Exception as exc:
        raise MandateError(_("The bank account could not be linked: %(e)s") % {"e": exc}) from exc
    status = setup.get("status")
    if status not in ("succeeded", "requires_action"):
        raise MandateError(_("The bank account could not be linked."))
    pm = _pm(setup)
    bank = pm.get("us_bank_account") or {}
    instant = status == "succeeded"
    return record_mandate(
        unit,
        owner,
        str(bank.get("last4") or ""),
        user=user,
        ip_address=ip_address,
        user_agent=user_agent,
        stripe_mandate_id=str(setup.get("mandate") or ""),
        stripe_customer_id=str(setup.get("customer") or ""),
        stripe_payment_method_id=str(pm.get("id") or ""),
        setup_intent_id=setup_id,
        bank_name=str(bank.get("bank_name") or ""),
        verification=Verification.INSTANT if instant else Verification.MICRODEPOSITS,
        verified_at=timezone.now() if instant else None,
    )


def verify(
    mandate: StripeMandate,
    *,
    amounts: list[int] | None = None,
    descriptor_code: str = "",
    client=None,
) -> StripeMandate:
    """Check the microdeposits; the mandate becomes chargeable on success."""
    if mandate.verified_at is not None:
        return mandate
    if mandate.revoked_at is not None or not mandate.setup_intent_id:
        raise MandateError(_("This bank account can't be verified."))
    client = client or client_for(mandate.association)
    try:
        setup = client.verify_microdeposits(
            mandate.setup_intent_id, amounts=amounts, descriptor_code=descriptor_code
        )
    except Exception as exc:
        raise MandateError(_("Verification failed: %(e)s") % {"e": exc}) from exc
    if setup.get("status") != "succeeded":
        raise MandateError(_("Verification failed."))
    mandate.verified_at = timezone.now()
    mandate.stripe_mandate_id = str(setup.get("mandate") or mandate.stripe_mandate_id)[:64]
    pm = _pm(setup)
    if pm.get("id"):
        mandate.stripe_payment_method_id = str(pm["id"])[:64]
    mandate.save(update_fields=["verified_at", "stripe_mandate_id", "stripe_payment_method_id"])
    audit.record("stripe.mandate_verified", mandate, association=mandate.association)
    return mandate
