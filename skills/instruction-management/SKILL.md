---
name: instruction-management
description: SlackからBotのスキルやルールの変更を依頼されたとき、既存内容を調べて変更案を作る。
---

# スキルとルールの管理

利用者から継続的な動作変更を求められたら、まず `instructions.read` を実行して既存のスキルとルールを確認する。対象が曖昧なら確認する。既存の記述と重複・矛盾しない最小の変更を作り、`instructions.update` に対象パス、読み取った `sha256`、変更後の全文を指定する。新規ファイルには `previous_sha256` に `missing` を指定する。Pythonが変更案を管理者に提示し、承認後に実行する。

手順は `skills/<名前>/SKILL.md`、共通または用途別ルールは `rules/` に置く。スキルにはYAML frontmatterのnameとdescriptionを含める。権限や承認の仕組みそのもの、ツール設定、プログラムの変更はこの操作ではできない。変更が必要な場合はその旨を伝える。
