from datetime import datetime

from duma.budget import Budget


def make(tmp_path, limit=1.0, day="2026-10-06"):
    now = [datetime.fromisoformat(f"{day}T10:00:00")]
    return Budget(tmp_path / "state" / "budget.json", limit, "America/Monterrey", now=lambda: now[0]), now


def test_spend_adds_up_and_survives_a_restart(tmp_path):
    budget, _ = make(tmp_path)
    budget.add(0.4)
    budget.add(0.35)
    again, _ = make(tmp_path)
    assert again.spent() == 0.75 and not again.exhausted()
    again.add(0.25)
    assert again.exhausted()


def test_a_new_day_starts_from_zero(tmp_path):
    budget, now = make(tmp_path)
    budget.add(2.0)
    assert budget.exhausted()
    now[0] = datetime.fromisoformat("2026-10-07T00:01:00")
    assert budget.spent() == 0.0 and not budget.exhausted()
    budget.add(0.1)
    assert budget.spent() == 0.1


def test_a_limit_of_zero_never_stops_and_a_broken_file_counts_as_nothing(tmp_path):
    budget, _ = make(tmp_path, limit=0)
    budget.add(100.0)
    assert not budget.exhausted()
    (tmp_path / "state" / "budget.json").write_text("not json", encoding="utf-8")
    assert budget.spent() == 0.0


def test_the_file_is_private_and_nothing_is_written_for_no_spend(tmp_path):
    budget, _ = make(tmp_path)
    budget.add(0.0)
    assert not (tmp_path / "state" / "budget.json").exists()
    budget.add(0.5)
    assert (tmp_path / "state" / "budget.json").stat().st_mode & 0o777 == 0o600
