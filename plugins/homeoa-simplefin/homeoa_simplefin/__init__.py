"""SimpleFIN Bridge bank feed for HomeOA (FIN-07, PLG-03).

* ``manifest``    plugin manifest (entry point ``simplefin``).
* ``client``      the SimpleFIN protocol over httpx: claim a setup token, read ``/accounts``.
* ``fake_server`` an ``httpx.MockTransport`` speaking the same protocol with fixtures.
* ``sync``        claim + sync services; the only code that writes, through
                  ``homeoa.banking.services.import_transactions``.
* ``provider``    ``SimpleFinFeed``, the ``BankFeedProvider`` implementation.
* ``tasks``       the periodic sync job (hourly tick, per-association interval, default 6 h).
* ``todos``       board to-do items for connection problems (expired access, unmapped).

The fake is used when the plugin setting ``use_fake`` is on or ``HOMEOA_SIMPLEFIN_FAKE=1``.
"""
