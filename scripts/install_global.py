#!/usr/bin/env python3
"""Install shared files; per-user plugin and service activation are separate."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

PREFIX = Path("/opt/codex-autoname")
START = "# BEGIN codex-autoname managed installation"
END = "# END codex-autoname managed installation"
PLUGIN = "codex-auto-title@codex-autoname"
FILES = ["README.md", "config.example.json", ".codex-plugin/plugin.json", "requirements.txt",
         "hooks/hooks.json", "scripts/auto_title.py", "scripts/install_global.py",
         "systemd/codex-auto-title.service", "marketplace.example.json",
         "tests/fake_app_server.py", "tests/test_auto_title.py", "tests/test_install_global.py"]


def system_config(existing: str) -> str:
    """Preserve other settings and update only our explicitly marked block."""
    tomllib.loads(existing)
    if START in existing or END in existing:
        if existing.count(START) != 1 or existing.count(END) != 1:
            raise ValueError("Existing installation markers are incomplete or duplicated")
        before, rest = existing.split(START, 1)
        _, after = rest.split(END, 1)
        remaining = before + after
    else:
        remaining = existing
    settings = tomllib.loads(remaining)
    if "codex-autoname" in settings.get("marketplaces", {}) or PLUGIN in settings.get("plugins", {}):
        raise ValueError("Existing unmanaged codex-autoname settings need manual reconciliation")
    block = f'''{START}
[marketplaces.codex-autoname]
source_type = "local"
source = "/opt/codex-autoname"

[plugins."{PLUGIN}"]
enabled = false
{END}
'''
    result = remaining.rstrip() + "\n\n" + block if remaining.strip() else block
    tomllib.loads(result)
    return result


def service_unit() -> str:
    return '''[Unit]
Description=Generate Codex chat titles after responses and inactivity

[Service]
Type=simple
ExecStart=/opt/codex-autoname/.venv/bin/python /opt/codex-autoname/plugin/scripts/auto_title.py worker
Environment=PATH=/opt/codex/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=-%h/.config/codex-auto-title/worker.env
UMask=0077
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
'''


def publish(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    old_mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {path}")
    if path.exists() and path.read_text() == text:
        return
    if path.exists():
        backup = path.with_name(path.name + ".bak." + str(time.time_ns()))
        shutil.copy2(path, backup)
    fd, name = tempfile.mkstemp(prefix=".codex-autoname-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.chmod(name, old_mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def install(source: Path, staging_root: Path | None = None):
    def destination(path: Path) -> Path:
        return staging_root / path.relative_to("/") if staging_root else path

    prefix = destination(PREFIX)
    marker = prefix / ".codex-autoname-install.json"
    if prefix.is_symlink() or (prefix.exists() and not marker.is_file()):
        raise ValueError("Installation prefix already exists and is not managed by this installer")
    config_path = destination(Path("/etc/codex/config.toml"))
    existing = config_path.read_text() if config_path.exists() else ""
    merged = system_config(existing)
    for relative in FILES:
        src = source / relative
        if not src.is_file() or src.is_symlink():
            raise ValueError(f"Missing or symlinked source file: {relative}")
    prefix.mkdir(parents=True, exist_ok=True, mode=0o755)
    marker.write_text(json.dumps({"source": "codex-autoname", "format": 1}) + "\n")
    for relative in FILES:
        target = prefix / "plugin" / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if target.is_symlink():
            raise ValueError(f"Refusing to replace a symlink: {target}")
        shutil.copyfile(source / relative, target)
        target.chmod(0o644)
    # Codex 0.164.0 reads portable metadata but ignores its lifecycle hooks.
    # Remove only our old managed manifest so the supported compatibility
    # manifest is selected; keeping both would retain the broken precedence.
    old_manifest = prefix / "plugin/plugin.json"
    if old_manifest.exists():
        if old_manifest.is_symlink() or json.loads(old_manifest.read_text()).get("name") != "codex-auto-title":
            raise ValueError("Unexpected root plugin manifest; refusing to remove it")
        old_manifest.unlink()
    catalog = {"name": "codex-autoname", "interface": {"displayName": "Codex Auto Name"},
               "plugins": [{"name": "codex-auto-title", "source": {"source": "local", "path": "./plugin"},
                            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                            "category": "Productivity"}]}
    publish(prefix / ".agents/plugins/marketplace.json", json.dumps(catalog, indent=2) + "\n")
    if staging_root is None:
        subprocess.run([sys.executable, "-m", "venv", str(prefix / ".venv")], check=True)
        subprocess.run([str(prefix / ".venv/bin/python"), "-m", "pip", "install",
                        "-r", str(prefix / "plugin/requirements.txt")], check=True)
    publish(config_path, merged)
    publish(destination(Path("/etc/systemd/user/codex-auto-title.service")), service_unit())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--staging-root", type=Path,
                        help="Render and copy into this test root; do not install dependencies or activate services")
    parser.add_argument("--enable-linger", metavar="USER",
                        help="Allow this user's service manager to continue after logout")
    args = parser.parse_args(argv)
    if args.staging_root is None and os.geteuid() != 0:
        parser.error("Global installation writes /opt and /etc; run this command as root")
    if args.staging_root is not None and args.enable_linger:
        parser.error("--enable-linger cannot be used with --staging-root")
    if args.enable_linger:
        pwd.getpwnam(args.enable_linger)
    os.umask(0o022)
    install(args.source.resolve(), args.staging_root.resolve() if args.staging_root else None)
    if args.enable_linger:
        subprocess.run(["loginctl", "enable-linger", args.enable_linger], check=True)
    print("Shared plugin and user-service files installed. Plugin defaults to disabled for all users.")
    print("No user plugin configuration or service enablement was changed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
