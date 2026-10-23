---
name: kaggle
description: Kaggle CLIによるコンペ・Dataset・Notebook・Model・Discussion・Benchmarkの検索、閲覧、取得と管理者限定の変更。
---

# Kaggle CLI

`kaggle.help`で操作一覧、`command`を指定するとその操作の引数を確認できる。
`command`は`datasets list`や`models instances versions download`のような、一覧にある完全なコマンド名。
`arguments`はシェル文字列ではなく、`["-s", "titanic", "--format", "json"]`のような引数の配列。
CLIのエイリアス・フラグの省略形を使わず、ヘルプで確認した完全なフラグだけを使う。

## 調査と取得

- `kaggle.read`でコンペの一覧・ファイル・提出履歴・スコア・ページ・Discussionを調べる。
- Dataset・Notebook・Model・Benchmarkの検索・状態確認・ダウンロードを扱う。
- `kernels logs`は1回の取得に使う。実行が終わっていなければ、その状態を説明する。
- 取得先はPythonがジョブごとに分離する。`--path`・`--wp`・`--output`を指定しない。
  返された`files`はKAGGLE_WORKSPACEからの相対パス。ファイルの取得とSlack添付は別であり、
  ローカルに保存しただけで「Slackに送信した」と報告しない。
- `datasets metadata`は通常は閲覧だが、`--update`を付けると変更なので`kaggle.write`が必要。

## 作成・実行・更新・提出・公開・削除

- `resources.kaggle_may_write=true`の依頼者だけが`kaggle.write`を利用できる。
- 非公開のNotebook実行・Dataset更新・Model作成、ローカルのメタデータ初期化、
  アップロード、コンペのホスト管理も変更操作に含む。
- Notebook実行は`kernels push`、コンペへの提出は`competitions submit`を使う。
  公開状態はアップロード元のメタデータにも依存する。推測で公開状態を決めない。
- 変更は既存のSlack承認を受けてからPythonが実行する。本人の依頼でも承認は省略しない。
- 作業ファイルは運用者がKAGGLE_WORKSPACE内に配置する。パスはそのディレクトリ基準。
  例: `competitions submit`の`arguments`は
  `["titanic", "--file", "owner/submission.csv", "--message", "baseline"]`。
- 同期の`--wait`・ログの`--follow`・アーカイブの自動展開は使わない。
  起動・受付と実行完了を区別し、必要なら別の状態確認操作を使う。
- 成否が不明な変更は再実行しない。実行結果を確認してから別の依頼として扱う。

認証・設定変更・トークン表示はSlack経由では扱わない。
公式CLI: https://github.com/Kaggle/kaggle-cli/tree/main/docs
