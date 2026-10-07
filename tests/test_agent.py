from types import SimpleNamespace as NS

from duma import agent as agent_module
from duma.agent import Agent, Session
from duma.audit import Audit
from duma.tools import ToolResult


def text(value):
    return NS(type="text", text=value)


def tool_use(id_, name, **args):
    return NS(type="tool_use", id=id_, name=name, input=args)


def reply(content, stop="end_turn", tokens=1200):
    return NS(content=content, stop_reason=stop, usage=NS(input_tokens=tokens, cache_creation_input_tokens=0, cache_read_input_tokens=300, output_tokens=50))


class ScriptedLLM:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    async def create(self, **kwargs):
        # Snapshot: the agent keeps appending to the same list afterwards.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeBox:
    schemas = [{"name": "resumen_atleta"}]

    def __init__(self, result=None):
        self.result = result or ToolResult("RESUMEN EN EL CHAT", "Resumen enviado al chat: 2 de 3 entrenos.")
        self.ran = []

    async def run(self, name, args, user, names=None):
        self.names = names
        self.ran.append((name, args, user))
        return self.result


def make(settings, llm, box=None, audit=None):
    return Agent(settings, llm, box or FakeBox(), "PROMPT", audit)


async def run(agent, session, said, user_text="hola", user=10):
    async def send(t):
        said.append(t)

    return await agent.run(session, user_text, user, send)


async def test_a_plain_answer(settings):
    llm = ScriptedLLM(reply([text("Listo.")]))
    session, said = Session(), []
    assert await run(make(settings, llm), session, said) == "Listo."
    assert said == [] and session.turns == 1
    call = llm.calls[0]
    assert call["model"] == "claude-sonnet-5-5" and call["output_config"] == {"effort": "medium"}
    assert call["system"][0]["text"].startswith("PROMPT\n\nHoy es ") and call["max_tokens"] == 4000
    assert "tool_choice" not in call and "thinking" not in call  # forced tool use and `thinking: disabled` are rejected by this model


async def test_a_direct_tool_goes_to_the_chat_and_the_model_gets_the_acknowledgement(settings):
    llm = ScriptedLLM(reply([tool_use("tu_1", "resumen_atleta", nombre="ana")], stop="tool_use"), reply([text("¿Algo más?")]))
    box, session, said = FakeBox(), Session(), []
    answer = await run(make(settings, llm, box), session, said, "dame a ana", user=956)

    assert answer == "¿Algo más?" and said == ["RESUMEN EN EL CHAT"]
    assert box.ran == [("resumen_atleta", {"nombre": "ana"}, 956)]
    second = llm.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[2]["content"] == [{"type": "tool_result", "tool_use_id": "tu_1", "content": "Resumen enviado al chat: 2 de 3 entrenos."}]
    assert session.context_tokens == 1500  # input + cache reads, as the API reports them


async def test_all_the_results_of_parallel_calls_go_back_in_one_message(settings):
    llm = ScriptedLLM(
        reply([tool_use("a", "resumen_atleta", nombre="uno"), tool_use("b", "resumen_atleta", nombre="dos")], stop="tool_use"),
        reply([text("ok")]),
    )
    session = Session()
    await run(make(settings, llm), session, [])
    results = llm.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


async def test_a_tool_error_is_marked_as_one(settings):
    llm = ScriptedLLM(reply([tool_use("a", "resumen_atleta", nombre="x")], stop="tool_use"), reply([text("No pude.")]))
    box = FakeBox(ToolResult(None, "`nombre` must be text", is_error=True))
    said = []
    await run(make(settings, llm, box), Session(), said)
    assert said == [] and llm.calls[1]["messages"][-1]["content"][0]["is_error"] is True


async def test_a_failure_leaves_the_history_as_it_was(settings):
    session = Session()
    llm = ScriptedLLM(reply([text("Primera.")]), reply([tool_use("a", "resumen_atleta", nombre="x")], stop="tool_use"), RuntimeError("boom"))
    agent = make(settings, llm)
    await run(agent, session, [], "uno")
    before = list(session.messages)

    answer = await run(agent, session, [], "dos")
    assert answer == agent_module.TRY_AGAIN
    assert session.messages == before  # no dangling tool_use, nothing half done


