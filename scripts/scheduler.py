#!/usr/bin/env python3
"""
scheduler.py — Cross-platform scheduling for Git Daily Review.

Supports:
  - macOS: launchd (LaunchAgent plist)
  - Linux: cron (crontab entry)
  - Windows: Task Scheduler (schtasks)

Usage:
  python scheduler.py --install    Install scheduled task
  python scheduler.py --remove     Remove scheduled task
  python scheduler.py --status     Check if scheduled task exists
"""

import argparse
import os
import platform
import subprocess
import sys
import yaml
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "config.yaml"
LABEL = "com.git-daily-review"
TASK_NAME = "GitDailyReview"


def load_schedule() -> dict:
    if not CONFIG_PATH.exists():
        print("❌ config.yaml not found — run setup.py first")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("schedule", {"hour": 17, "minute": 0})


def get_python_path() -> str:
    """Return the Python executable path (prefers venv if active)."""
    return sys.executable


def get_review_script() -> str:
    return str(ROOT_DIR / "scripts" / "daily_review.py")


def _warn_if_env_missing():
    """
    Warn (without blocking) if the configured cloud provider has no API key
    available via the project's .env file. Secrets are never written into
    plist/crontab/bat — daily_review.py loads .env at startup.
    """
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return
    ai = cfg.get("ai", {})
    provider = ai.get("provider", "anthropic").lower()
    defaults = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    if provider not in defaults:
        return  # e.g. ollama: no key needed
    env_var = ai.get("api_key_env") or defaults[provider]

    env_file = ROOT_DIR / ".env"
    in_env_file = env_file.exists() and env_var in env_file.read_text(
        encoding="utf-8", errors="replace")
    if not in_env_file and not os.environ.get(env_var):
        print(f"⚠️  {env_var} not found in {env_file} (provider: {provider}).")
        print(f"   The scheduled run will fail until you create it:")
        print(f"     echo '{env_var}=your-key' >> {env_file} && chmod 600 {env_file}")


# ── macOS (launchd) ──

def macos_install(schedule: dict):
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    python_path = get_python_path()
    script_path = get_review_script()
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    _warn_if_env_missing()

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{script_path}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{ROOT_DIR}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{schedule['hour']}</integer>
        <key>Minute</key>
        <integer>{schedule['minute']}</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>{Path.home()}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_dir / 'daily-review.stdout.log'}</string>
    <key>StandardErrorPath</key>
    <string>{log_dir / 'daily-review.stderr.log'}</string>
</dict>
</plist>
"""
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(plist_path)],
                   capture_output=True)
    plist_path.write_text(plist)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    print(f"✅ LaunchAgent installed: {plist_path}")
    print(f"   Runs daily at {schedule['hour']:02d}:{schedule['minute']:02d}")
    print(f"   Test: launchctl start {LABEL}")


def macos_remove():
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    if plist_path.exists():
        plist_path.unlink()
    print("✅ LaunchAgent removed")


def macos_status():
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    if LABEL in result.stdout:
        print(f"✅ Scheduled (launchd: {LABEL})")
    else:
        print("❌ Not scheduled")


# ── Linux (cron) ──

def linux_install(schedule: dict):
    python_path = get_python_path()
    script_path = get_review_script()
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "daily-review.log"

    _warn_if_env_missing()

    cron_line = (
        f"{schedule['minute']} {schedule['hour']} * * * "
        f"cd {ROOT_DIR} && {python_path} {script_path} "
        f">> {log_path} 2>&1"
    )

    # Remove old entry, add new
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""
    filtered = "\n".join(
        l for l in existing.splitlines() if "daily_review.py" not in l
    )
    new_crontab = filtered.rstrip() + "\n" + cron_line + "\n"

    proc = subprocess.run(["crontab", "-"], input=new_crontab, text=True,
                          capture_output=True)
    if proc.returncode == 0:
        print(f"✅ Crontab installed")
        print(f"   Runs daily at {schedule['hour']:02d}:{schedule['minute']:02d}")
        print(f"   Verify: crontab -l | grep daily_review")
    else:
        print(f"❌ Failed: {proc.stderr}")


def linux_remove():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        print("✅ No crontab to clean")
        return
    filtered = "\n".join(
        l for l in result.stdout.splitlines() if "daily_review.py" not in l
    )
    subprocess.run(["crontab", "-"], input=filtered + "\n", text=True)
    print("✅ Crontab entry removed")


def linux_status():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode == 0 and "daily_review.py" in result.stdout:
        print("✅ Scheduled (crontab)")
    else:
        print("❌ Not scheduled")


# ── Windows (Task Scheduler) ──

def windows_install(schedule: dict):
    python_path = get_python_path()
    script_path = get_review_script()
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    time_str = f"{schedule['hour']:02d}:{schedule['minute']:02d}"

    # Create a wrapper batch file (logging only — secrets live in .env,
    # loaded by daily_review.py at startup)
    _warn_if_env_missing()
    bat_path = ROOT_DIR / "scripts" / "run_review.bat"
    bat_content = f"""@echo off
cd /d "{ROOT_DIR}"
"{python_path}" "{script_path}" >> "{log_dir / 'daily-review.log'}" 2>&1
"""
    bat_path.write_text(bat_content)

    # Remove existing task
    subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True
    )

    # Create new task
    result = subprocess.run([
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", str(bat_path),
        "/SC", "DAILY",
        "/ST", time_str,
        "/F"
    ], capture_output=True, text=True)

    if result.returncode == 0:
        print(f"✅ Scheduled task installed: {TASK_NAME}")
        print(f"   Runs daily at {time_str}")
        print(f"   Verify: schtasks /Query /TN {TASK_NAME}")
    else:
        print(f"❌ Failed: {result.stderr}")


def windows_remove():
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("✅ Scheduled task removed")
    else:
        print(f"ℹ️  {result.stderr.strip()}")
    # Clean up batch file
    bat_path = ROOT_DIR / "scripts" / "run_review.bat"
    if bat_path.exists():
        bat_path.unlink()


def windows_status():
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"✅ Scheduled (Task Scheduler: {TASK_NAME})")
    else:
        print("❌ Not scheduled")


# ── Dispatcher ──

def main():
    parser = argparse.ArgumentParser(description="Git Daily Review — Scheduler")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true", help="Install scheduled task")
    group.add_argument("--remove", action="store_true", help="Remove scheduled task")
    group.add_argument("--status", action="store_true", help="Check scheduling status")
    args = parser.parse_args()

    system = platform.system()

    if args.install:
        schedule = load_schedule()
        print(f"\n📅 Installing schedule ({system})...\n")
        if system == "Darwin":
            macos_install(schedule)
        elif system == "Linux":
            linux_install(schedule)
        elif system == "Windows":
            windows_install(schedule)
        else:
            print(f"❌ Unsupported OS: {system}")

    elif args.remove:
        print(f"\n🗑️  Removing schedule ({system})...\n")
        if system == "Darwin":
            macos_remove()
        elif system == "Linux":
            linux_remove()
        elif system == "Windows":
            windows_remove()

    elif args.status:
        print(f"\n📋 Schedule status ({system}):\n")
        if system == "Darwin":
            macos_status()
        elif system == "Linux":
            linux_status()
        elif system == "Windows":
            windows_status()


if __name__ == "__main__":
    main()
