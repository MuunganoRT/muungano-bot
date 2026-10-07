from duma import main as main_module
from duma.agent import Session
from duma.audit import Audit
from duma.main import Bot, TOPIC_RETRY_S, _command, _k, topic_link, topic_title
from duma.telegram_api import TelegramError

ADMIN = -1001234567890


class FakeTelegram:
    def __init__(self, topics=False):
        self.sent, self.actions, self.left, self.created, self.forwarded = [], [], [], [], []
        self.documents, self.keyboards, self.popups, self.edits = [], [], [], []
        self.photos = []
        self.albums = []
        self.closed, self.undeletable = [], set()
        self.files = {"voz1": b"OggS fake", "csv1": "Nombre,Grupo\nAna,Maratón\n".encode(), "png1": b"\x89PNG fake"}
        self.attempts, self._topics, self._next = 0, topics, 500
        self.chat_error, self.chat_type, self.forum, self.status, self.can_manage = False, "supergroup", True, "administrator", True

    async def send_message(self, chat_id, text, thread_id=None, reply_markup=None):
        self.sent.append((chat_id, text, thread_id))
        if reply_markup:
            self.keyboards.append([b["callback_data"] for b in reply_markup["inline_keyboard"][0]])

    async def answer_callback_query(self, callback_query_id, text=""):
        self.popups.append(text)

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((message_id, text))

    async def close_forum_topic(self, chat_id, thread_id):
        if thread_id in self.undeletable:
            raise TelegramError("closeForumTopic", 400, "TOPIC_NOT_MODIFIED")
        self.closed.append(thread_id)

    async def download(self, file_id):
        if file_id == "roto":
            raise TelegramError("getFile", 400, "file is too big")
        return self.files[file_id]

    async def set_my_commands(self, commands, chat_id):
        self.commands = (chat_id, [name for name, _ in commands])

    async def send_document(self, chat_id, filename, content, caption="", thread_id=None):
        self.documents.append((chat_id, filename, content, caption, thread_id))

    async def send_photo(self, chat_id, filename, content, caption="", thread_id=None):
        self.photos.append((chat_id, filename, content, caption, thread_id))

    async def send_photos(self, chat_id, photos, caption="", thread_id=None):
        self.albums.append((chat_id, photos, caption, thread_id))

    async def send_chat_action(self, chat_id, thread_id=None, action="typing"):
        self.actions.append((chat_id, thread_id))

    async def leave_chat(self, chat_id):
        self.left.append(chat_id)

    async def get_me(self):
        return {"id": 999, "username": "duma_test_bot"}

    async def get_chat(self, chat_id):
        if self.chat_error:
            raise TelegramError("getChat", 400, "chat not found")
        return {"title": "Admins", "type": self.chat_type, "is_forum": self.forum}

    async def get_chat_member(self, chat_id, user_id):
        return {"status": self.status, "can_manage_topics": self.can_manage}

    async def create_forum_topic(self, chat_id, name):
        self.attempts += 1
        if not self._topics:
            raise TelegramError("createForumTopic", 400, "not enough rights to create a topic")
        self._next += 1
        self.created.append((chat_id, name))
        return self._next

    async def forward_message(self, chat_id, from_chat_id, message_id, thread_id=None):
        self.forwarded.append((message_id, thread_id))


class FakeAgent:
    def __init__(self, answer="respuesta"):
        self.answer, self.calls = answer, []

    async def run(self, session, text, user, send):
        self.calls.append((text, user))
        await send("directo")
        return self.answer

    async def fixed_tokens(self):
        return (2400, 2450)

    compacts = True

    async def compact(self, session):
        if self.compacts:
            session.messages.clear()
            session.context_tokens = 0
            session.notes = "notas"
        return self.compacts


def make(settings, tmp_path, agent=None, topics=False, clock=None):
    tg, agent = FakeTelegram(topics), agent or FakeAgent()
    extra = {"clock": clock} if clock else {}
    return Bot(settings, tg, agent, Audit(tmp_path / "audit.log"), **extra), tg, agent


def msg(text=None, user=10, chat=ADMIN, **extra):
    m = {"chat": {"id": chat, "type": "supergroup"}, "from": {"id": user}, **extra}
    if text is not None:
        m["text"] = text
    return {"update_id": 1, "message": m}


def test_token_counts_are_shortened():
    assert [_k(n) for n in (0, 246, 999, 1000, 2253, 14841, 100_000, 1_250_000)] == ["0", "246", "999", "1k", "2.3k", "14.8k", "100k", "1.2M"]


def test_command_parsing():
    assert _command("/help") == "help"
    assert _command("/Help@DumaBot algo") == "help"
    assert _command("hola /help") is None
    assert _command("/") == ""


async def test_a_message_reaches_the_agent_and_both_the_direct_text_and_the_answer_are_sent(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path)
    await bot.handle(msg("/ruun dame a ana"))
    assert agent.calls == [("dame a ana", 10)]
    assert [t for _, t, _ in tg.sent] == ["directo", "respuesta"]
    assert tg.actions == [(ADMIN, None)]


async def test_topic_messages_are_answered_in_their_topic_with_their_own_session(settings, tmp_path):
    bot, tg, _ = make(settings, tmp_path)
    await bot.handle(msg("hola", is_topic_message=True, message_thread_id=77))
    assert all(thread == 77 for _, _, thread in tg.sent)
    assert (10, 77) in bot._sessions and (10, 0) not in bot._sessions


async def test_messages_written_while_duma_was_down_are_skipped_not_answered(settings, tmp_path, caplog):
    bot, tg, agent = make(settings, tmp_path, topics=True)
    bot._wall_clock = lambda: 10_000.0
    with caplog.at_level("INFO", logger="duma"):
        await bot.handle(msg("/ruun viejo", date=10_000 - settings.stale_after_s - 1))
        await bot.handle(msg("/ruun reciente", date=10_000 - 5, message_id=9))
    assert agent.calls == [("reciente", 10)] and len(tg.created) == 1  # only the recent one opened a topic
    assert "skipped a message" in caplog.text and "viejo" not in caplog.text


