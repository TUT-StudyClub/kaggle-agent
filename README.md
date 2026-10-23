# Slack AIエージェント

NotionとGoogle CalendarをSlackから操作する、個人用アシスタントです。
Codex CLIが質問に応じてNotion・Google Calendar・Webを調べ、Pythonが操作を検証・実行します。
書き込みは内容を提示し、Slackで承認を受けてから実行します。

## セットアップ

Python 3.14・uv・Task・認証済みのCodex CLIを用意します。

```sh
task sync
task init
```

1. [Slackマニフェスト](config/slack-manifest.json)でAppを作成・インストールし、
   Socket Modeを有効にして`connections:write`付きのAppトークンを発行します。
2. [.env.example](.env.example)を参考に、`.env`へトークンと許可するユーザー・チャンネル（DMを含む）・Notion・カレンダーを設定します。
   Notionの対象ページにはインテグレーションを接続します。
3. Googleのデスクトップ用OAuthクライアントJSONを`GOOGLE_CLIENT_FILE`のパスへ配置します。

```sh
task auth:google
task doctor:ai
task doctor
task run
```

`task doctor:ai`はCodexの利用枠を消費します。診断がすべて成功したら起動してください。

## 使い方

- チャンネルではBotへのメンションで会話を開始します。続きの質問はメンション不要です。
- 操作案は、依頼した本人が👍などのリアクションか同じスレッドへの「OK」で承認します。
- スレッドへの`!stop`で全員分の自動追跡を停止し、Botへの再メンションで再開します。

## 開発

`task check`でlint・型・依存関係・テストを検証し、`task format`でコードを整形します。
