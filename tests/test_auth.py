from duma.auth import decide

ADMIN = -1001234567890


def message(chat=ADMIN, user=10, **extra):
    return {"update_id": 1, "message": {"chat": {"id": chat, "type": "supergroup"}, "from": {"id": user}, "text": "hola", **extra}}


def test_allowed_user_in_the_admin_group_is_handled(settings):
    assert decide(message(), settings).action == "handle"


def test_an_allowed_user_in_another_group_is_ignored(settings):
    assert decide(message(chat=-999), settings).action == "ignore"


def test_a_stranger_in_the_admin_group_is_ignored(settings):
    assert decide(message(user=99), settings).action == "ignore"


def test_a_private_message_is_ignored_even_from_an_allowed_user(settings):
    update = {"message": {"chat": {"id": 10, "type": "private"}, "from": {"id": 10}, "text": "hola"}}
    assert decide(update, settings).action == "ignore"


def test_bots_are_ignored(settings):
    update = message()
    update["message"]["from"]["is_bot"] = True
    assert decide(update, settings).action == "ignore"


def test_being_added_to_another_group_means_leaving_it(settings):
    update = {"my_chat_member": {"chat": {"id": -555, "type": "group"}, "new_chat_member": {"status": "member"}}}
    d = decide(update, settings)
    assert d.action == "leave" and d.chat_id == -555


def test_membership_changes_in_the_admin_group_or_removals_are_ignored(settings):
    in_admin = {"my_chat_member": {"chat": {"id": ADMIN, "type": "supergroup"}, "new_chat_member": {"status": "member"}}}
    kicked = {"my_chat_member": {"chat": {"id": -555, "type": "group"}, "new_chat_member": {"status": "kicked"}}}
    assert decide(in_admin, settings).action == "ignore"
    assert decide(kicked, settings).action == "ignore"


def test_the_group_migrating_is_reported_with_the_new_id(settings):
    update = {"message": {"chat": {"id": ADMIN, "type": "group"}, "from": {"id": 10}, "migrate_to_chat_id": -1009876}}
    d = decide(update, settings)
    assert d.action == "migrated" and d.chat_id == -1009876


def test_callbacks_follow_the_same_two_checks(settings):
    ok = {"callback_query": {"from": {"id": 10}, "message": {"chat": {"id": ADMIN}}}}
    stranger = {"callback_query": {"from": {"id": 99}, "message": {"chat": {"id": ADMIN}}}}
    elsewhere = {"callback_query": {"from": {"id": 10}, "message": {"chat": {"id": -1}}}}
    assert decide(ok, settings).action == "handle"
    assert decide(stranger, settings).action == "ignore"
    assert decide(elsewhere, settings).action == "ignore"


def test_other_updates_are_ignored(settings):
    assert decide({"edited_message": {}}, settings).action == "ignore"
    assert decide({}, settings).action == "ignore"
