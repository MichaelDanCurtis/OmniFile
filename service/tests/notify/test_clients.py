"""The Graph and Bot Connector clients against the fakes (no network)."""

from __future__ import annotations

import pytest

from omnifile_service.notify.botconnector import BotError
from omnifile_service.notify.graph import GraphError, group_sids_for

from .conftest import run
from .support import BOT_BASE, DOMAIN, TEAMS_APP_ID, Fakes, add_person, new_oid  # noqa: F401

USER_OID = "0f0f0f0f-1111-2222-3333-444444444444"
SID = f"{DOMAIN}-1105"


def seed_graph(fakes: Fakes) -> None:
    g = fakes.graph
    g.add_group("10000000-0000-0000-0000-000000000001", sid=f"{DOMAIN}-2001", displayName="Seismic")
    g.add_group(
        "10000000-0000-0000-0000-000000000002",
        sid=f"{DOMAIN}-2002",
        displayName="Geo",
        groups=["10000000-0000-0000-0000-000000000001"],
    )
    g.add_group("10000000-0000-0000-0000-000000000003", sid=None, displayName="Cloud only")
    g.add_user(
        USER_OID,
        sid=SID,
        displayName="Geo Scientist",
        mail="geo@contoso.example",
        groups=["10000000-0000-0000-0000-000000000002", "10000000-0000-0000-0000-000000000003"],
    )


def test_user_and_groups(fakes: Fakes) -> None:
    seed_graph(fakes)
    fakes.graph.page_size = 1  # every list pages

    async def go():
        user = await fakes.notifier.graph.user_by_sid(SID)
        missing = await fakes.notifier.graph.user_by_sid(f"{DOMAIN}-9999")
        groups = await fakes.notifier.graph.transitive_group_sids(USER_OID)
        members = await fakes.notifier.graph.group_members(
            "10000000-0000-0000-0000-000000000001", 50
        )
        synced = [g async for g in fakes.notifier.graph.synced_groups()]
        return user, missing, groups, members, synced

    user, missing, groups, members, synced = run(go())
    assert user.object_id == USER_OID and user.mail == "geo@contoso.example"
    assert missing is None
    assert sorted(groups) == [f"{DOMAIN}-2001", f"{DOMAIN}-2002"]  # nested, SID-less dropped
    assert group_sids_for(SID, groups) == sorted(
        [f"{DOMAIN}-2001", f"{DOMAIN}-2002", f"{DOMAIN}-513", "S-1-5-11"]
    )
    assert [m.object_id for m in members] == [USER_OID]
    assert len(synced) == 3


def test_install_chat_block_and_mail(fakes: Fakes) -> None:
    seed_graph(fakes)
    graph = fakes.notifier.graph

    async def go():
        await graph.install_app(USER_OID, TEAMS_APP_ID)
        await graph.install_app(USER_OID, TEAMS_APP_ID)  # 409: already installed, fine
        inst = await graph.installation_id(USER_OID, TEAMS_APP_ID)
        chat = await graph.chat_id(USER_OID, inst)
        fakes.graph.blocked.add("20000000-0000-0000-0000-000000000000")
        fakes.graph.add_user("20000000-0000-0000-0000-000000000000", sid=f"{DOMAIN}-1106")
        with pytest.raises(GraphError) as blocked:
            await graph.install_app("20000000-0000-0000-0000-000000000000", TEAMS_APP_ID)
        await graph.send_mail("omnifile@contoso.example", "geo@contoso.example", "S", "<p>b</p>")
        return chat, blocked.value

    chat, blocked = run(go())
    assert chat.startswith("19:")
    assert blocked.status == 403 and blocked.code == "graph_status_403"
    assert fakes.graph.mail == [
        {
            "sender": "omnifile@contoso.example",
            "to": ["geo@contoso.example"],
            "subject": "S",
            "html": "<p>b</p>",
        }
    ]


def test_graph_refuses_foreign_next_links(fakes: Fakes) -> None:
    graph = fakes.notifier.graph

    async def go():
        async for _ in graph._pages("https://evil.example/v1.0/users"):
            pass

    with pytest.raises(GraphError, match="foreign_url"):
        run(go())


def test_bot_send_update_and_errors(fakes: Fakes) -> None:
    bot = fakes.notifier.bot
    card = {"type": "AdaptiveCard", "version": "1.4", "body": []}

    async def go():
        aid = await bot.send(f"{BOT_BASE}/", "19:abc", card, "Recalling x")
        await bot.update(f"{BOT_BASE}/", "19:abc", aid, {**card, "body": [{"x": 1}]}, "s")
        fakes.bot.fail_next(429, retry_after=3)
        with pytest.raises(BotError) as throttled:
            await bot.update(f"{BOT_BASE}/", "19:abc", aid, card, "s")
        fakes.bot.fail_next(403)
        with pytest.raises(BotError) as gone:
            await bot.send(f"{BOT_BASE}/", "19:abc", card, "s")
        with pytest.raises(BotError, match="service_url_not_https"):
            await type(bot)(fakes.http, fakes.notifier.bot._tokens).send(
                "http://bot.test/", "19:abc", card, "s"
            )
        return aid, throttled.value, gone.value

    aid, throttled, gone = run(go())
    acts = fakes.bot.activities("19:abc")
    assert len(acts) == 1 and len(acts[0].versions) == 2
    assert fakes.bot.card("19:abc")["body"] == [{"x": 1}]
    assert throttled.throttled and throttled.retry_after == 3.0
    assert gone.gone and not gone.throttled
