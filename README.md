# Slack AIエージェント

NotionとGoogle CalendarをSlackから操作する、個人用アシスタントです。
Codex CLIが操作案を生成し、Pythonが検証・実行します。
公開情報の質問は、Notionで把握した対象を起点にKaggle公式・GitHubなどをWebで調べます。
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

## 使い方

許可したDMまたは`@Bot`へのメンションで依頼します。
書き込みは提示された内容を確認し、同じスレッドで`承認 <ジョブID>`と返信します。

公開Web調査は`config/tools.toml`の`web_search`で切り替えます（`live` / `disabled`）。
調査方針は`rules/research.md`で管理します。ログイン後のチーム固有情報は公開検索の対象外です。

情報源の選択は`task evaluate:research`で検証できます。Notion・Calendarは架空の取得結果を使い、
Webは実際に検索します。外部への書込みは行いません。Codexの利用枠を消費し、
結果は`var/research-evaluation.json`に保存します。個別実行は
`task evaluate:research -- --case public_documentation`の形式です。