async def test_the_general_topic_is_the_main_chat(settings, tmp_path):
    bot, tg, _ = make(settings, tmp_path)
    await bot.handle(msg("/ruun hola", is_topic_message=True, message_thread_id=1))
    assert all(thread is None for _, _, thread in tg.sent) and tg.actions == [(ADMIN, None)]
    assert (10, 0) in bot._sessions and (10, 1) not in bot._sessions


async def test_strangers_dms_and_other_groups_get_no_answer_at_all(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path)
    await bot.handle(msg("hola", user=99))
    await bot.handle(msg("hola", chat=-5))
    await bot.handle({"message": {"chat": {"id": 10, "type": "private"}, "from": {"id": 10}, "text": "hola"}})
    assert tg.sent == [] and agent.calls == []


async def test_an_ignored_message_is_logged_with_its_ids_and_never_its_text(settings, tmp_path, caplog):
    bot, tg, _ = make(settings, tmp_path)
    with caplog.at_level("INFO", logger="duma"):
        await bot.handle(msg("texto privado", user=99))
        await bot.handle(msg("otro texto privado", chat=-5))
    assert "sender is not allowed): chat=-1001234567890 user=99" in caplog.text
    assert "not the admin group): chat=-5 user=10" in caplog.text
    assert "privado" not in caplog.text and tg.sent == []


async def test_added_to_another_group_it_leaves(settings, tmp_path):
    bot, tg, _ = make(settings, tmp_path)
    await bot.handle({"my_chat_member": {"chat": {"id": -555, "type": "group"}, "new_chat_member": {"status": "member"}}})
    assert tg.left == [-555] and tg.sent == []


async def test_a_migration_is_logged_with_the_new_id_and_not_answered(settings, tmp_path, caplog):
    bot, tg, _ = make(settings, tmp_path)
    update = {"message": {"chat": {"id": ADMIN, "type": "group"}, "from": {"id": 10}, "migrate_to_chat_id": -1009876}}
    with caplog.at_level("WARNING", logger="duma"):
        await bot.handle(update)
    assert "-1009876" in caplog.text and tg.sent == []


async def test_commands(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path)
    await bot.handle(msg("/help"))
    await bot.handle(msg("/inventado"))
    assert "cheetah" in tg.sent[0][1] and tg.sent[1][1] == main_module.UNKNOWN_COMMAND
    assert agent.calls == []


async def test_new_clears_only_that_admins_session(settings, tmp_path):
    bot, tg, _ = make(settings, tmp_path)
    mine, other = bot._session(10, 77), bot._session(20, 77)
    mine.messages.append("x")
    other.messages.append("y")
    await bot.handle(msg("/clear", is_topic_message=True, message_thread_id=77))
    assert mine.messages == [] and other.messages == ["y"]


async def test_status_reports_the_context(settings, tmp_path):
    bot, tg, _ = make(settings, tmp_path)
    bot._session(10, 77).context_tokens = 4321
    await bot.handle(msg("/usage", is_topic_message=True, message_thread_id=77))
    assert "4.3k de 100k tokens (4%)" in tg.sent[0][1]


async def test_what_duma_cannot_read_gets_one_short_answer_and_service_messages_none(settings, tmp_path):
    from duma import media

    bot, tg, agent = make(settings, tmp_path)
    in_topic = {"is_topic_message": True, "message_thread_id": 77}
    await bot.handle(msg(video={"file_id": "v"}, **in_topic))
    await bot.handle(msg(document={"file_id": "x", "file_name": "socios.xlsx"}, **in_topic))
    await bot.handle(msg(document={"file_id": "z", "file_name": "respaldo.zip", "mime_type": "application/zip"}, **in_topic))
    await bot.handle(msg(document={"file_id": "p", "file_name": "grande.pdf", "file_size": 50 * 1024 * 1024}, **in_topic))
    await bot.handle(msg(new_chat_members=[{"id": 5}]))
    texts = [t for _, t, _ in tg.sent]
    assert texts[:3] == [media.NO_VIDEO, media.NO_EXCEL, media.UNKNOWN_TYPE] and "10 MB" in texts[3]
    assert len(texts) == 4 and agent.calls == []


async def test_a_session_past_the_limit_is_compacted_before_answering(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path)
    session = bot._session(10, 77)
    session.messages.append("viejo")
    session.context_tokens = settings.session_max_tokens + 1
    await bot.handle(msg("sigue", is_topic_message=True, message_thread_id=77))
    assert [t for _, t, _ in tg.sent] == [main_module.COMPACTING, "directo", "respuesta"]
    assert session.notes == "notas" and agent.calls == [("sigue", 10)]


async def test_when_it_cannot_be_compacted_the_session_starts_over_and_says_so(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path)
    agent.compacts = False
    session = bot._session(10, 77)
    session.messages.append("viejo")
    session.context_tokens = settings.session_max_tokens + 1
    await bot.handle(msg("sigue", is_topic_message=True, message_thread_id=77))
    assert [t for _, t, _ in tg.sent][:2] == [main_module.COMPACTING, main_module.FRESH_SESSION]
    assert session.messages == [] and session.notes == "" and agent.calls == [("sigue", 10)]


async def test_the_audit_never_stores_what_the_admin_wrote(settings, tmp_path):
    bot, _, _ = make(settings, tmp_path)
    await bot.handle(msg("/ruun dato personal que no debe quedar"))
    log = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert '"event": "message"' in log and "dato personal" not in log


async def test_two_messages_of_one_admin_run_one_after_the_other(settings, tmp_path):
    import asyncio

    order = []

    class Slow(FakeAgent):
        async def run(self, session, text, user, send):
            order.append(f"start {text}")
            await asyncio.sleep(0.01)
            order.append(f"end {text}")
            return None

    bot, _, _ = make(settings, tmp_path, Slow())
    await asyncio.gather(bot.handle(msg("/ruun uno")), bot.handle(msg("/ruun dos")))
    assert order in (["start uno", "end uno", "start dos", "end dos"], ["start dos", "end dos", "start uno", "end uno"])


