"""Default SignatureProvider: built-in click-to-sign (``homeoa.esign``)."""

from ..esign import SignatureProvider, SignatureRequest, SignatureResult, SignatureStatus


class ClickToSign(SignatureProvider):
    def request_signature(self, request: SignatureRequest) -> SignatureResult:
        from homeoa.core.models import Association
        from homeoa.esign import services

        association = Association.objects.get(pk=request.association_id)
        envelope = services.create_envelope(
            association,
            title=request.title,
            source_key=request.document_key,
            signers=[{"name": s.name, "email": s.email} for s in request.signers],
        )
        services.send(envelope)
        return SignatureResult(status=SignatureStatus.REQUESTED, reference=str(envelope.pk))
