"""スケジュール定義の読み込みと対象日判定のテスト."""

from __future__ import annotations

import tomllib
from datetime import date, time, timedelta
from pathlib import Path

import jpholiday
import pytest

from app import ConfigError, ScheduleEntry, entry_matches, load_schedules, parse_schedules, upcoming

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_SCHEDULE = REPO_ROOT / "schedules.toml"

DEFAULTS = {"default_message": "テストメッセージ", "default_to": ("+819000000000",)}

# 検証に使う実在の日付
MON_PLAIN = date(2026, 5, 11)  # 月曜 (平日)
WED_SUBSTITUTE = date(2026, 5, 6)  # 水曜 / 振替休日
THU_HOLIDAY = date(2026, 1, 1)  # 木曜 / 元日
SAT_PLAIN = date(2026, 5, 2)  # 土曜 (祝日ではない)
SUN_HOLIDAY = date(2026, 5, 3)  # 日曜 / 憲法記念日
SUN_PLAIN = date(2026, 5, 10)  # 日曜 (祝日ではない)


def load_bundled() -> list[ScheduleEntry]:
    return load_schedules(BUNDLED_SCHEDULE, **DEFAULTS)


def times_on(entries: list[ScheduleEntry], d: date) -> set[time]:
    return {e.at for e in entries if e.enabled and entry_matches(e, d)}


def parse(toml_text: str) -> list[ScheduleEntry]:
    return parse_schedules(tomllib.loads(toml_text), **DEFAULTS)


# --- 同梱の schedules.toml (平日 7:00 / 土日祝 11:00 / 土曜 9:00 追加) --------


def test_bundled_file_loads():
    entries = load_bundled()
    assert [e.name for e in entries] == [
        "weekday-morning",
        "weekend-and-holiday",
        "saturday-extra",
    ]
    # to を書いていないエントリは TWILIO_TO_NUMBER 由来の既定値を使う
    assert all(e.to == ("+819000000000",) for e in entries)
    assert all(e.message == "テストメッセージ" for e in entries)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (MON_PLAIN, {time(7, 0)}),  # 平日
        (date(2026, 5, 15), {time(7, 0)}),  # 金曜
        (SAT_PLAIN, {time(9, 0), time(11, 0)}),  # 土曜は 2 回
        (SUN_PLAIN, {time(11, 0)}),  # 日曜
        (THU_HOLIDAY, {time(11, 0)}),  # 平日だが祝日 -> 7:00 は鳴らさない
        (WED_SUBSTITUTE, {time(11, 0)}),  # 振替休日も祝日扱い
        (SUN_HOLIDAY, {time(11, 0)}),  # 日曜かつ祝日でも 1 回だけ
    ],
)
def test_bundled_schedule_times(day: date, expected: set[time]):
    assert times_on(load_bundled(), day) == expected


def test_saturday_that_is_a_holiday_still_rings_twice():
    """土曜が祝日でも 9:00 と 11:00 の 2 回。"""
    saturday_holiday = next(
        d
        for d in (date(2026, 1, 1) + timedelta(days=i) for i in range(800))
        if d.weekday() == 5 and jpholiday.is_holiday(d)
    )
    assert times_on(load_bundled(), saturday_holiday) == {time(9, 0), time(11, 0)}


def test_upcoming_lists_saturday_twice():
    entries = load_bundled()
    plan = upcoming(entries, SAT_PLAIN, days=1)
    assert [(d, e.at) for d, e in plan] == [(SAT_PLAIN, time(9, 0)), (SAT_PLAIN, time(11, 0))]


# --- days / holidays の解釈 --------------------------------------------------


def test_days_omitted_matches_every_day():
    (entry,) = parse('[[schedule]]\ntime = "08:00"\n')
    assert all(entry_matches(entry, d) for d in (MON_PLAIN, SAT_PLAIN, SUN_HOLIDAY))


