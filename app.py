"""スケジュールに従って Twilio 経由で自動架電する常駐デーモン.

架電スケジュールはコードに埋め込まず、外部の TOML ファイル (既定:
/opt/twilio_caller/schedules.toml) に [[schedule]] エントリとして
いくつでも記述できる。書式は schedules.toml のコメントを参照。

Webhook サーバは立てず、client.calls.create() の twiml パラメータに
TwiML 文字列を直接渡す。留守番電話検知は同期 AMD (machine_detection) を
有効にしたうえで Call リソースの answered_by をポーリングし、留守電と
判定されたらメッセージ本文の再生前に通話を切断する。

使い方:
    python -u app.py              スケジューラを起動して常駐する
    python app.py --check         設定を検証し、今後の架電予定を一覧表示する
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time as time_module
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import FrameType
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import jpholiday
from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

logger = logging.getLogger("twilio_caller")

DEFAULT_SCHEDULE_FILE = "/opt/twilio_caller/schedules.toml"
DEFAULT_MESSAGE = "おはようございます。時間になりました。"

# AMD が「人間ではない」と判定したときの answered_by の値。
# human と unknown (判定不能) は再生を続ける。
MACHINE_ANSWERS = frozenset(
    {"machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax"}
)

# 通話がこれらの状態になったら AMD の判定を待つ意味はない。
FINAL_CALL_STATUSES = frozenset({"completed", "busy", "failed", "no-answer", "canceled"})


class ConfigError(RuntimeError):
    """環境変数またはスケジュール定義ファイルの内容が不正."""


# --- 祝日判定 ----------------------------------------------------------------


def is_holiday(d: date) -> bool:
    """日本の祝日か (振替休日・国民の休日を含む)。"""
    return jpholiday.is_holiday(d)


# --- スケジュール定義 --------------------------------------------------------

WEEKDAY_NUMBERS: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
DAY_ALIASES: dict[str, frozenset[str]] = {
    "weekday": frozenset({"mon", "tue", "wed", "thu", "fri"}),
    "weekend": frozenset({"sat", "sun"}),
    "all": frozenset(WEEKDAY_NUMBERS),
}
PSEUDO_DAY_HOLIDAY = "holiday"
HOLIDAY_MODES = frozenset({"include", "exclude", "only"})

SCHEDULE_KEYS = frozenset({"name", "time", "days", "holidays", "message", "to", "enabled"})
ALL_WEEKDAYS = frozenset(WEEKDAY_NUMBERS.values())


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    name: str
    at: time
    weekdays: frozenset[int]  # 0=月 … 6=日
    match_holiday: bool  # days に "holiday" が含まれるか
    holidays: str  # include | exclude | only
    message: str
    to: tuple[str, ...]
    enabled: bool

    @property
    def label(self) -> str:
        return f"{self.name} ({self.at.strftime('%H:%M')})"


def entry_matches(entry: ScheduleEntry, d: date) -> bool:
    """日付 d がこのエントリの対象日か。

    days の OR マッチを評価し、そのうえで holidays フィルタを AND で掛ける。
    """
    holiday = is_holiday(d)

    matched = d.weekday() in entry.weekdays or (entry.match_holiday and holiday)
    if not matched:
        return False

    if entry.holidays == "exclude" and holiday:
        return False
    if entry.holidays == "only" and not holiday:
        return False
    return True


def _parse_time(raw: Any, where: str) -> time:
    if isinstance(raw, time):  # TOML のローカル時刻リテラル (例: 07:00:00)
        return raw.replace(second=0, microsecond=0, tzinfo=None)
    if not isinstance(raw, str):
        raise ConfigError(f"{where}: time は \"HH:MM\" 形式の文字列で指定してください: {raw!r}")
    try:
        return datetime.strptime(raw.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ConfigError(f"{where}: time を解釈できません (\"HH:MM\" 形式): {raw!r}") from exc


def _parse_days(raw: Any, where: str) -> tuple[frozenset[int], bool]:
    """days を (曜日番号の集合, 祝日にマッチするか) に展開する。"""
    if raw is None:
        return ALL_WEEKDAYS, False
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise ConfigError(f"{where}: days は文字列の配列で指定してください: {raw!r}")
    if not raw:
        raise ConfigError(f"{where}: days が空です。省略すれば毎日が対象になります")

    weekdays: set[int] = set()
    match_holiday = False
    for value in raw:
        key = value.strip().lower()
        if key == PSEUDO_DAY_HOLIDAY:
            match_holiday = True
        elif key in DAY_ALIASES:
            weekdays.update(WEEKDAY_NUMBERS[name] for name in DAY_ALIASES[key])
        elif key in WEEKDAY_NUMBERS:
            weekdays.add(WEEKDAY_NUMBERS[key])
        else:
            allowed = ", ".join(
                [*WEEKDAY_NUMBERS, PSEUDO_DAY_HOLIDAY, *DAY_ALIASES]
            )
            raise ConfigError(f"{where}: days の値 {value!r} が不正です (指定できる値: {allowed})")
    return frozenset(weekdays), match_holiday


def _parse_to(raw: Any, where: str, default_to: Sequence[str]) -> tuple[str, ...]:
    if raw is None:
        numbers: list[str] = list(default_to)
    elif isinstance(raw, str):
        numbers = [raw]
    elif isinstance(raw, list) and all(isinstance(v, str) for v in raw):
        numbers = list(raw)
    else:
        raise ConfigError(f"{where}: to は文字列または文字列の配列で指定してください: {raw!r}")

    # 重複を除きつつ記述順を保つ
    unique = list(dict.fromkeys(n.strip() for n in numbers if n.strip()))
    if not unique:
        raise ConfigError(
            f"{where}: 宛先がありません。to を指定するか、環境変数 TWILIO_TO_NUMBER を設定してください"
        )
    return tuple(unique)


def parse_schedules(
    data: dict[str, Any],
    *,
    default_message: str = DEFAULT_MESSAGE,
    default_to: Sequence[str] = (),
) -> list[ScheduleEntry]:
    """パース済みの TOML データから ScheduleEntry のリストを組み立てる。

    不正な内容はすべてここで ConfigError として検出し、起動時に落とす。
    """
    raw_entries = data.get("schedule")
    if raw_entries is None:
        raise ConfigError("[[schedule]] が 1 つも定義されていません")
    if not isinstance(raw_entries, list) or not all(isinstance(e, dict) for e in raw_entries):
        raise ConfigError("schedule は [[schedule]] のテーブル配列で記述してください")
    if not raw_entries:
        raise ConfigError("[[schedule]] が 1 つも定義されていません")

    entries: list[ScheduleEntry] = []
    for index, raw in enumerate(raw_entries, start=1):
        name = raw.get("name")
        where = f"schedule[{index}]" + (f" ({name})" if isinstance(name, str) and name else "")

        unknown = set(raw) - SCHEDULE_KEYS
        if unknown:
            raise ConfigError(
                f"{where}: 未知のキーがあります: {', '.join(sorted(unknown))} "
                f"(指定できるキー: {', '.join(sorted(SCHEDULE_KEYS))})"
            )

        if "time" not in raw:
            raise ConfigError(f"{where}: time は必須です")
        at = _parse_time(raw["time"], where)

        weekdays, match_holiday = _parse_days(raw.get("days"), where)

        holidays = raw.get("holidays", "include")
        if not isinstance(holidays, str) or holidays.strip().lower() not in HOLIDAY_MODES:
            raise ConfigError(
                f"{where}: holidays は {', '.join(sorted(HOLIDAY_MODES))} のいずれかです: {holidays!r}"
            )
        holidays = holidays.strip().lower()

        if match_holiday and holidays == "exclude":
            raise ConfigError(
                f"{where}: days に \"holiday\" を含めながら holidays = \"exclude\" は矛盾しています"
            )

        message = raw.get("message", default_message)
        if not isinstance(message, str) or not message.strip():
            raise ConfigError(f"{where}: message は空でない文字列で指定してください: {message!r}")

        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{where}: enabled は true / false で指定してください: {enabled!r}")

        entries.append(
            ScheduleEntry(
                name=name if isinstance(name, str) and name else f"schedule-{at.strftime('%H%M')}",
                at=at,
                weekdays=weekdays,
                match_holiday=match_holiday,
                holidays=holidays,
                message=message,
                to=_parse_to(raw.get("to"), where, default_to),
                enabled=enabled,
            )
        )
    return entries


def load_schedules(
    path: Path,
    *,
    default_message: str = DEFAULT_MESSAGE,
    default_to: Sequence[str] = (),
) -> list[ScheduleEntry]:
    try:
        with path.open("rb") as fp:
            data = tomllib.load(fp)
    except FileNotFoundError as exc:
        raise ConfigError(f"スケジュール定義ファイルが見つかりません: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} の TOML を解釈できません: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path} を読み込めません: {exc}") from exc

    return parse_schedules(data, default_message=default_message, default_to=default_to)


# --- 環境変数 ----------------------------------------------------------------


def _env(name: str, default: str = "", *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"環境変数 {name} が設定されていません")
    return value


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"環境変数 {name} は整数で指定してください: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"環境変数 {name} は {minimum} 以上で指定してください: {value}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Config:
    account_sid: str
    auth_token: str
    from_number: str
    to_number: str
    schedule_file: Path
    message: str
    voice: str
    language: str
    say_repeat: int
    lead_pause_seconds: int
    ring_timeout: int
    call_interval_seconds: int
    machine_detection: str  # "Enable" | "DetectMessageEnd" | "" (無効)
    machine_detection_timeout: int
    amd_poll_seconds: int
    amd_poll_timeout: int
    timezone: ZoneInfo
    dry_run: bool

    @classmethod
    def from_env(cls) -> Config:
        tz_name = _env("CALLER_TIMEZONE", "Asia/Tokyo")
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:  # ZoneInfoNotFoundError / ValueError をまとめて扱う
            raise ConfigError(f"タイムゾーン {tz_name!r} を解決できません") from exc

        machine_detection = _env("CALLER_MACHINE_DETECTION", "Enable").strip()
        if machine_detection.lower() in {"off", "none", "false", "0", ""}:
            machine_detection = ""
        elif machine_detection not in {"Enable", "DetectMessageEnd"}:
            raise ConfigError(
                "CALLER_MACHINE_DETECTION は Enable / DetectMessageEnd / off のいずれかです: "
                f"{machine_detection!r}"
            )

        return cls(
            account_sid=_env("TWILIO_ACCOUNT_SID", required=True),
            auth_token=_env("TWILIO_AUTH_TOKEN", required=True),
            from_number=_env("TWILIO_FROM_NUMBER", required=True),
            to_number=_env("TWILIO_TO_NUMBER").strip(),
            schedule_file=Path(_env("CALLER_SCHEDULE_FILE", DEFAULT_SCHEDULE_FILE)),
            message=_env("CALLER_MESSAGE", DEFAULT_MESSAGE),
            voice=_env("CALLER_VOICE", "Polly.Mizuki"),
            language=_env("CALLER_LANGUAGE", "ja-JP"),
            say_repeat=_env_int("CALLER_SAY_REPEAT", 2, minimum=1),
            lead_pause_seconds=_env_int("CALLER_LEAD_PAUSE_SECONDS", 2, minimum=0),
            ring_timeout=_env_int("CALLER_RING_TIMEOUT", 40, minimum=5),
            call_interval_seconds=_env_int("CALLER_CALL_INTERVAL_SECONDS", 5, minimum=0),
            machine_detection=machine_detection,
            machine_detection_timeout=_env_int("CALLER_MACHINE_DETECTION_TIMEOUT", 20, minimum=3),
            amd_poll_seconds=_env_int("CALLER_AMD_POLL_SECONDS", 1, minimum=1),
            amd_poll_timeout=_env_int("CALLER_AMD_POLL_TIMEOUT", 45, minimum=5),
            timezone=tz,
            dry_run=_env_bool("CALLER_DRY_RUN"),
        )

    @property
    def default_to(self) -> tuple[str, ...]:
        return (self.to_number,) if self.to_number else ()


# --- TwiML -------------------------------------------------------------------


def _attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def build_twiml(cfg: Config, message: str) -> str:
    """再生する TwiML を組み立てる。

    先頭に <Pause> を置くことで、AMD の判定結果を受けて切断するまでの間に
    メッセージ本文が流れ始めるのを防ぐ。
    """
    say = (
        f'<Say voice="{_attr(cfg.voice)}" language="{_attr(cfg.language)}">'
        f"{escape(message)}</Say>"
    )

    parts = ["<Response>"]
    if cfg.lead_pause_seconds > 0:
        parts.append(f'<Pause length="{cfg.lead_pause_seconds}"/>')
    for i in range(cfg.say_repeat):
        if i:
            parts.append('<Pause length="1"/>')
        parts.append(say)
    parts.append("</Response>")
    return "".join(parts)


# --- 架電 --------------------------------------------------------------------


def _wait_for_amd(client: Client, call_sid: str, cfg: Config) -> str | None:
    """answered_by が確定するまで Call リソースをポーリングする。

    Webhook を持たない構成のため、AMD の結果は Call リソースを取得して読む。
    確定しなかった場合や通話が先に終了した場合は None を返す。
    """
    deadline = time_module.monotonic() + cfg.amd_poll_timeout
    while time_module.monotonic() < deadline:
        try:
            call = client.calls(call_sid).fetch()
        except TwilioRestException:
            logger.exception("[%s] Call リソースの取得に失敗しました", call_sid)
            return None

        if call.answered_by:
            return str(call.answered_by)
        if call.status in FINAL_CALL_STATUSES:
            logger.info("[%s] AMD 判定前に通話が終了しました (status=%s)", call_sid, call.status)
            return None
        time_module.sleep(cfg.amd_poll_seconds)

    logger.warning(
        "[%s] AMD の判定が %d 秒以内に確定しませんでした", call_sid, cfg.amd_poll_timeout
    )
    return None


def _hangup(client: Client, call_sid: str) -> None:
    try:
        client.calls(call_sid).update(status="completed")
        logger.info("[%s] 留守番電話と判定したため切断しました", call_sid)
    except TwilioRestException:
        logger.exception("[%s] 切断に失敗しました", call_sid)


def place_call(client: Client | None, cfg: Config, to: str, message: str) -> str | None:
    """1 件発信し、留守電と判定されたら切断する。成功時は Call SID を返す。"""
    twiml = build_twiml(cfg, message)

    if cfg.dry_run or client is None:
        logger.info("[dry-run] %s -> %s twiml=%s", cfg.from_number, to, twiml)
        return None

    params: dict[str, Any] = {
        "to": to,
        "from_": cfg.from_number,
        "twiml": twiml,
        "timeout": cfg.ring_timeout,
    }
    if cfg.machine_detection:
        params["machine_detection"] = cfg.machine_detection
        params["machine_detection_timeout"] = cfg.machine_detection_timeout

    try:
        call = client.calls.create(**params)
    except TwilioRestException:
        logger.exception("発信に失敗しました (%s -> %s)", cfg.from_number, to)
        return None

    logger.info("[%s] 発信しました (%s -> %s)", call.sid, cfg.from_number, to)

    if cfg.machine_detection:
        answered_by = _wait_for_amd(client, call.sid, cfg)
        if answered_by:
            logger.info("[%s] AMD 判定: %s", call.sid, answered_by)
            if answered_by in MACHINE_ANSWERS:
                _hangup(client, call.sid)

    return call.sid


def run_entry(client: Client | None, cfg: Config, entry: ScheduleEntry) -> None:
    """エントリの全宛先へ順次発信する。1 件失敗しても残りは続行する。"""
    for index, to in enumerate(entry.to):
        if index and cfg.call_interval_seconds:
            time_module.sleep(cfg.call_interval_seconds)
        try:
            place_call(client, cfg, to, entry.message)
        except Exception:
            logger.exception("[%s] %s への発信処理で予期しないエラーが発生しました", entry.name, to)


# --- スケジューラ ------------------------------------------------------------


def make_job(client: Client | None, cfg: Config, entry: ScheduleEntry):
    def job() -> None:
        today = datetime.now(cfg.timezone).date()
        if not entry_matches(entry, today):
            logger.info("%s: %s は対象日ではないためスキップします", entry.label, today)
            return
        logger.info("%s: %s の架電を開始します (宛先 %d 件)", entry.label, today, len(entry.to))
        run_entry(client, cfg, entry)

    job.__name__ = f"job_{entry.name}"
    return job


def build_scheduler(
    client: Client | None, cfg: Config, entries: Iterable[ScheduleEntry]
) -> BlockingScheduler:
    scheduler = BlockingScheduler(
        timezone=cfg.timezone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )
    for index, entry in enumerate(entries, start=1):
        if not entry.enabled:
            logger.info("%s: enabled = false のため登録しません", entry.label)
            continue
        scheduler.add_job(
            make_job(client, cfg, entry),
            trigger=CronTrigger(hour=entry.at.hour, minute=entry.at.minute, timezone=cfg.timezone),
            id=f"{index:02d}-{entry.name}",
            name=entry.label,
        )
    return scheduler


# --- --check (予定の一覧表示) ------------------------------------------------


def upcoming(entries: Sequence[ScheduleEntry], start: date, days: int) -> list[tuple[date, ScheduleEntry]]:
    """start から days 日分の架電予定を (日付, エントリ) の時系列で返す。"""
    plan: list[tuple[date, ScheduleEntry]] = []
    for offset in range(days):
        d = start + timedelta(days=offset)
        matched = [e for e in entries if e.enabled and entry_matches(e, d)]
        plan.extend((d, e) for e in sorted(matched, key=lambda e: e.at))
    return plan


def print_check(cfg: Config, entries: Sequence[ScheduleEntry], days: int) -> None:
    weekday_ja = "月火水木金土日"
    print(f"設定ファイル: {cfg.schedule_file}")
    print(f"タイムゾーン: {cfg.timezone.key}\n")

    print(f"エントリ ({len(entries)} 件):")
    for entry in entries:
        state = "" if entry.enabled else "  [無効]"
        print(f"  - {entry.label}{state}")
        print(f"      宛先    : {', '.join(entry.to)}")
        print(f"      文言    : {entry.message}")
        days_desc = ", ".join(
            [
                *(name for name, num in WEEKDAY_NUMBERS.items() if num in entry.weekdays),
                *(["holiday"] if entry.match_holiday else []),
            ]
        )
        print(f"      対象日  : {days_desc or '(なし)'} / holidays={entry.holidays}")

    today = datetime.now(cfg.timezone).date()
    print(f"\n今後 {days} 日間の架電予定:")
    plan = upcoming(entries, today, days)
    if not plan:
        print("  (予定はありません)")
        return
    for d, entry in plan:
        holiday_names = jpholiday.is_holiday_name(d)
        mark = f" [{holiday_names}]" if holiday_names else ""
        print(f"  {d} ({weekday_ja[d.weekday()]}){mark}  {entry.at.strftime('%H:%M')}  {entry.name}")


# --- エントリポイント --------------------------------------------------------


def setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("CALLER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Twilio 自動架電デーモン")
    parser.add_argument(
        "--check",
        action="store_true",
        help="設定を検証して今後の架電予定を表示し、発信せずに終了する",
    )
    parser.add_argument(
        "--days", type=int, default=14, help="--check で表示する日数 (既定: 14)"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()

    try:
        cfg = Config.from_env()
        entries = load_schedules(
            cfg.schedule_file, default_message=cfg.message, default_to=cfg.default_to
        )
    except ConfigError as exc:
        logger.error("設定エラー: %s", exc)
        return 1

    if args.check:
        print_check(cfg, entries, max(1, args.days))
        return 0

    client = None if cfg.dry_run else Client(cfg.account_sid, cfg.auth_token)
    scheduler = build_scheduler(client, cfg, entries)

    # 停止シグナルは複数回届くことがある (systemd の再送、uv 等のラッパーからの転送)。
    # 2 回目以降や start() 前に受け取った場合でもトレースバックを出さずに終わらせる。
    shutdown_requested = threading.Event()

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        logger.info("シグナル %s を受信しました。終了します", signal.Signals(signum).name)
        try:
            scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            pass

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    logger.info(
        "起動しました tz=%s schedule=%s machine_detection=%s dry_run=%s",
        cfg.timezone.key,
        cfg.schedule_file,
        cfg.machine_detection or "off",
        cfg.dry_run,
    )
    jobs = scheduler.get_jobs()
    if not jobs:
        logger.error("有効なスケジュールが 1 つもありません: %s", cfg.schedule_file)
        return 1
    now = datetime.now(cfg.timezone)
    for job in jobs:
        # 起動前のジョブは next_run_time がまだ確定していないためトリガから直接求める
        logger.info("ジョブ登録: %s -> 次回発火 %s", job.name, job.trigger.get_next_fire_time(None, now))

    try:
        if not shutdown_requested.is_set():
            scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    logger.info("停止しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
