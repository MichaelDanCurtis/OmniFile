"""The M4 dev stack's Microsoft stand-ins, in one process (teams.md section 11).

    python dev/m4/fakes.py --seed FILE --graph-port P --bot-port P --relay-url URL

Runs, from `omnifile_service.notify.fakes`, unchanged:
- the fake Microsoft Graph (and Entra token endpoint) on --graph-port: the worker's
  OMNIFILE_GRAPH_URL is http://127.0.0.1:<port>/v1.0, OMNIFILE_ENTRA_AUTHORITY the root;
- the fake Bot Connector (token endpoint and activities) on --bot-port: the worker's
  OMNIFILE_TEAMS_SERVICE_URL is http://127.0.0.1:<port>/;
- the fake Teams client, which signs Bot Framework JWTs with a test key. Its OpenID
  metadata and JWKS are served on the bot port under /_teams/<path of login.botframework.com>,
  where dev/m4/relay.py points the real relay's http client.

Test controls, on the bot port (dev and tests only; nothing here is a Microsoft API):
  GET  /_fake/conversations                     every conversation: activities, card versions
  GET  /_fake/conversations/{cid}/card          the latest card in that conversation
  POST /_fake/bot/fail {status, count}          the next sends/updates answer `status`
                       {clear: true}            drop the injected answers
  POST /_fake/teams/click {conversation_id, title, clicker[, card]}
                                                click a button the way Teams does, through
                                                the relay; returns the relay's reply
  POST /_fake/teams/replay                      send the last click again, byte for byte
  GET  /_fake/graph                             Graph's state: mail sent, installs, calls
  POST /_fake/graph/users {...}                 add a user (FakeGraph.add_user fields)
  POST /_fake/graph/groups {...}                add a group
  POST /_fake/graph/fail {prefix, status, count}

The seed file (JSON):
  {"tenant", "bot_app_id", "bot_secret", "graph_client_id", "graph_secret",
   "graph": {"users": [...], "groups": [...], "blocked": [...]}}   (FakeGraph.from_seed)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from omnifile_service.notify.fakes import FakeBotConnector, FakeGraph, FakeTeams
from omnifile_service.notify.fakes.teams import JWKS_URL, METADATA_URL

log = logging.getLogger("omnifile-dev-fakes")
LOGIN_HOST = "https://login.botframework.com"


class Fakes:
    def __init__(self, seed: dict[str, Any], relay_url: str) -> None:
        self.graph = FakeGraph.from_seed({"tenant": seed["tenant"], **seed.get("graph", {})})
        self.graph.clients[seed["graph_client_id"]] = seed["graph_secret"]
        self.bot = FakeBotConnector(clients={seed["bot_app_id"]: seed["bot_secret"]})
        self._relay = httpx.Client(base_url=relay_url, trust_env=False, timeout=10.0)
        self.teams = FakeTeams(
            bot_app_id=seed["bot_app_id"], tenant_id=seed["tenant"], bot=self.bot, post=self._post
        )
        self.last: tuple[bytes, dict[str, str]] | None = None

    def _post(self, path: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
        self.last = (body, dict(headers))
        return self._relay.post(path, content=body, headers=headers)

    # --- the bot port -------------------------------------------------------------------

    def control(self) -> Starlette:
        async def openid(request: Request) -> Response:
            # Microsoft's two documents, as FakeTeams serves them to an in-process relay.
            url = LOGIN_HOST + "/" + request.path_params["path"]
            if url not in (METADATA_URL, JWKS_URL):
                return Response(status_code=404)
            r = self.teams.handler(httpx.Request("GET", url))
            return Response(r.content, r.status_code, media_type="application/json")

        async def conversations(_: Request) -> Response:
            out = {
                cid: [
                    {"activity_id": a.activity_id, "versions": [_card(v) for v in a.versions]}
                    for a in acts
                ]
                for cid, acts in self.bot.conversations.items()
            }
            return JSONResponse(out)

        async def card(request: Request) -> Response:
            try:
                return JSONResponse(self.bot.card(request.path_params["cid"]))
            except LookupError:
                return JSONResponse({"error": "no activity"}, 404)

        async def bot_fail(request: Request) -> Response:
            b = await request.json()
            if b.get("clear"):
                self.bot.failures.clear()
            else:
                self.bot.fail_next(int(b["status"]), int(b.get("count", 1)), b.get("retry_after"))
            return JSONResponse({"ok": True})

        async def click(request: Request) -> Response:
            b = await request.json()
            # The relay fetches its JWKS from this process: never block the loop on it.
            try:
                r = await asyncio.to_thread(
                    self.teams.click,
                    b["conversation_id"],
                    b["title"],
                    clicker=b["clicker"],
                    card=b.get("card"),
                )
            except LookupError as e:
                return JSONResponse({"error": str(e)}, 404)
            return JSONResponse(_reply(r))

        async def replay(_: Request) -> Response:
            if self.last is None:
                return JSONResponse({"error": "nothing clicked yet"}, 404)
            body, headers = self.last
            r = await asyncio.to_thread(
                self._relay.post, "/api/messages", content=body, headers=headers
            )
            return JSONResponse(_reply(r))

        async def graph_state(_: Request) -> Response:
            return JSONResponse(
                {
                    "mail": self.graph.mail,
                    "installed": self.graph.installed,
                    "chats": self.graph.chats,
                    "calls": [list(c) for c in self.graph.calls[-200:]],
                }
            )

        async def graph_user(request: Request) -> Response:
            self.graph.add_user(**(await request.json()))
            return JSONResponse({"ok": True})

        async def graph_group(request: Request) -> Response:
            self.graph.add_group(**(await request.json()))
            return JSONResponse({"ok": True})

        async def graph_fail(request: Request) -> Response:
            b = await request.json()
            self.graph.fail[b["prefix"]] = (int(b["status"]), int(b.get("count", 1)))
            return JSONResponse({"ok": True})

        async def healthz(_: Request) -> Response:
            return JSONResponse({"status": "ok"})

        return Starlette(
            routes=[
                Route("/healthz", healthz),
                Route("/_teams/{path:path}", openid),
                Route("/_fake/conversations", conversations),
                Route("/_fake/conversations/{cid:path}/card", card),
                Route("/_fake/bot/fail", bot_fail, methods=["POST"]),
                Route("/_fake/teams/click", click, methods=["POST"]),
                Route("/_fake/teams/replay", replay, methods=["POST"]),
                Route("/_fake/graph", graph_state),
                Route("/_fake/graph/users", graph_user, methods=["POST"]),
                Route("/_fake/graph/groups", graph_group, methods=["POST"]),
                Route("/_fake/graph/fail", graph_fail, methods=["POST"]),
                Mount("/", app=self.bot.app()),
            ]
        )


def _card(version: dict[str, Any]) -> dict[str, Any]:
    return {"seq": version.get("_seq"), "card": version["attachments"][0]["content"]}


def _reply(r: httpx.Response) -> dict[str, Any]:
    try:
        body = r.json() if r.content else None
    except ValueError:
        body = r.text
    return {"status": r.status_code, "body": body}


async def serve(args: argparse.Namespace) -> None:
    seed = json.loads(Path(args.seed).read_text())
    fakes = Fakes(seed, args.relay_url)
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                fakes.graph.app(),
                host=args.host,
                port=args.graph_port,
                log_level=args.log_level,
                lifespan="off",
            )
        ),
        uvicorn.Server(
            uvicorn.Config(
                fakes.control(),
                host=args.host,
                port=args.bot_port,
                log_level=args.log_level,
                lifespan="off",
            )
        ),
    ]
    await asyncio.gather(*(s.serve() for s in servers))


def main() -> None:
    p = argparse.ArgumentParser(prog="dev/m4/fakes.py")
    p.add_argument("--seed", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--graph-port", type=int, required=True)
    p.add_argument("--bot-port", type=int, required=True)
    p.add_argument("--relay-url", required=True)
    p.add_argument("--log-level", default="warning")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