# ── General as the lobby: one topic per request ───────────────────────


def test_topic_title_and_link():
    assert topic_title("dame el resumen de ana") == "dame el resumen de ana"
    assert topic_title("   ") == "Consulta"
    long = topic_title("dame el resumen completo de septiembre de todos los atletas del grupo")
    assert len(long) <= 41 and long.endswith("…") and "  " not in long
    assert topic_link(-1001234567890, 501) == "https://t.me/c/1234567890/501"


async def test_a_request_in_general_opens_a_topic_and_is_answered_there(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path, topics=True)
    await bot.handle(msg("/ruun dame el resumen de ana", message_id=55))

    assert tg.created == [(ADMIN, "dame el resumen de ana")]
    assert tg.forwarded == [(55, 501)]  # the request is copied into the topic
    pointer = tg.sent[0]
    assert pointer[2] is None and "«dame el resumen de ana»" in pointer[1] and "t.me/c/1234567890/501" in pointer[1]
    # the direct text and the answer go to the topic, not to General
    assert [(t, th) for _, t, th in tg.sent[1:]] == [("directo", 501), ("respuesta", 501)]
    assert tg.actions == [(ADMIN, 501)]
    assert (10, 501) in bot._sessions and (10, 0) not in bot._sessions


async def test_the_topic_id_is_logged_without_the_title(settings, tmp_path, caplog):
    bot, _, _ = make(settings, tmp_path, topics=True)
    with caplog.at_level("INFO", logger="duma"):
        await bot.handle(msg("/ruun dato personal en el título", message_id=5))
    assert "opened topic 501 (https://t.me/c/1234567890/501)" in caplog.text
    assert "personal" not in caplog.text


async def test_each_request_in_general_is_its_own_session(settings, tmp_path):
    bot, tg, _ = make(settings, tmp_path, topics=True)
    await bot.handle(msg("/ruun uno", message_id=1))
    await bot.handle(msg("/ruun dos", message_id=2, is_topic_message=True, message_thread_id=1))  # General, as thread 1
    assert [name for _, name in tg.created] == ["uno", "dos"]
    assert {(10, 501), (10, 502)} <= set(bot._sessions)


async def test_a_message_inside_a_topic_continues_that_topic(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path, topics=True)
    await bot.handle(msg("sigue", is_topic_message=True, message_thread_id=77))
    assert tg.created == [] and all(th == 77 for _, _, th in tg.sent) and (10, 77) in bot._sessions


async def test_commands_in_general_do_not_open_topics(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path, topics=True)
    for command in ("/help", "/clear", "/usage", "/inventado"):
        await bot.handle(msg(command))
    assert tg.created == [] and agent.calls == []
    texts = [t for _, t, _ in tg.sent]
    assert "cheetah" in texts[0] and "dentro del tema" in texts[1] and texts[2] == main_module.IN_LOBBY_USAGE


async def test_commands_inside_a_topic_act_on_its_session(settings, tmp_path):
    bot, tg, _ = make(settings, tmp_path, topics=True)
    session = bot._session(10, 77)
    session.messages.append("x")
    session.context_tokens = 4321
    await bot.handle(msg("/usage", is_topic_message=True, message_thread_id=77))
    await bot.handle(msg("/clear", is_topic_message=True, message_thread_id=77))
    assert "4.3k" in tg.sent[0][1] and session.messages == []


async def test_a_file_in_general_without_ruun_gets_the_reminder_and_opens_nothing(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path, topics=True)
    await bot.handle(msg(photo=[{"file_id": "png1"}]))
    assert tg.created == [] and [t for _, t, _ in tg.sent] == [main_module.ASK_WITH_RUN] and agent.calls == []


async def test_without_the_permission_it_answers_in_general_and_waits_before_trying_again(settings, tmp_path, caplog):
    now = [1000.0]
    bot, tg, agent = make(settings, tmp_path, topics=False, clock=lambda: now[0])
    with caplog.at_level("WARNING", logger="duma"):
        await bot.handle(msg("/ruun uno"))
    assert "could not open a topic" in caplog.text
    assert [(t, th) for _, t, th in tg.sent] == [("directo", None), ("respuesta", None)]  # answered in General
    assert (10, 0) in bot._sessions and tg.attempts == 1

    await bot.handle(msg("dos"))  # still within the wait: no new attempt
    assert tg.attempts == 1

    now[0] += TOPIC_RETRY_S + 1
    await bot.handle(msg("/ruun tres"))  # the wait is over
    assert tg.attempts == 2


async def test_while_topics_cannot_be_opened_general_commands_work_on_its_session(settings, tmp_path):
    bot, tg, _ = make(settings, tmp_path, topics=False)
    await bot.handle(msg("/ruun uno"))  # fails once, General becomes the working chat
    bot._session(10, 0).context_tokens = 99
    await bot.handle(msg("/usage"))
    assert "99" in tg.sent[-1][1]


# ── The startup check ─────────────────────────────────────────────────


async def test_the_startup_check_reports_what_duma_can_see(settings, tmp_path, caplog):
    bot, tg, _ = make(settings, tmp_path)
    with caplog.at_level("INFO", logger="duma"):
        await bot.check()
    assert "connected as @duma_test_bot" in caplog.text
    assert "admin group -1001234567890, 2 allowed user(s)" in caplog.text
    assert "'Admins': supergroup, topics on; Duma is administrator" in caplog.text
    assert "WARNING" not in caplog.text and "ERROR" not in caplog.text


