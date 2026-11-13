# homeoa-lob

`PostalProvider` that sends letters through Lob, plus a tracking webhook.

Only an in-repo fake (`homeoa_lob.fake.FakeLob`) exists: it returns Lob-shaped letter ids
and tracking numbers and never touches the network. There is no live API key path; a
real client is future work and needs sandbox keys from the user first.

- Enable on `/settings/plugins/lob/` and set a webhook secret.
- Webhook: `POST /hooks/lob/<association_id>/`, Lob-style JSON
  (`{"id": "evt_…", "event_type": {"id": "letter.delivered"}, "body": {"id": "ltr_…"}}`),
  signed with `Lob-Signature` = hex HMAC-SHA256 of `"<Lob-Signature-Timestamp>.<raw body>"`.
- Certified mail: a batch marked certified sends with `extra_service=certified`; the fake
  returns a tracking number.