async def test_too_many_steps_is_reported_and_rolled_back(settings):
    loop = [reply([tool_use(f"t{i}", "resumen_atleta", nombre="x")], stop="tool_use") for i in range(settings.max_turns)]
    session = Session()
    answer = await run(make(settings, ScriptedLLM(*loop)), session, [])
    assert answer == agent_module.TOO_MANY_STEPS and session.messages == []


async def test_refusal_and_cut_off(settings):
    assert await run(make(settings, ScriptedLLM(reply([], stop="refusal"))), Session(), []) == agent_module.REFUSED
    cut = await run(make(settings, ScriptedLLM(reply([text("Empecé a decir")], stop="max_tokens"))), Session(), [])
    assert cut.startswith("Empecé a decir") and cut.endswith(agent_module.CUT_OFF)


async def test_an_answer_with_no_text_sends_nothing(settings):
    assert await run(make(settings, ScriptedLLM(reply([]))), Session(), []) is None


async def test_the_audit_records_the_tool_and_its_arguments_but_not_the_result(settings, tmp_path):
    audit = Audit(tmp_path / "audit.log")
    llm = ScriptedLLM(reply([tool_use("a", "resumen_atleta", nombre="ana")], stop="tool_use"), reply([text("ok")]))
    await run(make(settings, llm, audit=audit), Session(), [], user=956)
    line = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert '"tool": "resumen_atleta"' in line and '"user": 956' in line and '"nombre": "ana"' in line
    assert "RESUMEN EN EL CHAT" not in line
    assert oct((tmp_path / "audit.log").stat().st_mode & 0o777) == "0o600"


def test_a_session_resets_clean():
    s = Session(messages=[1, 2], context_tokens=9, turns=3)
    s.reset()
    assert (s.messages, s.context_tokens, s.turns) == ([], 0, 0)


async def test_a_tool_file_goes_to_the_chat_with_its_caption_and_never_to_the_model(settings):
    from duma.tools import OutFile

    llm = ScriptedLLM(reply([tool_use("tu_1", "buscar_atletas")], stop="tool_use"), reply([text("Listo.")]))
    box = FakeBox(ToolResult("51 personas.", "51 resultado(s); mandé el archivo.", files=[OutFile("atletas.csv", b"Nombre\nAna")]))
    sent = []

    async def send(t, files=None, buttons=None):
        sent.append((t, files))

    await make(settings, llm, box).run(Session(), "todos", 10, send)
    assert sent == [("51 personas.", box.result.files)]
    assert "Ana" not in str(llm.calls[1]["messages"][2])


async def test_every_request_marks_the_fixed_part_and_the_conversation_for_caching(settings):
    llm = ScriptedLLM(reply([tool_use("tu_1", "resumen_atleta", nombre="ana")], stop="tool_use"), reply([text("Listo.")]))
    await run(make(settings, llm), Session(), [])
    for call in llm.calls:
        assert call["cache_control"] == {"type": "ephemeral"}
        assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    # The same bytes on both requests, or the second one could not read what the first one wrote.
    assert llm.calls[0]["system"] == llm.calls[1]["system"] and llm.calls[0]["tools"] == llm.calls[1]["tools"]


async def test_the_session_adds_up_what_each_request_spent_even_when_the_turn_fails(settings):
    llm = ScriptedLLM(reply([tool_use("tu_1", "resumen_atleta", nombre="ana")], stop="tool_use"), RuntimeError("boom"))
    session = Session()
    assert await run(make(settings, llm), session, []) == agent_module.TRY_AGAIN
    assert (session.usage.calls, session.usage.input, session.usage.cache_read, session.usage.output) == (1, 1200, 300, 50)
    session.reset()
    assert session.usage.calls == 1


