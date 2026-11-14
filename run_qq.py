"""Start the official QQ transport with the current Python interpreter."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from social_bot.network_policy import direct_environment


def main():
    root = Path(__file__).resolve().parent
    node = shutil.which("node")
    if not node:
        raise SystemExit("请先安装 Node.js 20 或更高版本")
    if not (root / "qq/node_modules/@tencent-connect/qqbot-nodejs").exists():
        raise SystemExit("请先执行 npm --prefix qq ci")
    env = dict(direct_environment(), QQ_PYTHON=sys.executable)
    if "--check" in sys.argv:
        from social_bot.platforms.qq.worker import build_settings
        settings = build_settings(Path(env.get("QQ_CONFIG", root / "qq/config.json")), "check")
        with tempfile.TemporaryFile(dir=Path(settings.runtime.database_path).parent) as probe:
            probe.write(b"storage check")
        print("Python 配置、DeepSeek 密钥文件读取和记忆目录写入检查通过；未调用外部 API。", flush=True)
        return subprocess.call([node, str(root / "qq/index.mjs"), "--sdk-check"], cwd=root, env=env)
    try:
        return subprocess.call([node, str(root / "qq/index.mjs"), *sys.argv[1:]], cwd=root, env=env)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
