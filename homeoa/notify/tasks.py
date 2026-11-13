"""Background jobs of the notification engine."""

from homeoa.jobs import app


@app.task(name="notify.send_email", queue="default")
def send_email(delivery_id: int) -> None:
    from homeoa.audit import audit_context
    from homeoa.core.context import association_context
    from homeoa.notify.channels.email import deliver
    from homeoa.notify.models import Delivery

    delivery = (
        Delivery.all_associations.select_related(
            "notification__recipient", "notification__association"
        )
        .filter(pk=delivery_id)
        .first()
    )
    if delivery is None:
        return
    with (
        association_context(delivery.notification.association),
        audit_context(label="job:notify.send_email"),
    ):
        deliver(delivery)