async def test_fixed_tokens_are_counted_once_and_split_between_instructions_and_tools(settings):
    class Counting(ScriptedLLM):
        counted = 0

        async def count(self, **kwargs):
            Counting.counted += 1
            return 8 + (2400 if "system" in kwargs else 0) + (2450 if "tools" in kwargs else 0)

    agent = make(settings, Counting())
    assert await agent.fixed_tokens() == (2400, 2450)
    assert await agent.fixed_tokens() == (2400, 2450) and Counting.counted == 3


async def test_fixed_tokens_are_unknown_when_the_count_fails(settings):
    assert await make(settings, ScriptedLLM()).fixed_tokens() is None  # this LLM has no `count`


async def test_a_proposal_reaches_the_chat_with_its_buttons(settings):
    llm = ScriptedLLM(reply([tool_use("tu_1", "guardar_preferencia", regla="x")], stop="tool_use"), reply([text("Te la dejé para confirmar.")]))
    box = FakeBox(ToolResult("Regla: «x»", "Aún no está guardada.", buttons=[("Guardar", "ok:abc"), ("Cancelar", "no:abc")]))
    sent = []

    async def send(t, files=None, buttons=None):
        sent.append((t, buttons))

    await make(settings, llm, box).run(Session(), "siempre así", 10, send)
    assert sent == [("Regla: «x»", [("Guardar", "ok:abc"), ("Cancelar", "no:abc")])]


async def test_preferences_ride_after_the_cached_instructions_and_only_when_there_are_any(settings):
    rules = [""]
    llm = ScriptedLLM(reply([text("a")]), reply([text("b")]))
    agent = Agent(settings, llm, FakeBox(), "PROMPT", extra_system=lambda: rules[0])
    await run(agent, Session(), [])
    rules[0] = "## Preferencias\n- tablas siempre"
    await run(agent, Session(), [])
    first, second = llm.calls[0]["system"], llm.calls[1]["system"]
    assert len(first) == 1 and len(second) == 2
    # The cached block is byte-identical with and without preferences: saving one does not throw the cache away.
    assert second[0] == first[0] and second[1] == {"type": "text", "text": rules[0]}


async def test_the_answer_gets_its_names_back_but_the_history_keeps_the_codes(settings):
    llm = ScriptedLLM(reply([text("ATLETA_01 va 24/26.")]), reply([text("ok")]))
    session, agent = Session(), make(settings, llm)
    session.names.code(10, "Ana Peña")
    assert await run(agent, session, []) == "Ana Peña va 24/26."
    await run(agent, session, [])
    assert "Ana" not in str(llm.calls[1]["messages"]) and "ATLETA_01" in str(llm.calls[1]["messages"])


async def test_tools_get_the_sessions_own_names(settings):
    llm = ScriptedLLM(reply([tool_use("tu_1", "consultar")], stop="tool_use"), reply([text("ok")]))
    box, session = FakeBox(), Session()
    await run(make(settings, llm, box), session, [])
    assert box.names is session.names


async def test_compaction_replaces_the_history_with_notes_that_open_the_next_request(settings):
    llm = ScriptedLLM(reply([text("Hola.")]), reply([text("- pidió inactivos\n- el actual: ATLETA_01")]), reply([text("Sigo.")]))
    agent, session = make(settings, llm), Session()
    session.names.code(10, "Ana Peña")
    await run(agent, session, [], "¿cuántos inactivos?")

    assert await agent.compact(session) is True
    summary_call = llm.calls[1]
    assert summary_call["messages"][-1]["content"] == agent_module.SUMMARY_REQUEST and len(summary_call["messages"]) == 3
    assert summary_call["tools"] == FakeBox.schemas  # the history has tool calls: the request needs their definitions
    assert session.messages == [] and session.context_tokens == 0 and session.usage.calls == 2
    assert session.names.athlete_id("ATLETA_01") == 10  # the codes in the notes still resolve

    await run(agent, session, [], "¿y los activos?")
    opening = llm.calls[2]["messages"]
    assert len(opening) == 1 and opening[0]["content"].startswith(agent_module.NOTES_HEADER)
    assert "- el actual: ATLETA_01" in opening[0]["content"] and opening[0]["content"].endswith("¿y los activos?")