async def test_the_startup_check_names_each_problem(settings, tmp_path, caplog):
    bot, tg, _ = make(settings, tmp_path)

    tg.chat_error = True
    with caplog.at_level("INFO", logger="duma"):
        await bot.check()
    assert "TELEGRAM_ADMIN_CHAT_ID is wrong" in caplog.text

    caplog.clear()
    tg.chat_error, tg.can_manage, tg.chat_type = False, False, "group"
    with caplog.at_level("INFO", logger="duma"):
        await bot.check()
    assert "not a supergroup" in caplog.text and "cannot manage topics" in caplog.text

    caplog.clear()
    tg.status = "left"
    with caplog.at_level("INFO", logger="duma"):
        await bot.check()
    assert "not a member of the admin group (status: left)" in caplog.text


async def test_every_update_is_logged_by_kind_and_never_by_content(settings, tmp_path, caplog):
    bot, tg, _ = make(settings, tmp_path)
    with caplog.at_level("INFO", logger="duma"):
        await bot.handle(msg("texto que no debe aparecer", user=99))
    assert "update received: message" in caplog.text and "no debe aparecer" not in caplog.text


# ── The API check ─────────────────────────────────────────────────────


class FakeApi:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    async def get(self, path, *, telegram_user_id, params=None):
        self.calls.append((path, telegram_user_id, params))
        if self.error:
            raise self.error
        return {"success": True, "athletes": [], "total": 0}


async def run_check(settings, tmp_path, caplog, error=None):
    from duma.api_client import ApiError  # noqa: F401  (the tests below build the errors)

    api = FakeApi(error)
    bot = Bot(settings, FakeTelegram(), FakeAgent(), Audit(tmp_path / "audit.log"), api=api)
    caplog.clear()
    with caplog.at_level("INFO", logger="duma"):
        await bot.check()
    return api, caplog.text


async def test_the_startup_check_confirms_a_working_api(settings, tmp_path, caplog):
    api, log_text = await run_check(settings, tmp_path, caplog)
    assert "API ok at http://api.test" in log_text
    assert api.calls == [("/assistant/athletes", 10, {"q": "zz"})]  # as the lowest allowed admin id


async def test_the_startup_check_tells_a_bad_token_from_a_wrong_server_from_a_dead_one(settings, tmp_path, caplog):
    from duma.api_client import ApiError

    _, text = await run_check(settings, tmp_path, caplog, ApiError(401, "The API rejected my token"))
    assert "rejected the token" in text and "ASSISTANT_TOKEN" in text and "Is another server" in text
    _, text = await run_check(settings, tmp_path, caplog, ApiError(404, "Not Found"))
    assert "has no /assistant routes" in text
    _, text = await run_check(settings, tmp_path, caplog, ApiError(0, "I could not reach the API"))
    assert "cannot use the API" in text and "could not reach" in text


async def test_a_file_from_a_tool_is_uploaded_to_the_topic_where_it_was_asked(settings, tmp_path):
    from duma.tools import OutFile

    class Filer(FakeAgent):
        async def run(self, session, text, user, send):
            await send("51 personas.", [OutFile("atletas.csv", b"Nombre")])
            return None

    bot, tg, _ = make(settings, tmp_path, Filer())
    await bot.handle(msg("todos los del grupo", message_thread_id=7, is_topic_message=True))
    assert tg.documents == [(ADMIN, "atletas.csv", b"Nombre", "51 personas.", 7)]
    assert tg.sent == []


async def test_several_pictures_from_a_tool_go_as_one_album(settings, tmp_path):
    from duma.tools import OutFile

    class Drawer(FakeAgent):
        async def run(self, session, text, user, send):
            await send("", [OutFile("vueltas_1.png", b"a", photo=True), OutFile("vueltas_2.png", b"b", photo=True)])
            return None

    bot, tg, _ = make(settings, tmp_path, Drawer())
    await bot.handle(msg("las vueltas en imagen", message_thread_id=7, is_topic_message=True))
    assert tg.albums == [(ADMIN, [("vueltas_1.png", b"a"), ("vueltas_2.png", b"b")], "", 7)]
    assert tg.photos == [] and tg.documents == [] and tg.sent == []


async def test_a_chart_from_a_tool_is_shown_as_a_picture_not_attached(settings, tmp_path):
    from duma.tools import OutFile

    class Drawer(FakeAgent):
        async def run(self, session, text, user, send):
            await send("", [OutFile("km.png", b"\x89PNG", photo=True)])
            return None

    bot, tg, _ = make(settings, tmp_path, Drawer())
    await bot.handle(msg("gráfica de km", message_thread_id=7, is_topic_message=True))
    assert tg.photos == [(ADMIN, "km.png", b"\x89PNG", "", 7)]
    assert tg.documents == [] and tg.sent == []


# ── /ruun: the only way to ask from General ───────────────────────────


async def test_a_plain_message_in_general_gets_the_reminder_once_and_opens_nothing(settings, tmp_path):
    now = [1000.0]
    bot, tg, agent = make(settings, tmp_path, topics=True, clock=lambda: now[0])
    await bot.handle(msg("oigan, ¿quién va al packrun?"))
    await bot.handle(msg("yo voy"))  # admins chatting: no second reminder
    await bot.handle(msg("yo también", user=20))  # another admin gets their own
    assert [t for _, t, _ in tg.sent] == [main_module.ASK_WITH_RUN, main_module.ASK_WITH_RUN]
    assert tg.created == [] and agent.calls == [] and bot._sessions == {}

    now[0] += main_module.HINT_EVERY_S + 1
    await bot.handle(msg("¿cuántos inactivos hay?"))
    assert len(tg.sent) == 3


async def test_ruun_without_a_question_asks_for_one(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path, topics=True)
    await bot.handle(msg("/ruun"))
    await bot.handle(msg("/ruun@DumaBot   "))
    assert [t for _, t, _ in tg.sent] == [main_module.RUN_NEEDS_TEXT] * 2 and tg.created == [] and agent.calls == []


