"""Funding and recovery on Gnosis Chain.

``chain`` is stdlib-only. ``wallet`` and ``recover`` need the ``remote``
extra (``pip install -e '.[remote]'``, which brings web3); they import it
lazily so local benchmarking never needs it.
"""
