from duma import confirmations
from duma.confirmations import Confirmations


async def make(db, ttl=60):
    now = [1000.0]
    return await Confirmations.open(db, ttl, clock=lambda: now[0]), now


async def test_a_proposal_is_decided_once_and_only_by_who_asked(db):
    store, _ = await make(db)
    action = await store.propose(10, "preference_add", {"rule": "tablas"}, "Regla: «tablas»")

    assert await store.claim(action, 20) == ("not_yours", None)  # someone else's click consumes nothing
    status, pending = await store.claim(action, 10)
    assert status == "ok" and pending.payload == {"rule": "tablas"} and pending.summary == "Regla: «tablas»"
    assert (await store.claim(action, 10))[0] == "gone"  # the double click
    assert await store.claim("no-such-id", 10) == ("gone", None)


async def test_it_expires_and_an_expired_one_cannot_be_revived(db):
    store, now = await make(db, ttl=60)
    action = await store.propose(10, "preference_add", {"rule": "x"}, "s")
    now[0] += 61
    assert (await store.claim(action, 10))[0] == "expired"
    now[0] -= 61
    assert (await store.claim(action, 10))[0] == "gone"


async def test_proposals_survive_a_restart(db):
    store, _ = await make(db)
    action = await store.propose(5_000_000_000, "preference_add", {"rule": "x"}, "s")  # a Telegram id past 32 bits
    again, _ = await make(db)  # what a new process does: open the same tables
    assert (await again.claim(action, 5_000_000_000))[0] == "ok"


async def test_old_proposals_are_forgotten_and_live_ones_kept(db):
    store, now = await make(db, ttl=60)
    old = await store.propose(10, "preference_add", {"rule": "x"}, "s")
    now[0] += 86400 + 61
    fresh = await store.propose(10, "preference_add", {"rule": "y"}, "s")
    await store.purge()
    assert await store.claim(old, 10) == ("gone", None) and (await store.claim(fresh, 10))[0] == "ok"


def test_buttons_carry_only_the_id():
    pair = confirmations.buttons("abc123", "Guardar")
    assert pair == [("Guardar", "ok:abc123"), ("Cancelar", "no:abc123")]
    assert confirmations.parse("ok:abc123") == ("ok", "abc123") and confirmations.parse("no:abc123") == ("no", "abc123")
    assert confirmations.parse("borrar:todo") is None and confirmations.parse("ok:") is None and confirmations.parse("") is None
