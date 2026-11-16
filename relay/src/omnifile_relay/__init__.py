"""OmniFile Gateway Teams relay.

The only public endpoint in the product. It accepts Bot Framework ``adaptiveCard/action``
invokes, checks them, and forwards an accepted click inward to the private service.
It holds no data access, no bot credentials and no signing key.
"""

import importlib.metadata

try:
    # The installed distribution's version: its pyproject.toml, which release/stamp.sh writes from VERSION.
    __version__ = importlib.metadata.version("omnifile-relay")
except importlib.metadata.PackageNotFoundError:  # imported from a source tree that isn't installed
    __version__ = "0+unknown"
