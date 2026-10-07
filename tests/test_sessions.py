from duma.agent import Session
from duma.sessions import SessionStore
from duma.usage import Usage


async def make(db):
    now = [1000.0]
    return await SessionStore.open(db, clock=lambda: now[0]), now


async def test_a_session_comes_back_whole_after_a_restart(db):
    store, _ = await make(db)
    session = Session(context_tokens=5099, turns=2, notes="- pidió inactivos")
    session.messages += [
        {"role": "user", "content": "¿cuántos inactivos?"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "abc=="}, {"type": "text", "text": "Hay 311."}]},
    ]
    session.usage = Usage(input=12, cache_write=5097, cache_read=14841, output=218, calls=4)
    session.names.code(10, "Ana Peña")
    await store.save(5_000_000_000, 77, session)

    again, _ = await make(db)  # what a new process does: open the same tables
    loaded = await again.load(5_000_000_000, 77)
    assert loaded.messages == session.messages and loaded.usage == session.usage
    # Key order too: the history goes back to the model byte for byte.
    assert [list(b) for b in loaded.messages[1]["content"]] == [["type", "thinking", "signature"], ["type", "text"]]
    assert (loaded.context_tokens, loaded.turns, loaded.notes) == (5099, 2, "- pidió inactivos")
    assert loaded.names.restore("ATLETA_01") == "Ana Peña" and loaded.names.code(11, "Luis") == "ATLETA_02"
    assert await again.load(5_000_000_000, 78) is None and await again.load(20, 77) is None


async def test_saving_again_replaces_the_row(db):
    store, _ = await make(db)
    session = Session()
    await store.save(10, 77, session)
    session.messages.append({"role": "user", "content": "hola"})
    await store.save(10, 77, session)
    assert (await store.load(10, 77)).messages == [{"role": "user", "content": "hola"}]


async def test_a_topic_is_idle_only_when_every_admin_in_it_is(db):
    store, now = await make(db)
    await store.save(10, 77, Session())
    await store.save(10, 78, Session())
    now[0] += 3600
    await store.save(20, 77, Session())  # someone else wrote in 77 an hour later
    now[0] += 1800
    assert await store.idle_topics(3600) == [78]
    now[0] += 3600
    assert sorted(await store.idle_topics(3600)) == [77, 78]
    await store.delete_topic(77)
    assert await store.load(10, 77) is None and await store.load(20, 77) is None and await store.load(10, 78) is not None


async def test_a_row_from_another_shape_is_a_fresh_session_not_a_crash(db):
    store, _ = await make(db)
    await db.execute("INSERT INTO sessions VALUES (10, 77, $1, 0)", '{"messages": []}')
    await db.execute("INSERT INTO sessions VALUES (10, 78, $1, 0)", "not json")
    assert await store.load(10, 77) is None and await store.load(10, 78) is None
