#!/usr/bin/env python3
"""
setup.py — Entry point for Git Daily Review setup.

Launches the web-based setup wizard in your default browser.
The wizard walks you through:
  1. Repository configuration
  2. Project architecture & tech stack
  3. Quality gate selection
  4. Scheduling preferences
  5. API key configuration

Usage:
    python setup.py          # Launch wizard
    python setup.py --check  # Verify existing configuration
    python setup.py --reset  # Reset configuration
"""

import argparse
import os
import sys
import subprocess
import platform

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
CONFIG_DIR = os.path.join(ROOT_DIR, "config")


def check_python_version():
    if sys.version_info < (3, 10):
        print(f"❌ Python 3.10+ required (you have {sys.version})")
        sys.exit(1)


def install_dependencies():
    """Install required Python packages."""
    deps = ["anthropic", "pyyaml"]
    print("📦 Checking dependencies...")

    missing = []
    for dep in deps:
        try:
            __import__(dep.replace("-", "_"))
        except ImportError:
            missing.append(dep)

    if missing:
        print(f"   Installing: {', '.join(missing)}")
        cmd = [sys.executable, "-m", "pip", "install"] + missing
        # Handle externally-managed-environment (PEP 668 / Homebrew Python)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 and "externally-managed" in result.stderr:
            print("   ⚠️  System Python detected — creating virtual environment...")
            venv_path = os.path.join(ROOT_DIR, ".venv")
            subprocess.run([sys.executable, "-m", "venv", venv_path], check=True)
            # Determine venv python path
            if platform.system() == "Windows":
                venv_python = os.path.join(venv_path, "Scripts", "python.exe")
            else:
                venv_python = os.path.join(venv_path, "bin", "python")
            subprocess.run([venv_python, "-m", "pip", "install"] + deps, check=True)
            print(f"   ✅ Virtual environment created at .venv/")
            print(f"   Restarting setup with venv Python...")
            os.execv(venv_python, [venv_python, __file__] + sys.argv[1:])
        elif result.returncode != 0:
            print(f"   ❌ pip install failed: {result.stderr}")
            sys.exit(1)

    print("   ✅ All dependencies installed")


def check_config():
    """Verify existing configuration."""
    config_path = os.path.join(CONFIG_DIR, "config.yaml")
    kb_path = os.path.join(CONFIG_DIR, "knowledge-base.yaml")

    print("\n🔍 Configuration check:\n")

    # Config file
    if os.path.exists(config_path):
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        repos = cfg.get("repositories", {})
        print(f"  ✅ config.yaml found ({len(repos)} repositories)")
        for key, repo in repos.items():
            path = os.path.expanduser(repo.get("path", ""))
            exists = os.path.isdir(path)
            icon = "✅" if exists else "❌"
            print(f"     {icon} {repo.get('name', key)}: {path}")
    else:
        print(f"  ❌ config.yaml not found — run: python setup.py")

    # Knowledge base
    if os.path.exists(kb_path):
        print(f"  ✅ knowledge-base.yaml found")
    else:
        print(f"  ⚠️  knowledge-base.yaml not found (will use defaults)")

    # API key
    if os.environ.get("ANTHROPIC_API_KEY"):
        key = os.environ["ANTHROPIC_API_KEY"]
        print(f"  ✅ ANTHROPIC_API_KEY set ({key[:12]}...)")
    else:
        print(f"  ❌ ANTHROPIC_API_KEY not set")

    # Scheduling
    print()


def reset_config():
    """Remove generated configuration files."""
    import shutil
    for f in ["config.yaml", "knowledge-base.yaml"]:
        path = os.path.join(CONFIG_DIR, f)
        if os.path.exists(path):
            os.remove(path)
            print(f"  🗑️  Removed {f}")
    print("  ✅ Configuration reset. Run `python setup.py` to reconfigure.")


def launch_wizard():
    """Launch the web-based setup wizard."""
    sys.path.insert(0, SCRIPTS_DIR)
    from wizard_app import run_wizard
    run_wizard(ROOT_DIR)


def main():
    parser = argparse.ArgumentParser(description="Git Daily Review — Setup")
    parser.add_argument("--check", action="store_true", help="Verify configuration")
    parser.add_argument("--reset", action="store_true", help="Reset configuration")
    args = parser.parse_args()

    print()
    print("╔══════════════════════════════════════════╗")
    print("║     🔍 Git Daily Review — Setup          ║")
    print("╚══════════════════════════════════════════╝")
    print()

    check_python_version()

    if args.check:
        install_dependencies()
        check_config()
        return

    if args.reset:
        reset_config()
        return

    install_dependencies()
    os.makedirs(CONFIG_DIR, exist_ok=True)
    launch_wizard()


if __name__ == "__main__":
    main()