async def test_ruun_inside_a_topic_asks_there_and_a_plain_message_still_works(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path, topics=True)
    await bot.handle(msg("/ruun@DumaBot y los de maratón", is_topic_message=True, message_thread_id=77))
    await bot.handle(msg("¿y en agosto?", is_topic_message=True, message_thread_id=77))
    assert agent.calls == [("y los de maratón", 10), ("¿y en agosto?", 10)]
    assert tg.created == [] and all(th == 77 for _, _, th in tg.sent)


# ── /usage ────────────────────────────────────────────────────────────


async def test_usage_in_a_topic_shows_its_cost_and_what_fills_the_context(settings, tmp_path):
    from duma.usage import Usage

    bot, tg, _ = make(settings, tmp_path, topics=True)
    session = bot._session(10, 77)
    session.context_tokens = 6000
    session.usage = Usage(input=950, cache_write=5000, cache_read=10000, output=400, calls=4)
    await bot.handle(msg("/usage", is_topic_message=True, message_thread_id=77))
    assert tg.sent == [
        (
            ADMIN,
            "$0.0204 USD en 4 llamada(s) al modelo\n"
            "Tokens:\n"
            "· Entrada: 950\n"
            "· Caché escrita: 5k\n"
            "· Caché leída: 10k\n"
            "· Salida: 400\n"
            "Contexto ahora: 6k de 100k tokens (6%)\n"
            "· Instrucciones: 2.4k\n"
            "· Herramientas: 2.5k\n"
            "· Conversación: 1.1k",
            77,
        )
    ]


async def test_usage_before_any_question_and_estado_is_gone(settings, tmp_path):
    bot, tg, _ = make(settings, tmp_path, topics=True)
    await bot.handle(msg("/usage", is_topic_message=True, message_thread_id=77))
    assert "$0.0000 USD en 0 llamada(s)" in tg.sent[0][1] and "Contexto: vacío" in tg.sent[0][1]
    await bot.handle(msg("/estado", is_topic_message=True, message_thread_id=77))
    assert tg.sent[1][1] == main_module.UNKNOWN_COMMAND


async def test_a_new_session_keeps_what_the_topic_already_cost(settings, tmp_path):
    from duma.usage import Usage

    bot, tg, _ = make(settings, tmp_path)
    session = bot._session(10, 77)
    session.usage = Usage(output=1000, calls=1)
    await bot.handle(msg("/clear", is_topic_message=True, message_thread_id=77))
    assert session.usage.output == 1000


async def test_the_startup_registers_the_command_list_for_the_admin_group_only(settings, tmp_path):
    bot, tg, _ = make(settings, tmp_path)
    await bot.check()
    assert tg.commands == (ADMIN, ["ruun", "usage", "clear", "prefs", "forget", "help"])


# ── The daily budget ──────────────────────────────────────────────────


class Spender(FakeAgent):
    """Each question costs 100k output tokens: $1.00 at this model's price."""

    async def run(self, session, text, user, send):
        from types import SimpleNamespace as NS

        session.usage.add(NS(usage=NS(input_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0, output_tokens=100_000)))
        return await super().run(session, text, user, send)


def with_budget(settings, tmp_path, limit):
    from duma.budget import Budget

    tg, agent = FakeTelegram(topics=True), Spender()
    budget = Budget(tmp_path / "budget.json", limit, settings.tz)
    return Bot(settings, tg, agent, Audit(tmp_path / "audit.log"), budget=budget), tg, agent, budget


async def test_the_answer_that_reaches_the_budget_says_so_and_the_next_question_is_refused(settings, tmp_path):
    bot, tg, agent, budget = with_budget(settings, tmp_path, limit=2.0)
    await bot.handle(msg("/ruun uno", message_id=1))
    assert budget.spent() == 1.0 and main_module.BUDGET_REACHED.format(limit=2.0) not in [t for _, t, _ in tg.sent]

    await bot.handle(msg("y dos", is_topic_message=True, message_thread_id=501))
    assert tg.sent[-1] == (ADMIN, main_module.BUDGET_REACHED.format(limit=2.0), 501)

    opened = len(tg.created)
    await bot.handle(msg("/ruun tres", message_id=3))  # refused in General, and no topic is opened for it
    await bot.handle(msg("y cuatro", is_topic_message=True, message_thread_id=501))
    refusal = main_module.OVER_BUDGET.format(limit=2.0)
    assert tg.sent[-2:] == [(ADMIN, refusal, None), (ADMIN, refusal, 501)]
    assert len(agent.calls) == 2 and len(tg.created) == opened and budget.spent() == 2.0


async def test_commands_still_work_over_budget(settings, tmp_path):
    bot, tg, _, budget = with_budget(settings, tmp_path, limit=1.0)
    budget.add(5.0)
    await bot.handle(msg("/help"))
    await bot.handle(msg("/usage", is_topic_message=True, message_thread_id=77))
    assert "cheetah" in tg.sent[0][1] and "llamada(s) al modelo" in tg.sent[1][1]


async def test_the_startup_check_says_what_the_budget_is_or_why_it_cannot_work(settings, tmp_path, caplog):
    import dataclasses

    bot, _, _, budget = with_budget(settings, tmp_path, limit=5.0)
    budget.add(0.25)
    with caplog.at_level("INFO", logger="duma"):
        bot._check_budget()
        bot._s = dataclasses.replace(settings, model="some-future-model")
        bot._check_budget()
        budget.limit_usd = 0
        bot._check_budget()
    assert "daily budget: $5.00, spent today $0.2500" in caplog.text
    assert "no price for model some-future-model" in caplog.text and "no daily budget" in caplog.text


# ── Confirmations and standing preferences ────────────────────────────


async def with_preferences(settings, tmp_path):
    from duma.confirmations import Confirmations
    from duma.database import SqliteDatabase
    from duma.preferences import Preferences

    tg = FakeTelegram(topics=True)
    store = await Confirmations.open(SqliteDatabase(tmp_path / "bot.sqlite"), 60)
    prefs = Preferences(tmp_path / "preferencias.md", settings.tz)
    bot = Bot(settings, tg, FakeAgent(), Audit(tmp_path / "audit.log"), confirmations=store, preferences=prefs)
    return bot, tg, store, prefs


