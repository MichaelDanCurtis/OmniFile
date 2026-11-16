# Fixture for delete_scan (never imported): backend deletes through every
# kind of receiver, each of which the scan must find.
# ruff: noqa


async def by_name(backend, ref):
    await backend.delete(ref)


async def by_registry_call(registry, bid, ref):
    await registry.get(bid).delete(ref)


class Holder:
    async def by_attribute(self, ref):
        await self._backend.delete(ref)


async def by_subscript(backends, bid, ref):
    await backends[bid].delete(ref)
