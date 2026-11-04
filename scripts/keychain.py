"""
keychain.py — Lettura dei token dall'ambiente o dal Portachiavi macOS

I token (PR, notifiche) non stanno mai nei file del progetto: la
configurazione ne indica solo la provenienza. Il valore resta in memoria nel
processo che lo usa e non viene mai stampato né salvato.
"""

import os
import subprocess
import sys


def read_token(env: str = None, keychain_service: str = None, keychain_account: str = None):
    """Valore della variabile d'ambiente `env` o della voce `keychain_service` del Portachiavi; None se assente."""
    if env and os.environ.get(env):
        return os.environ[env]
    if keychain_service and sys.platform == "darwin":
        argv = ["security", "find-generic-password", "-s", keychain_service, "-w"]
        if keychain_account:
            argv[3:3] = ["-a", keychain_account]
        r = subprocess.run(argv, capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None