def click(data, user=10, message_id=900):
    return {
        "update_id": 2,
        "callback_query": {"id": "cq1", "from": {"id": user}, "data": data, "message": {"message_id": message_id, "chat": {"id": ADMIN, "type": "supergroup"}}},
    }


async def test_saving_a_preference_happens_on_the_click_and_the_message_loses_its_buttons(settings, tmp_path):
    bot, tg, store, prefs = await with_preferences(settings, tmp_path)
    action = await store.propose(10, "preference_add", {"rule": "Siempre en tabla"}, "Regla: «Siempre en tabla»")

    await bot.handle(click(f"ok:{action}", user=20))  # another allowed admin
    assert tg.popups == [main_module.NOT_YOURS] and prefs.rules() == [] and tg.edits == []

    await bot.handle(click(f"ok:{action}"))
    assert prefs.rules() == ["Siempre en tabla"]
    assert tg.edits == [(900, "Regla: «Siempre en tabla»\n\nGuardada. Aplica desde el siguiente mensaje.")]

    await bot.handle(click(f"ok:{action}"))  # the double click
    assert prefs.rules() == ["Siempre en tabla"] and tg.popups[-1] == main_module.NO_LONGER_VALID and len(tg.edits) == 1
    log = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert log.count('"event": "confirmation"') == 2 and "Siempre en tabla" not in log


async def test_cancel_and_an_expired_proposal_save_nothing(settings, tmp_path):
    from duma.confirmations import Confirmations
    from duma.database import SqliteDatabase

    bot, tg, store, prefs = await with_preferences(settings, tmp_path)
    action = await store.propose(10, "preference_add", {"rule": "Siempre en tabla"}, "Regla")
    await bot.handle(click(f"no:{action}"))
    assert prefs.rules() == [] and tg.edits == [(900, "Regla\n\nCancelado.")]

    now = [1000.0]
    bot._confirmations = await Confirmations.open(SqliteDatabase(tmp_path / "otro.sqlite"), 60, clock=lambda: now[0])
    late = await bot._confirmations.propose(10, "preference_add", {"rule": "Siempre en tabla"}, "Regla")
    now[0] += 61
    await bot.handle(click(f"ok:{late}"))
    assert prefs.rules() == [] and tg.edits[-1] == (900, f"Regla\n\n{main_module.EXPIRED}")


async def test_a_click_that_is_not_a_proposal_or_comes_from_a_stranger_does_nothing(settings, tmp_path):
    bot, tg, store, prefs = await with_preferences(settings, tmp_path)
    action = await store.propose(10, "preference_add", {"rule": "Siempre en tabla"}, "Regla")
    await bot.handle(click("borrar:todo"))
    await bot.handle(click(f"ok:{action}", user=99))  # not on the allow-list: dropped before it gets here
    assert prefs.rules() == [] and tg.edits == [] and tg.popups == [""]


async def test_prefs_lists_and_forget_asks_before_removing(settings, tmp_path):
    bot, tg, store, prefs = await with_preferences(settings, tmp_path)
    await bot.handle(msg("/prefs"))
    await bot.handle(msg("/forget 1"))
    assert [t for _, t, _ in tg.sent] == [main_module.NO_PREFS, main_module.NO_PREFS]

    prefs.add(10, "Siempre en tabla")
    prefs.add(10, "Primero el score")
    await bot.handle(msg("/prefs"))
    assert tg.sent[-1][1] == "1. Siempre en tabla\n2. Primero el score"
    for bad in ("/forget", "/forget 3", "/forget dos"):
        await bot.handle(msg(bad))
        assert tg.sent[-1][1] == main_module.FORGET_NEEDS_NUMBER

    await bot.handle(msg("/forget 2"))
    assert tg.sent[-1][1] == "Voy a olvidar esta regla permanente:\n«Primero el score»" and len(prefs.rules()) == 2
    await bot.handle(click(tg.keyboards[-1][0]))
    assert prefs.rules() == ["Siempre en tabla"] and tg.edits[-1][1].endswith("Olvidada.")


async def test_a_proposal_from_a_tool_is_sent_with_its_keyboard(settings, tmp_path):
    class Proposer(FakeAgent):
        async def run(self, session, text, user, send):
            await send("Regla: «x»", buttons=[("Guardar", "ok:abc"), ("Cancelar", "no:abc")])
            return None

    bot, tg, _ = make(settings, tmp_path, Proposer())
    await bot.handle(msg("siempre así", is_topic_message=True, message_thread_id=77))
    assert tg.sent == [(ADMIN, "Regla: «x»", 77)] and tg.keyboards == [["ok:abc", "no:abc"]]


async def test_confirming_a_replacement_swaps_the_old_rule_for_the_new_one(settings, tmp_path):
    bot, tg, store, prefs = await with_preferences(settings, tmp_path)
    prefs.add(10, "Las listas siempre en tabla")
    prefs.add(10, "Primero el score")
    payload = {"rule": "Las listas en tabla, salvo las cortas", "replace": [prefs.lines()[0]]}
    action = await store.propose(10, "preference_add", payload, "Regla")
    await bot.handle(click(f"ok:{action}"))
    assert prefs.rules() == ["Primero el score", "Las listas en tabla, salvo las cortas"]


# ── Files an admin sends ──────────────────────────────────────────────


async def test_a_csv_with_its_caption_reaches_the_model_as_text_and_the_audit_keeps_neither(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path)
    document = {"file_id": "csv1", "file_name": "socios de ana.csv", "mime_type": "text/csv", "file_size": 26}
    await bot.handle(msg(caption="¿quién falta aquí?", document=document, is_topic_message=True, message_thread_id=77))
    content, user = agent.calls[0]
    assert user == 10 and content == [
        {"type": "text", "text": "Archivo «socios de ana.csv»:\n\nNombre,Grupo\nAna,Maratón\n"},
        {"type": "text", "text": "¿quién falta aquí?"},
    ]
    log = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert '"file": "text"' in log and '"bytes": 26' in log and "Ana" not in log and "socios" not in log


