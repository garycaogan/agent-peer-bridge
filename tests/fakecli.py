"""A fake agent CLI the executor can launch on any OS: an executable script, or a .cmd shim on Windows (like npm installs)."""
import os
import sys
from pathlib import Path


def fake_cli(path, source):
    path = Path(path)
    if os.name != "nt":
        path.write_text(source, encoding="utf-8")
        path.chmod(0o700)
        return str(path)
    script = path.with_suffix(".py")
    script.write_text(source, encoding="utf-8")
    shim = path.with_suffix(".cmd")
    shim.write_text(f'@"{sys.executable}" "%~dp0{script.name}" %*\r\n', encoding="utf-8")
    return str(shim)