def test_day_aliases_expand():
    (weekday,) = parse('[[schedule]]\ntime = "08:00"\ndays = ["weekday"]\n')
    assert entry_matches(weekday, MON_PLAIN)
    assert not entry_matches(weekday, SAT_PLAIN)

    (weekend,) = parse('[[schedule]]\ntime = "08:00"\ndays = ["weekend"]\n')
    assert entry_matches(weekend, SAT_PLAIN)
    assert entry_matches(weekend, SUN_PLAIN)
    assert not entry_matches(weekend, MON_PLAIN)

    (every,) = parse('[[schedule]]\ntime = "08:00"\ndays = ["all"]\n')
    assert entry_matches(every, MON_PLAIN) and entry_matches(every, SUN_PLAIN)


def test_holidays_only_ignores_weekday_match():
    (entry,) = parse('[[schedule]]\ntime = "08:00"\nholidays = "only"\n')
    assert entry_matches(entry, THU_HOLIDAY)
    assert entry_matches(entry, WED_SUBSTITUTE)
    assert not entry_matches(entry, MON_PLAIN)
    assert not entry_matches(entry, SAT_PLAIN)  # 土曜でも祝日でなければ対象外


def test_holidays_exclude_filters_matched_days():
    (entry,) = parse('[[schedule]]\ntime = "08:00"\ndays = ["weekend"]\nholidays = "exclude"\n')
    assert entry_matches(entry, SAT_PLAIN)
    assert not entry_matches(entry, SUN_HOLIDAY)


def test_days_accepts_plain_string_and_toml_local_time():
    (entry,) = parse("[[schedule]]\ntime = 07:30:00\ndays = \"mon\"\n")
    assert entry.at == time(7, 30)
    assert entry.weekdays == frozenset({0})


# --- エントリ単位の上書き ----------------------------------------------------


def test_per_entry_message_and_multiple_destinations():
    (entry,) = parse(
        """
        [[schedule]]
        time = "20:00"
        message = "ゴミ出しの日です。"
        to = ["+819012345678", "+819087654321", "+819012345678"]
        """
    )
    assert entry.message == "ゴミ出しの日です。"
    assert entry.to == ("+819012345678", "+819087654321")  # 重複は除去され順序は保たれる


def test_disabled_entry_is_kept_but_marked():
    (entry,) = parse('[[schedule]]\ntime = "08:00"\nenabled = false\n')
    assert entry.enabled is False


def test_name_defaults_to_time():
    (entry,) = parse('[[schedule]]\ntime = "08:05"\n')
    assert entry.name == "schedule-0805"


# --- 不正な設定 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("toml_text", "expected_message"),
    [
        ('[[schedule]]\ndays = ["mon"]\n', "time は必須"),
        ('[[schedule]]\ntime = "25:00"\n', "time を解釈できません"),
        ('[[schedule]]\ntime = "7am"\n', "time を解釈できません"),
        ('[[schedule]]\ntime = "08:00"\ndays = ["monday"]\n', "days の値"),
        ('[[schedule]]\ntime = "08:00"\ndays = []\n', "days が空です"),
        ('[[schedule]]\ntime = "08:00"\nholidays = "skip"\n', "holidays は"),
        (
            '[[schedule]]\ntime = "08:00"\ndays = ["holiday"]\nholidays = "exclude"\n',
            "矛盾しています",
        ),
        ('[[schedule]]\ntime = "08:00"\nenabled = "yes"\n', "enabled は"),
        ('[[schedule]]\ntime = "08:00"\nmessage = ""\n', "message は"),
        ('[[schedule]]\ntime = "08:00"\ntypo = 1\n', "未知のキー"),
        ("", "1 つも定義されていません"),
    ],
)
def test_invalid_configuration_raises(toml_text: str, expected_message: str):
    with pytest.raises(ConfigError, match=expected_message):
        parse(toml_text)


def test_missing_destination_mentions_env_var():
    with pytest.raises(ConfigError, match="TWILIO_TO_NUMBER"):
        parse_schedules(
            tomllib.loads('[[schedule]]\ntime = "08:00"\n'),
            default_message="x",
            default_to=(),
        )


def test_missing_file_raises():
    with pytest.raises(ConfigError, match="見つかりません"):
        load_schedules(REPO_ROOT / "no-such-file.toml", **DEFAULTS)