async def test_a_photo_without_text_gets_a_default_question(settings, tmp_path):
    from duma import media

    bot, tg, agent = make(settings, tmp_path)
    photo = [{"file_id": "chica", "file_size": 10}, {"file_id": "png1", "file_size": 900}]
    await bot.handle(msg(photo=photo, is_topic_message=True, message_thread_id=77))
    image, question = agent.calls[0][0]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg" and image["source"]["data"]
    assert question == {"type": "text", "text": media.DEFAULT_QUESTION}


async def test_ruun_with_a_file_opens_the_topic_named_after_the_question_or_the_file(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path, topics=True)
    document = {"file_id": "csv1", "file_name": "pagos.csv", "mime_type": "text/csv"}
    await bot.handle(msg(caption="/ruun revisa estos pagos", document=document, message_id=1))
    await bot.handle(msg(caption="/ruun", document=document, message_id=2))
    assert [name for _, name in tg.created] == ["revisa estos pagos", "pagos.csv"]
    assert agent.calls[0][0][1]["text"] == "revisa estos pagos" and len(agent.calls) == 2


async def test_a_file_that_cannot_be_downloaded_says_so_and_asks_nothing(settings, tmp_path):
    bot, tg, agent = make(settings, tmp_path)
    document = {"file_id": "roto", "file_name": "a.csv", "mime_type": "text/csv"}
    await bot.handle(msg(document=document, is_topic_message=True, message_thread_id=77))
    assert [t for _, t, _ in tg.sent] == [main_module.DOWNLOAD_FAILED] and agent.calls == []


# ── Sessions on disk and idle topics ──────────────────────────────────


class Talker(FakeAgent):
    async def run(self, session, text, user, send):
        session.messages.append({"role": "user", "content": text})
        session.context_tokens += 100
        return f"{len(session.messages)} mensaje(s)"


async def with_store(settings, tmp_path, now, idle_hours=24):
    import dataclasses

    from duma.database import SqliteDatabase
    from duma.sessions import SessionStore

    tg = FakeTelegram(topics=True)
    store = await SessionStore.open(SqliteDatabase(tmp_path / "bot.sqlite"), clock=lambda: now[0])
    settings = dataclasses.replace(settings, session_idle_hours=idle_hours)
    bot = Bot(settings, tg, Talker(), Audit(tmp_path / "audit.log"), clock=lambda: now[0], wall_clock=lambda: now[0], store=store)
    return bot, tg, store


async def test_a_restart_keeps_the_conversation_and_what_it_cost(settings, tmp_path):
    now = [1000.0]
    bot, tg, _ = await with_store(settings, tmp_path, now)
    in_topic = {"is_topic_message": True, "message_thread_id": 77, "date": 1000}
    await bot.handle(msg("uno", **in_topic))
    await bot.handle(msg("dos", **in_topic))

    again, tg2, _ = await with_store(settings, tmp_path, now)  # a new process over the same file
    await again.handle(msg("tres", **in_topic))
    assert tg2.sent[-1][1] == "3 mensaje(s)" and again._session(10, 77).context_tokens == 300


async def test_clear_is_saved_too(settings, tmp_path):
    now = [1000.0]
    bot, _, _ = await with_store(settings, tmp_path, now)
    in_topic = {"is_topic_message": True, "message_thread_id": 77, "date": 1000}
    await bot.handle(msg("uno", **in_topic))
    await bot.handle(msg("/clear", **in_topic))
    again, _, _ = await with_store(settings, tmp_path, now)
    assert (await again._open(10, 77)).messages == []


async def test_an_idle_topic_is_closed_and_forgotten_and_a_busy_one_is_left_alone(settings, tmp_path, caplog):
    now = [1000.0]
    bot, tg, store = await with_store(settings, tmp_path, now, idle_hours=24)
    for thread in (77, 78, 79):
        await bot.handle(msg("hola", is_topic_message=True, message_thread_id=thread, date=now[0]))
    await bot.sweep()
    assert tg.closed == []

    now[0] += 23 * 3600
    await bot.handle(msg("sigo aquí", is_topic_message=True, message_thread_id=78, date=now[0]))
    now[0] += 2 * 3600  # 77 and 79 are 25 h old, 78 only 2 h
    tg.undeletable.add(79)  # deleted by hand in Telegram
    with caplog.at_level("INFO", logger="duma"):
        await bot.sweep()
    assert tg.closed == [77] and (ADMIN, main_module.CLOSING_IDLE, 77) in tg.sent
    assert await store.load(10, 77) is None and await store.load(10, 79) is None and await store.load(10, 78) is not None
    assert (10, 77) not in bot._sessions and (10, 78) in bot._sessions
    assert "closed idle topic 77" in caplog.text and "could not close idle topic 79" in caplog.text


async def test_the_sweep_runs_at_most_every_ten_minutes_and_never_with_zero_hours(settings, tmp_path):
    now = [1000.0]
    bot, tg, store = await with_store(settings, tmp_path, now, idle_hours=1)
    await bot.sweep()  # nothing to do, and the next one is ten minutes away
    now[0] -= 7200
    await store.save(10, 77, Session())  # a session last touched two hours ago
    now[0] += 7200 + main_module.SWEEP_EVERY_S - 1
    await bot.sweep()
    assert tg.closed == []
    now[0] += 2
    await bot.sweep()
    assert tg.closed == [77]

    never, tg2, store2 = await with_store(settings, tmp_path / "otro", [1000.0], idle_hours=0)
    await store2.save(10, 5, Session())
    never._clock = lambda: 10**9
    await never.sweep()
    assert tg2.closed == [] and await store2.load(10, 5) is not None


async def test_generals_own_session_is_forgotten_without_closing_anything(settings, tmp_path):
    now = [1000.0]
    bot, tg, store = await with_store(settings, tmp_path, now, idle_hours=1)
    await store.save(10, 0, Session())
    now[0] += 7200
    await bot.sweep()
    assert await store.load(10, 0) is None and tg.closed == [] and tg.sent == []


