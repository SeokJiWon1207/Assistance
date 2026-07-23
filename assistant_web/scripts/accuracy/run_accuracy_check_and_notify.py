#!/usr/bin/env python3
"""dynamic_accuracy_check.py를 실행하고, 통과하지 못했을 때만 Slack으로 알린다.

이 스크립트 자체는 cron/launchd에 등록하지 않는다 - 등록 여부와 주기는
사용자가 직접 결정해서 crontab/launchd에 추가해야 한다. 여기서는 실행과
알림 로직만 제공한다.

사용법:
    cd assistant_web
    python3 scripts/accuracy/run_accuracy_check_and_notify.py

Slack 알림을 받으려면 .env 또는 환경변수에 SLACK_WEBHOOK_URL(Incoming Webhook URL)을
설정한다. 설정하지 않으면 알림 없이 검증만 실행한다.

crontab 등록 예시(매일 오전 8시):
    0 8 * * * cd /path/to/Assistance/assistant_web && /usr/bin/python3 scripts/accuracy/run_accuracy_check_and_notify.py >> runtime/accuracy_reports/cron.log 2>&1
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent.parent
REPORT_DIR = APP_DIR / "runtime" / "accuracy_reports"
DEFAULT_OBSIDIAN_VAULTS = (
    APP_DIR.parent / "ObsidianNotes",
    Path.home() / "Documents" / "Obsidian Vault",
)
DEFAULT_OBSIDIAN_FOLDER = "Automation Reports/Mantis Notion"


def load_dotenv() -> None:
    env_path = APP_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def latest_report() -> Optional[Dict[str, Any]]:
    json_reports = sorted(REPORT_DIR.glob("accuracy-*.json"))
    if not json_reports:
        return None
    return json.loads(json_reports[-1].read_text(encoding="utf-8"))


def latest_report_path() -> Optional[Path]:
    json_reports = sorted(REPORT_DIR.glob("accuracy-*.json"))
    return json_reports[-1] if json_reports else None


def previous_report_path(current_report: Path) -> Optional[Path]:
    json_reports = sorted(REPORT_DIR.glob("accuracy-*.json"))
    previous = [path for path in json_reports if path != current_report]
    return previous[-1] if previous else None


def load_report(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_slack_message(report: Dict[str, Any], exit_code: int) -> str:
    summary = report.get("summary", {})
    errors = report.get("errors", [])
    failed_results = [
        result
        for result in report.get("results", [])
        if result.get("outcome") in {"assertion_failed", "blocked_by_prerequisite"}
        or (not result.get("outcome") and not result.get("passed"))
    ]
    dependency_results = [
        result
        for result in report.get("results", [])
        if result.get("outcome") in {"dependency_error", "execution_error"}
    ]
    score = summary.get("score")
    score_label = f"{score}%" if score is not None else "N/A"
    status_label = {
        "passed": "통과",
        "failed": "정확도 실패",
        "incomplete": "연동 오류로 불완전",
    }.get(summary.get("status"), "통과" if exit_code == 0 else "확인 필요")

    lines = [
        f"*AI 비서 정확도 자동 검증* — {status_label}",
        f"정확도: {score_label} ({summary.get('passed', 0)}/{summary.get('total', 0)} 평가)",
        (
            f"기본: {(summary.get('base') or {}).get('passed', 0)}/{(summary.get('base') or {}).get('total', 0)} · "
            f"후속: {(summary.get('follow_up') or {}).get('passed', 0)}/{(summary.get('follow_up') or {}).get('total', 0)}"
        ),
        f"실행 시각: {report.get('run_at', '')}",
    ]
    if errors:
        lines.append(f"오류 {len(errors)}건: " + "; ".join(str(e) for e in errors[:3]))
    if failed_results:
        lines.append(f"실패 질문 {len(failed_results)}건:")
        for result in failed_results[:5]:
            question = str(result.get("question", ""))[:120]
            lines.append(f"- [{result.get('source', '?')}] {question}")
        if len(failed_results) > 5:
            lines.append(f"…외 {len(failed_results) - 5}건")
    if dependency_results:
        lines.append(f"연동/실행 오류 문항 {len(dependency_results)}건")
    return "\n".join(lines)


def detect_obsidian_vault() -> Optional[Path]:
    configured = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    if configured:
        vault = Path(configured).expanduser()
        return vault if vault.exists() and vault.is_dir() else None
    for candidate in DEFAULT_OBSIDIAN_VAULTS:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def obsidian_note_directory(vault: Path) -> Path:
    folder = os.getenv("OBSIDIAN_ACCURACY_FOLDER", DEFAULT_OBSIDIAN_FOLDER).strip().strip("/")
    return vault / folder if folder else vault


def source_pass_counts(report: Dict[str, Any]) -> List[Tuple[str, int, int]]:
    results = report.get("results", [])
    counts: List[Tuple[str, int, int]] = []
    for source in ("mantis", "notion"):
        source_results = [
            result
            for result in results
            if result.get("source") == source
            and result.get("outcome") not in {"dependency_error", "execution_error"}
        ]
        passed = sum(1 for result in source_results if result.get("passed"))
        counts.append((source, passed, len(source_results)))
    return counts


def score_delta_line(report: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> str:
    current_raw = (report.get("summary") or {}).get("score")
    if current_raw is None:
        return "- 직전 대비: 계산하지 않음 (현재 실행 불완전)"
    current_score = int(current_raw)
    if not previous:
        return "- 직전 리포트: 없음"
    previous_raw = (previous.get("summary") or {}).get("score")
    if previous_raw is None:
        return "- 직전 대비: 계산하지 않음 (이전 실행 불완전)"
    previous_score = int(previous_raw)
    delta = current_score - previous_score
    sign = "+" if delta > 0 else ""
    previous_run_at = str(previous.get("run_at") or "")
    return f"- 직전 대비: {sign}{delta}점 (이전 {previous_score}% · {previous_run_at})"


def format_failed_result(result: Dict[str, Any]) -> str:
    expected = json.dumps(result.get("expected"), ensure_ascii=False)
    actual = json.dumps(
        result.get("dependency_error") or result.get("error") or result.get("actual", ""),
        ensure_ascii=False,
    )
    question = str(result.get("question") or "").strip()
    return (
        f"- [{result.get('source', '?')}] {result.get('category', '')}: {question}\n"
        f"  - expected: `{expected}`\n"
        f"  - actual: `{actual}`"
    )


def build_obsidian_note(report: Dict[str, Any], report_path: Path, previous: Optional[Dict[str, Any]]) -> str:
    summary = report.get("summary") or {}
    results = report.get("results") or []
    errors = report.get("errors") or []
    failed_results = [
        result
        for result in results
        if result.get("outcome") in {"assertion_failed", "blocked_by_prerequisite"}
        or (not result.get("outcome") and not result.get("passed"))
    ]
    dependency_results = [
        result
        for result in results
        if result.get("outcome") in {"dependency_error", "execution_error"}
    ]
    source_lines = [
        f"- {source.capitalize()}: {passed}/{total}"
        for source, passed, total in source_pass_counts(report)
    ]
    run_at = str(report.get("run_at") or "")
    note_title = f"Mantis·Notion 정확도 점검 {run_at[:10]}"

    score = summary.get("score")
    score_label = f"{score}%" if score is not None else "N/A"
    lines = [
        "---",
        "tags: [automation, accuracy-check, mantis, notion]",
        "---",
        "",
        f"# {note_title}",
        "",
        f"- 실행 시각: {run_at}",
        f"- 실행 상태: {summary.get('status', 'unknown')}",
        f"- 정확도: {score_label} ({summary.get('passed', 0)}/{summary.get('total', 0)} 평가)",
        f"- 기본 질문: {(summary.get('base') or {}).get('passed', 0)}/{(summary.get('base') or {}).get('total', 0)}",
        f"- 후속 시나리오: {(summary.get('follow_up') or {}).get('passed', 0)}/{(summary.get('follow_up') or {}).get('total', 0)}",
        f"- 시도 문항: {summary.get('attempted', len(results))}",
        *source_lines,
        score_delta_line(report, previous),
        f"- 원본 JSON: `{report_path}`",
        f"- 원본 Markdown: `{report_path.with_suffix('.md')}`",
        "",
    ]

    if failed_results:
        lines.extend(["## 실패 문항", ""])
        lines.extend(format_failed_result(result) for result in failed_results)
        lines.append("")
    else:
        lines.extend(["## 실패 문항", "", "- 없음", ""])

    if errors:
        lines.extend(["## 연동 오류", ""])
        lines.extend(f"- {error}" for error in errors)
        lines.append("")
    else:
        lines.extend(["## 연동 오류", "", "- 없음", ""])

    if dependency_results:
        lines.extend(["## 연동 오류 문항", ""])
        lines.extend(format_failed_result(result) for result in dependency_results)
        lines.append("")

    lines.extend([
        "## 메모",
        "",
        "- 통과 케이스 상세는 원본 리포트에서 확인",
        "- 이 노트는 예약 정확도 점검 실행 후 자동 생성",
        "",
    ])
    return "\n".join(lines)


def write_obsidian_note(report_path: Path, report: Dict[str, Any]) -> Optional[Path]:
    vault = detect_obsidian_vault()
    if not vault:
        print("Obsidian 볼트를 찾지 못해 노트 저장을 건너뜁니다.", file=sys.stderr)
        return None

    note_dir = obsidian_note_directory(vault)
    note_dir.mkdir(parents=True, exist_ok=True)
    previous_path = previous_report_path(report_path)
    previous = load_report(previous_path) if previous_path else None

    run_at_raw = str(report.get("run_at") or "")
    try:
        run_at = datetime.fromisoformat(run_at_raw)
        stamp = run_at.strftime("%Y-%m-%d %H-%M-%S")
    except ValueError:
        stamp = report_path.stem.replace("accuracy-", "")
    note_path = note_dir / f"{stamp} Mantis Notion 정확도 점검.md"
    note_path.write_text(build_obsidian_note(report, report_path, previous), encoding="utf-8")
    return note_path


def notify_slack(webhook_url: str, report: Dict[str, Any], exit_code: int) -> None:
    payload = {"text": build_slack_message(report, exit_code)}
    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        if response.status_code >= 300:
            print(f"Slack 알림 전송 실패: {response.status_code} {response.text[:200]}", file=sys.stderr)
    except requests.RequestException as exc:
        print(f"Slack 알림 전송 실패: {exc}", file=sys.stderr)


def run_accuracy_check(questions: str, follow_up_scenarios: str) -> int:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "dynamic_accuracy_check.py"),
            "--questions",
            questions,
            "--follow-up-scenarios",
            follow_up_scenarios,
        ],
        cwd=APP_DIR,
    )
    return result.returncode


def main() -> int:
    load_dotenv()
    questions = os.getenv("ACCURACY_CHECK_QUESTIONS", "10")
    follow_up_scenarios = os.getenv("ACCURACY_CHECK_FOLLOW_UP_SCENARIOS", "4")
    exit_code = run_accuracy_check(questions, follow_up_scenarios)

    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("SLACK_WEBHOOK_URL이 설정되지 않아 Slack 알림을 건너뜁니다.", file=sys.stderr)
        return exit_code

    report_path = latest_report_path()
    report = load_report(report_path) if report_path else None
    if not report:
        print("생성된 정확도 리포트를 찾지 못했습니다.", file=sys.stderr)
        return exit_code

    note_path = write_obsidian_note(report_path, report) if report_path else None
    if note_path:
        print(f"Obsidian note: {note_path}")

    # 통과(exit_code == 0)일 때는 조용히 넘어가고, 확인이 필요할 때만 알려서
    # 알림 피로도를 줄인다.
    if exit_code != 0:
        notify_slack(webhook_url, report, exit_code)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
