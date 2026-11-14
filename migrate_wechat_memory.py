"""在线备份既有 SQLite 历史，验证后更新微信运行配置；不读取微信客户端数据库。"""
import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path


def migrate(config_path: Path, destination: Path) -> dict:
    config_path, destination = config_path.resolve(), destination.resolve()
    original = config_path.read_bytes()
    config = json.loads(original.decode("utf-8-sig"))
    source = Path(config["runtime"]["database_path"])
    if not source.is_absolute():
        source = config_path.parent / source
    source = source.resolve()
    if source == destination:
        raise ValueError("配置已指向目标数据库，无需重复迁移")
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError("目标已存在，请先核对，迁移不会覆盖已有记忆库")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".migrating.sqlite3")
    if temporary.exists():
        raise FileExistsError(temporary)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = config_path.with_name(config_path.name + "." + stamp + ".bak")
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(temporary) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise RuntimeError("备份完整性检查失败")
                counts = dict(dst.execute("SELECT role, COUNT(*) FROM messages GROUP BY role"))
                group_messages = dst.execute("SELECT COUNT(*) FROM messages WHERE session_id LIKE 'wechat:group:%'").fetchone()[0]
                groups = dst.execute("SELECT COUNT(DISTINCT session_id) FROM messages WHERE session_id LIKE 'wechat:group:%'").fetchone()[0]
                states = dst.execute("SELECT COUNT(*) FROM group_states").fetchone()[0]
        # Windows 在 with 退出后仍可能持有连接；显式关闭才能重命名文件。
        src.close()
        dst.close()
        temporary.rename(destination)
        backup.write_bytes(original)
        config["runtime"]["database_path"] = destination.as_posix()
        config["runtime"]["wechat_group_memory_enabled"] = True
        updated = config_path.with_suffix(".migrating.json")
        updated.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(updated, config_path)
        return {"source": str(source), "destination": str(destination), "config_backup": str(backup),
                "messages": counts, "group_messages": group_messages, "group_sessions": groups,
                "group_states": states, "integrity_check": "ok"}
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=root / "config.json")
    parser.add_argument("--destination", type=Path, default=Path("E:/deepseek-chat-memory/wechat-default.sqlite3"))
    args = parser.parse_args()
    print(json.dumps(migrate(args.config, args.destination), ensure_ascii=False, indent=2))