# ── Voice notes ───────────────────────────────────────────────────────


class FakeTranscriber:
    def __init__(self, text="¿cuántos inactivos hay?", fail=False, ready=True):
        self.text, self.fail, self.heard, self.ready = text, fail, [], ready

    async def prepare(self):
        self.ready = True
        return True

    async def transcribe(self, audio):
        from duma.voice import NotReady, VoiceError

        if not self.ready:
            raise NotReady("downloading")
        self.heard.append(audio)
        if self.fail:
            raise VoiceError("exited with 1")
        return self.text


def with_voice(settings, tmp_path, transcriber):
    tg, agent = FakeTelegram(topics=True), FakeAgent()
    return Bot(settings, tg, agent, Audit(tmp_path / "audit.log"), transcriber=transcriber), tg, agent


VOICE = {"voice": {"file_id": "voz1", "duration": 4, "file_size": 9}, "is_topic_message": True, "message_thread_id": 77}


async def test_a_voice_note_is_shown_as_understood_and_then_asked_as_text(settings, tmp_path):
    from duma import media

    ears = FakeTranscriber()
    bot, tg, agent = with_voice(settings, tmp_path, ears)
    await bot.handle(msg(**VOICE))
    assert ears.heard == [b"OggS fake"] and agent.calls == [("¿cuántos inactivos hay?", 10)]
    assert [t for _, t, _ in tg.sent] == [media.HEARD.format(text="¿cuántos inactivos hay?"), "directo", "respuesta"]
    log = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert '"file": "voice"' in log and '"seconds": 4' in log and "inactivos" not in log


async def test_a_voice_note_that_cannot_be_understood_says_so_and_asks_nothing(settings, tmp_path, caplog):
    from duma import media

    for ears in (FakeTranscriber(fail=True), FakeTranscriber(text="")):
        bot, tg, agent = with_voice(settings, tmp_path, ears)
        with caplog.at_level("WARNING", logger="duma"):
            await bot.handle(msg(**VOICE))
        assert [t for _, t, _ in tg.sent] == [media.NOT_UNDERSTOOD] and agent.calls == []
    assert "could not transcribe a voice note" in caplog.text


async def test_without_the_speech_model_a_voice_note_gets_an_honest_answer(settings, tmp_path):
    from duma import media

    bot, tg, agent = with_voice(settings, tmp_path, None)
    await bot.handle(msg(**VOICE))
    assert [t for _, t, _ in tg.sent] == [media.NO_VOICE_HERE] and agent.calls == []


async def test_a_voice_note_in_general_opens_nothing_and_is_not_transcribed(settings, tmp_path):
    ears = FakeTranscriber()
    bot, tg, agent = with_voice(settings, tmp_path, ears)
    await bot.handle(msg(voice={"file_id": "voz1", "duration": 4}))
    assert [t for _, t, _ in tg.sent] == [main_module.ASK_WITH_RUN] and ears.heard == [] and tg.created == []


async def test_while_the_speech_model_downloads_a_voice_note_is_told_to_wait(settings, tmp_path):
    from duma import media

    ears = FakeTranscriber(ready=False)
    bot, tg, agent = with_voice(settings, tmp_path, ears)
    await bot.handle(msg(**VOICE))
    assert [t for _, t, _ in tg.sent] == [media.VOICE_NOT_READY] and agent.calls == []

    await bot.check()  # the startup fetches the model in the background
    await bot._preparing
    await bot.handle(msg(**VOICE))
    assert agent.calls == [("¿cuántos inactivos hay?", 10)]


def choose(index, user=10, asked=10, thread=7):
    labels = ["42k MTY, los 5 grupos", "Berlin 4:00hr"]
    return {
        "update_id": 3,
        "callback_query": {
            "id": "cq2",
            "from": {"id": user},
            "data": f"q:{asked}:{index}",
            "message": {
                "message_id": 901,
                "chat": {"id": ADMIN, "type": "supergroup"},
                "text": "¿Cuál grupo?",
                "message_thread_id": thread,
                "is_topic_message": True,
                "reply_markup": {
                    "inline_keyboard": [[{"text": label, "callback_data": f"q:{asked}:{i}"}] for i, label in enumerate(labels)]
                },
            },
        },
    }


async def test_clicking_an_option_is_the_same_as_typing_it_in_that_topic(settings, tmp_path):
    agent = FakeAgent()
    bot, tg, _ = make(settings, tmp_path, agent)
    await bot.handle(choose(1))
    assert agent.calls == [("Berlin 4:00hr", 10)]
    # The question keeps the choice and loses its buttons, so it cannot be answered twice.
    assert tg.edits == [(901, "¿Cuál grupo?\n\n→ Berlin 4:00hr")]
    assert tg.sent and all(thread == 7 for _, _, thread in tg.sent)


async def test_only_who_was_asked_can_answer_and_a_stale_button_does_nothing(settings, tmp_path):
    agent = FakeAgent()
    bot, tg, _ = make(settings, tmp_path, agent)
    await bot.handle(choose(0, user=20))  # another allowed admin
    assert tg.popups == [main_module.NOT_YOURS] and agent.calls == [] and tg.edits == []
    await bot.handle(choose(5))  # an option that is not on the message
    assert agent.calls == [] and tg.edits == []


def test_the_options_of_a_question_go_one_per_row_and_a_confirmation_side_by_side():
    from duma.tools import choice_buttons

    rows = main_module._keyboard(choice_buttons(["Uno", "Dos", "Tres"], 10))["inline_keyboard"]
    assert [len(row) for row in rows] == [1, 1, 1] and rows[2][0] == {"text": "Tres", "callback_data": "q:10:2"}
    assert [len(row) for row in main_module._keyboard([("Guardar", "ok:a"), ("Cancelar", "no:a")])["inline_keyboard"]] == [2]
