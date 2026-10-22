import argparse
import json
import logging
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from pydantic import ValidationError

from apps.slack.main import run
from composition import build
from config import Settings
from diagnostics import (
    authorize_google,
    configuration_report,
    initialize,
    service_report,
)
from modules.interaction.composition import build_job_management
from modules.planning.composition import build_planning
from shared.errors import AgentError


def doctor(probe: bool, evaluate: bool = False) -> None:
    planning = build_planning()
    print("Python: " + sys.version.split()[0])
    print("Codex: ChatGPT認証を確認")
    print(f"モデル: {planning.model} / 推論強度: {planning.reasoning_effort}（固定）")
    print("スキル: " + ", ".join(skill.name for skill in planning.skillbook.skills))
    print("実アカウントの課金・クォータ設定と利用条件は別途確認が必要です。")
    if probe:
        print(f"構造化出力: 成功 ({planning.probe():.1f}秒)")
    if evaluate:
        results = planning.evaluate()
        print(json.dumps(results, ensure_ascii=False, indent=2))
        if not all(result["passed"] for result in results):
            raise AgentError("evaluation_failed", "日本語の評価ケースが不合格でした。")


def main() -> None:
    parser = argparse.ArgumentParser(description="Slack AIエージェント (Python 3.14)")
    parser.add_argument("--config", type=Path, default=Path(".env"), help="読み込む.envファイル")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    commands.add_parser("init", help="既存ファイルを上書きせずローカル設定を作成")
    commands.add_parser("auth-google", help="ブラウザでGoogleを認証しローカルにトークンを保存")
    check = commands.add_parser("doctor")
    check.add_argument("--probe", action="store_true", help="契約枠を使い構造化出力を1回検証")
    check.add_argument("--services", action="store_true", help="実接続先の設定・権限を読取りで検査")
    check.add_argument(
        "--evaluate", action="store_true", help="架空データで日本語の意図判定を4回検証"
    )
    commands.add_parser("purge", help="設定した保持期間を過ぎた本文・ジョブ等を削除")
    status = commands.add_parser("status")
    status.add_argument("job_id")
    reconcile = commands.add_parser(
        "reconcile", help="結果不明の書き込みを外部APIの読取りだけで照合"
    )
    reconcile.add_argument("job_id")
    args = parser.parse_args()
    # HTTP/SDKのDEBUGログには秘密情報や本文が含まれるので有効化しない。
    logging.basicConfig(level=logging.CRITICAL)
    try:
        if args.command == "init":
            created = initialize(Path.cwd())
            print(
                "作成: " + ", ".join(created) if created else "既存の設定ファイルを保持しました。"
            )
            print(".envに接続先IDとトークンを入力し、task doctorを実行してください。")
            return
        if args.command == "doctor":
            doctor(args.probe, args.evaluate)
            if args.services:
                settings, checks = configuration_report(args.config)
                if settings is not None and all(check.ok for check in checks):
                    checks.extend(service_report(settings))
                for check in checks:
                    print(f"{'OK' if check.ok else '未完了'} {check.name}: {check.message}")
                if not all(check.ok for check in checks):
                    raise SystemExit(1)
            return
        settings = Settings.load(args.config)
        if args.command == "auth-google":
            authorize_google(settings)
            print(
                "Google OAuthの認証ファイルを保存しました。task doctorで接続先を確認してください。"
            )
            return
        if args.command == "run":
            run(settings)
            return
        if args.command == "reconcile":
            with ExitStack() as stack:
                _, agent = build(settings, stack)
                print(
                    "照合済み"
                    if agent.reconcile(args.job_id)
                    else "結果を確認できません。再送しません。"
                )
            return
        jobs = build_job_management(settings.database)
        if args.command == "purge":
            print(f"削除ジョブ数: {jobs.purge(time.time() - settings.retention_days * 86400)}")
        else:
            job = jobs.status(args.job_id)
            print(
                json.dumps(
                    {"id": job.id, "status": job.status, "error_code": job.error_code}
                    if job
                    else {"error": "not_found"},
                    ensure_ascii=False,
                )
            )
    except AgentError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        raise SystemExit(1) from None
    except OSError, ValidationError, ValueError:
        print(
            "設定または実行環境を確認してください。秘密情報を含む詳細は出力しません。",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except Exception:
        print(
            "外部サービスの接続・認証を確認してください。詳細の生出力は抑止しました。",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
