# twilio_caller

指定したスケジュールに従って Twilio 経由で電話をかけ、応答があったら合成音声でメッセージを読み上げる常駐デーモン。Raspberry Pi (Debian 12) 上で systemd サービスとして動かすことを想定している。

- Webhook サーバは不要。`client.calls.create()` の `twiml` パラメータに TwiML を直接渡す
- 架電スケジュールは `schedules.toml` に何件でも記述できる（コード変更不要）
- 日本の祝日判定に [jpholiday](https://pypi.org/project/jpholiday/) を使用
- 留守番電話を検知したら、メッセージを読み上げずに切断

## 動作の仕組み（留守番電話検知）

Webhook を持たない構成では Twilio から AMD の結果（`AnsweredBy`）を受け取るコールバック先がない。そのため次の手順で判定している。

1. `machine_detection` を有効にして発信する（同期 AMD。判定が終わるまで TwiML の実行が保留される）
2. Call リソースを数秒おきに取得し、`answered_by` の確定を待つ
3. `machine_start` / `machine_end_*` / `fax` なら `update(status="completed")` で切断する
4. `human` および判定不能（`unknown`）はそのままメッセージを再生する

TwiML の先頭には `<Pause length="2"/>`（`CALLER_LEAD_PAUSE_SECONDS`）を入れてあり、切断が間に合わなかった場合でも本文が流れ始める前に切れるようにしている。AMD を使わない場合は `CALLER_MACHINE_DETECTION=off`。

> Twilio の AMD は有料オプション（1 コールあたり課金）。不要なら `off` にする。

## スケジュールの書き方

`schedules.toml` に `[[schedule]]` を並べる。

```toml
# 平日 (月〜金) 7:00。祝日は鳴らさない
[[schedule]]
name = "weekday-morning"
time = "07:00"
days = ["mon", "tue", "wed", "thu", "fri"]
holidays = "exclude"

# 土日・祝日は 11:00
[[schedule]]
name = "weekend-and-holiday"
time = "11:00"
days = ["sat", "sun", "holiday"]

# 土曜だけ 9:00 にもう一度 (土曜は 9:00 と 11:00 の 2 回)
[[schedule]]
name = "saturday-extra"
time = "09:00"
days = ["sat"]
```

| キー | 必須 | 内容 |
| --- | --- | --- |
| `time` | ○ | `"HH:MM"`（24時間表記）。TOML のローカル時刻 `07:00:00` 形式も可 |
| `name` | | ログ表示用の名前。省略時は時刻から自動生成 |
| `days` | | 対象日の **OR** 条件。省略時は毎日。`mon`〜`sun` / `holiday`（祝日）/ `weekday`（月〜金）/ `weekend`（土日）/ `all` |
| `holidays` | | `days` の結果に掛ける **AND** フィルタ。`include`（既定）/ `exclude`（祝日は除外）/ `only`（祝日のみ） |
| `message` | | 読み上げ文言。省略時は `CALLER_MESSAGE` |
| `to` | | 宛先番号。文字列または配列（複数宛先に順次発信）。省略時は `TWILIO_TO_NUMBER` |
| `enabled` | | `false` で一時的に無効化（既定 `true`） |

- 同じ日の同じ時刻に複数エントリがマッチした場合は、それぞれ独立に発信する
- `days` に `"holiday"` を含めつつ `holidays = "exclude"` は矛盾するため起動時にエラーになる
- 不正な値（時刻の書式、未知の曜日名、未知のキーなど）は**起動時にすべて検出**して終了する

変更後の反映手順:

```bash
python app.py --check          # 設定を検証し、今後 14 日分の予定を表示（発信はしない）
sudo systemctl restart twilio-caller
```

ラズパイ上では `.env` を読み込ませる必要がある（[7. 更新・設定変更](#7-更新設定変更) 参照）。

## セットアップ（Raspberry Pi / Debian 12）

### 1. uv と Python 3.13 を用意

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. 配置

clone してから `/opt` へ移動する。

```bash
git clone git@github.com:urib0/twilio_caller.git
sudo mv twilio_caller /opt/twilio_caller

# 実行用のシステムユーザーを作り、所有権を移す
sudo useradd --system --home /opt/twilio_caller --shell /usr/sbin/nologin twilio
sudo chown -R twilio:twilio /opt/twilio_caller
```

`.git` はそのまま残しておいてよい（`git pull` で更新できる）。systemd 側は `ReadWritePaths=/opt/twilio_caller` を指定してあるので動作に影響しない。

### 3. 依存関係をインストール

`.venv` と `.env` は `.gitignore` 済みで clone には含まれないため、ここで作る。

```bash
cd /opt/twilio_caller
uv sync --frozen                          # /opt/twilio_caller/.venv が作られる
sudo chown -R twilio:twilio /opt/twilio_caller
```

> uv をログインユーザーの `~/.local/bin` にインストールしている場合、`sudo -u twilio uv ...` では PATH が通らず失敗する。上のように自分のユーザーで `uv sync` してから `chown` し直すのが確実（絶対パスで `sudo -u twilio ~/.local/bin/uv sync --frozen --directory /opt/twilio_caller` としてもよい）。

### 4. 環境変数ファイル

```bash
sudo cp /opt/twilio_caller/.env.example /opt/twilio_caller/.env
sudo vi /opt/twilio_caller/.env       # SID / トークン / 発信元・宛先番号を設定
sudo chown root:twilio /opt/twilio_caller/.env
sudo chmod 640 /opt/twilio_caller/.env
```

主な環境変数（すべて `.env.example` に一覧あり）:

| 変数 | 既定値 | 内容 |
| --- | --- | --- |
| `TWILIO_ACCOUNT_SID` | （必須） | Account SID |
| `TWILIO_AUTH_TOKEN` | （必須） | Auth Token |
| `TWILIO_FROM_NUMBER` | （必須） | 発信元番号（E.164） |
| `TWILIO_TO_NUMBER` | | 既定の宛先。全エントリが `to` を持つなら省略可 |
| `CALLER_SCHEDULE_FILE` | `/opt/twilio_caller/schedules.toml` | スケジュール定義ファイル |
| `CALLER_MESSAGE` | おはようございます。時間になりました。 | 既定の読み上げ文言 |
| `CALLER_VOICE` / `CALLER_LANGUAGE` | `Polly.Mizuki` / `ja-JP` | 音声と言語 |
| `CALLER_MACHINE_DETECTION` | `Enable` | `Enable` / `DetectMessageEnd` / `off` |
| `CALLER_TIMEZONE` | `Asia/Tokyo` | スケジュールの基準タイムゾーン |
| `CALLER_DRY_RUN` | `0` | `1` で実発信せずログ出力のみ |

### 5. systemd に登録

```bash
sudo cp /opt/twilio_caller/twilio-caller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now twilio-caller
systemctl status twilio-caller
```

### 6. ログ

`python -u` と `PYTHONUNBUFFERED=1` を指定しているので、標準出力はそのまま journal に流れる。

```bash
journalctl -u twilio-caller -f          # 追跡
journalctl -u twilio-caller --since today
```

### 7. 更新・設定変更

`.git` を残してあるので、リポジトリを更新して再起動するだけでよい。所有権を `twilio` に移したあとは自分のユーザーで書き込めなくなるため、pull の間だけ戻す。

```bash
cd /opt/twilio_caller
sudo chown -R "$USER" /opt/twilio_caller   # 自分の SSH 鍵で pull するため一時的に戻す
git pull
uv sync --frozen                           # 依存が変わったときのみ
sudo chown -R twilio:twilio /opt/twilio_caller
sudo systemctl restart twilio-caller
```

`.env` と `.venv` は `.gitignore` 済みなので `git pull` で上書きされることはない。

`schedules.toml` を編集したときは、再起動の前に `--check` で確認する。`.env`（`root:twilio` / 640）を読ませる必要があるので、読み込んでから実行する。

```bash
sudo bash -c 'set -a; . /opt/twilio_caller/.env; set +a; \
  /opt/twilio_caller/.venv/bin/python /opt/twilio_caller/app.py --check'
```

## 開発・動作確認

```bash
uv sync
uv run pytest                                    # スケジュール判定のテスト

# 設定の検証と予定表示（発信しない）
CALLER_SCHEDULE_FILE=./schedules.toml uv run python app.py --check --days 14

# 実発信せずにデーモンを動かす（TwiML の内容がログに出る）
CALLER_DRY_RUN=1 CALLER_SCHEDULE_FILE=./schedules.toml uv run python -u app.py
```

実際に 1 本だけかけて確認したいとき:

```bash
uv run python -c "
import app
from twilio.rest import Client
cfg = app.Config.from_env()
app.place_call(Client(cfg.account_sid, cfg.auth_token), cfg, cfg.to_number, cfg.message)
"
```

## 注意点

- ラズパイは RTC を持たないため、unit は `time-sync.target` の後に起動するようにしている。NTP が有効か `timedatectl` で確認すること
- スケジュールの基準は `CALLER_TIMEZONE`（既定 `Asia/Tokyo`）。unit 側でも `TZ=Asia/Tokyo` を指定しているのでログの時刻も JST になる
- 一時的な停止（ネットワーク断など）から復帰した場合、`misfire_grace_time` は 300 秒。5 分以上遅れた発火は実行されない
- `.env` は `.gitignore` 済み。認証情報をコミットしないこと
