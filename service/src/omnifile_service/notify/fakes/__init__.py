"""Fakes for tests and the dev stack (teams.md section 11). Never used in production: the
worker refuses to point at them unless OMNIFILE_DEV_MODE=1.

- `graph.FakeGraph`: users by on-premises SID, groups, Teams app install and chat, sendMail,
  and an Entra-style token endpoint.
- `botconnector.FakeBotConnector`: a token endpoint and the activity send/update calls,
  recording every card version per conversation; can answer 403, 404 or 429.
- `teams.FakeTeams`: plays Teams. It signs Bot Framework JWTs with a test RSA key, serves the
  matching OpenID metadata and JWKS (as `login.botframework.com`, through an httpx
  transport), and clicks a card's button by POSTing the invoke to a relay.
"""

from .botconnector import FakeBotConnector
from .graph import FakeGraph
from .teams import FakeTeams

__all__ = ["FakeBotConnector", "FakeGraph", "FakeTeams"]
