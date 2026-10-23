# Slack AIエージェント

NotionとGoogle CalendarをSlackから操作する、個人用アシスタントです。
Codex CLIが操作案を生成し、Pythonが検証・実行します。
質問に応じてNotion・Google Calendar・Webを選び、必要に応じて組み合わせて調べます。
書き込みは内容を提示し、Slackで承認を受けてから実行します。

## セットアップ

```sh
task sync
task init
```

1. [Slackマニフェスト](config/slack-manifest.json)でAppを作成し、
   Socket Modeと`connections:write`を持つAppトークンを用意します。
2. `.env`にSlack・Notionのトークンと、許可するユーザー・
   チャンネル（DMを含む）・Notionページ・カレンダーを設定します。
   Notionの対象ページにはインテグレーションを接続します。
3. Googleのデスクトップ用OAuthクライアントJSONを、設定の`GOOGLE_CLIENT_FILE`へ配置します。
4. 以下を実行してGoogle認証とCodexの接続確認を行います。
   `task doctor:ai`はCodexの利用枠を消費します。

   ```sh
   task auth:google
   task doctor:ai
   ```

5. 利用条件・利用枠を確認し、設定・接続診断がすべて成功したら起動します。

   ```sh
   task doctor
   task run
   ```