async def test_the_notes_also_open_a_message_that_came_with_a_file(settings):
    llm = ScriptedLLM(reply([text("ok")]))
    session = Session(notes="- pidió inactivos")
    await make(settings, llm).run(session, [{"type": "image", "source": {}}, {"type": "text", "text": "¿y esto?"}], 10, None)
    blocks = llm.calls[0]["messages"][0]["content"]
    assert blocks[0]["type"] == "text" and "- pidió inactivos" in blocks[0]["text"] and blocks[1]["type"] == "image"


async def test_a_compaction_that_fails_leaves_the_session_as_it_was(settings):
    for bad in (RuntimeError("boom"), reply([tool_use("tu_9", "cifras")], stop="tool_use"), reply([text("")])):
        llm = ScriptedLLM(reply([text("Hola.")]), bad)
        agent, session = make(settings, llm), Session()
        await run(agent, session, [])
        assert await agent.compact(session) is False and len(session.messages) == 2 and session.notes == ""
    assert await make(settings, ScriptedLLM()).compact(Session()) is False  # nothing to compact


async def test_the_history_holds_plain_blocks_with_only_what_the_api_sent(settings):
    import json

    class Block:
        """Stands for an SDK block: it knows how to dump itself, and the dump has only the fields that came."""

        type = "thinking"

        def model_dump(self, **options):
            assert options == {"mode": "json", "exclude_unset": True, "by_alias": True}
            return {"type": "thinking", "thinking": "", "signature": "abc=="}

    llm = ScriptedLLM(reply([Block(), text("Listo.")]))
    session = Session()
    await run(make(settings, llm), session, [])
    assert session.messages[1]["content"] == [{"type": "thinking", "thinking": "", "signature": "abc=="}, {"type": "text", "text": "Listo."}]
    json.dumps(session.messages)  # nothing in the history needs the SDK to be saved


THOUGHT = {"type": "thinking", "thinking": "", "signature": "abc=="}


async def test_thinking_written_under_other_instructions_or_tools_is_dropped_before_the_next_request(settings):
    box = FakeBox()
    box.schemas = [{"name": "resumen_atleta"}]
    session = Session()
    llm = ScriptedLLM(reply([text("Uno.")]), reply([text("Dos.")]), reply([text("Tres.")]), reply([text("Cuatro.")]))
    agent = Agent(settings, llm, box, "PROMPT")
    await run(agent, session, [], "uno")
    session.messages[1]["content"].insert(0, dict(THOUGHT))
    session.messages.append({"role": "user", "content": "solo pienso"})
    session.messages.append({"role": "assistant", "content": [dict(THOUGHT)]})

    # Same instructions and tools: the history goes back exactly as it was written.
    await run(agent, session, [], "dos")
    assert session.messages[1]["content"][0] == THOUGHT and session.messages[3]["content"] == [THOUGHT]

    # A deploy added a tool.
    box.schemas = [{"name": "resumen_atleta"}, {"name": "entrenos_atleta"}]
    await run(agent, session, [], "tres")
    assert session.messages[1]["content"] == [{"type": "text", "text": "Uno."}]
    assert session.messages[3]["content"] == [{"type": "text", "text": "(sin texto)"}]
    assert all(b.get("type") != "thinking" for m in llm.calls[-1]["messages"] if isinstance(m["content"], list) for b in m["content"])

    # The instructions changed.
    session.messages[-1]["content"].insert(0, dict(THOUGHT))
    await run(Agent(settings, llm, box, "OTRO PROMPT"), session, [], "cuatro")
    assert THOUGHT not in session.messages[-3]["content"]


async def test_a_session_saved_before_the_fingerprint_existed_loses_its_thinking_once():
    from duma.sessions import _dump, _load

    session = Session(messages=[{"role": "user", "content": "hola"}], prefix="abc")
    assert _load(_dump(session)).prefix == "abc"
    import json

    old = json.loads(_dump(session))
    del old["prefix"]
    assert _load(json.dumps(old)).prefix == ""
