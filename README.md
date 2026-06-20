# J4-Secretary Discord Bot

Discord で買い物リスト・予定・メモを管理する Bot です。

## 機能

- **🛒 買い物リスト** — 場所と品名を追加・編集・完了
- **📅 予定管理** — 日時指定でリマインド、スヌーズ機能付き
- **📝 メモ** — 自由にメモを保存
- **🔍 検索** — 買い物履歴から検索
- **📍 場所管理** — よく行く場所の登録・並び替え
- **🔄 自動復旧** — Bot 再起動後もボタンが動作（Persistent Views）
- **💾 自動バックアップ** — データ保存時に自動バックアップ（60秒に1回）

## 必要要件

- Docker & Docker Compose
- Discord Bot トークン

## 初回導入

### 1. Discord Bot の作成

1. [Discord Developer Portal](https://discord.com/developers/applications) にアクセス
2. **New Application** → アプリ名を入力して作成
3. 左メニュー **Bot** → **Add Bot**
4. **Privileged Gateway Intents** で以下を有効化：
   - **Message Content Intent**
   - **Server Members Intent**
5. **Bot Token** をコピー（後で `.env` に設定）
6. **OAuth2** → **URL Generator** で招待リンクを生成：
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Read Messages/View Channels`, `Manage Messages`, `Embed Links`, `Read Message History`
7. 生成したリンクで Bot をサーバーに招待

### 2. ファイルの配置

```bash
mkdir -p ~/j4-secretary/app
mkdir -p ~/j4-secretary/data
```

以下のファイルを配置：

```
~/j4-secretary/
├── app/
│   └── main.py          # Bot 本体
├── data/                 # データ保存先（自動作成）
│   └── bot.log           # ログ（自動作成）
├── compose.yaml          # Docker Compose 設定
└── .env                  # トークン（要作成）
```

### 3. .env ファイルの作成

```bash
echo 'DISCORD_TOKEN=あなたのトークン' > ~/j4-secretary/.env
```

⚠️ `.env` ファイルは絶対に Git にコミットしないでください。

### 4. 起動

```bash
cd ~/j4-secretary
docker compose up -d
```

初回は `pip install discord.py` が走るので数秒かかります。

### 5. 動作確認

Discord チャンネルで：

```
!setup
```

パネルが表示されたら導入完了です。

## コマンド

| コマンド | 説明 |
|---------|------|
| `!setup` | パネルを表示 |
| `!stop` | Secretary 機能を停止 |
| `!help` | ヘルプを表示 |
| `!search <キーワード>` | 買い物履歴を検索 |
| `!list` | 登録済み場所一覧 |

## テキストで買い物を追加

チャンネルに直接テキストを送信すると買い物に追加されます：

```
西友 牛乳
```

- `場所 品名` → その場所に登録
- `品名` だけ → 履歴から場所を自動補完

## 運用

### ログ確認

```bash
docker compose logs -f
```

### 再起動

```bash
docker compose restart
```

### 停止

```bash
docker compose down
```

### バックアップ

データは `data/` に JSON で保存され、変更時に `data/backup/` に自動バックアップされます（最新10件）。

## ファイル構成

```
j4-secretary/
├── app/
│   └── main.py              # Bot 本体（全機能）
├── data/
│   ├── <channel_id>.json    # チャンネルごとのデータ
│   ├── bot.log              # ログ
│   └── backup/              # 自動バックアップ
│       └── <channel_id>_<timestamp>.json
├── compose.yaml              # Docker Compose 設定
└── .env                      # 環境変数（トークン）
```

## 環境変数

| 変数 | 説明 | デフォルト |
|------|------|-----------|
| `DISCORD_TOKEN` | Discord Bot トークン | 必須 |
| `TZ` | タイムゾーン | `Asia/Tokyo` |

## ライセンス

自由に使ってください。
