"""多端联测前的本地检查；不启动监听，不调用模型，不发送消息。"""
import importlib.metadata
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from social_bot.settings import load_settings

ROOT = Path(__file__).resolve().parent


def main():
    os.chdir(ROOT)
    report_dir = ROOT / "reports"
    report_dir.mkdir(exist_ok=True)
    settings = load_settings(ROOT / "config.json")
    db_path = Path(settings.runtime.database_path).resolve()
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        groups = db.execute("SELECT COUNT(*) FROM messages WHERE session_id LIKE 'wechat:group:%'").fetchone()[0]
    db.close()
    packages = {}
    for package in ("wxautox4", "wxauto4", "playwright"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    allowed = settings.scope.groups | settings.scope.private_users
    missing_targets = len(allowed - set(settings.wechat.targets))
    qq = json.loads((ROOT / "qq/config.json").read_text(encoding="utf-8-sig"))
    douyin = json.loads((ROOT / "douyin/config.json").read_text(encoding="utf-8-sig"))
    report = {"wechat": {"database": str(db_path), "integrity": integrity,
                "group_messages": groups, "group_memory_enabled": settings.runtime.wechat_group_memory_enabled,
                "enabled": settings.wechat.enabled, "target_count": len(settings.wechat.targets),
                "allowed_without_listener": missing_targets},
              "packages": packages,
              "qq": {"private_allowlist_count": len(qq["private_users"]), "group_allowlist_count": len(qq["groups"])},
              "douyin": {"enabled": douyin["enabled"], "peer_count": len(douyin["peers"])}, "checks": {}}
    commands = {
        "python_tests": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        "qq_config_sdk": [sys.executable, "run_qq.py", "--check"],
        "douyin_config": [sys.executable, "run_douyin.py", "check"],
        "bilibili_config": [sys.executable, "run_bilibili.py", "check"],
        "qq_tests": ["node", "--test", "qq/bridge.test.mjs", "qq/decode-voice.test.mjs", "qq/memory-pool.test.mjs"],
        "dependencies": [sys.executable, "-m", "pip", "check"],
    }
    for name, command in commands.items():
        try:
            result = subprocess.run(command, cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                                    capture_output=True, encoding="utf-8", errors="replace",
                                    timeout=120 if name == "python_tests" else 60)
            (report_dir / (name + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
            report["checks"][name] = {"passed": result.returncode == 0, "exit_code": result.returncode}
        except (OSError, subprocess.TimeoutExpired) as exc:
            report["checks"][name] = {"passed": False, "error": str(exc)}
        print(name + ": " + ("PASS" if report["checks"][name]["passed"] else "FAIL"), flush=True)
    path = report_dir / "preflight.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("检查报告：" + str(path))
    return 0 if integrity == "ok" and not missing_targets and all(c["passed"] for c in report["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
