# Slack AIエージェント

Notion・Google Calendar・KaggleをSlackから操作するアシスタントです。

## セットアップ

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
