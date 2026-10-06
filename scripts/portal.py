"""
portal.py — Portale web locale delle daily review

Interfaccia navigabile su tutti i report raccolti: dashboard (una di default
più quelle personalizzate), storico commit e review, report per giorno,
git-flow dei repository, timeline dei rilasci e coda KB.

    python3 scripts/portal.py            # http://127.0.0.1:8765
    python3 scripts/portal.py --port 9000 --no-browser

I dati vengono dalla base dati locale (`review_db.py`, `data/review.db`),
alimentata dalle routine; a ogni richiesta del dataset il portale fa un
import incrementale dei file cambiati sotto `reports/` e della coda KB.
Git-flow e rilasci si leggono dal vivo dai cloni locali (mai `fetch`).

Solo locale: ascolta su 127.0.0.1, nessun CDN, nessuna chiamata esterna — i
dati sono del cliente. Sola lettura su report, KB e repository; l'unica
scrittura dell'utente sono le dashboard personalizzate, salvate nel DB.
"""

import argparse
import http.server
import json
import re
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))

import portal_data as pd  # noqa: E402
import review_db as db  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT_DIR / "templates" / "portal.html"
MAX_BODY = 512 * 1024
SYNC_INTERVAL = 5  # secondi minimi tra due scansioni dei file

_cache = {"version": None, "data": None, "synced": 0.0}
_cache_lock = threading.Lock()


def get_dataset() -> dict:
    with _cache_lock:
        if time.monotonic() - _cache["synced"] > SYNC_INTERVAL:
            db.sync()
            _cache["synced"] = time.monotonic()
        version = db.data_version()
        if _cache["version"] != version:
            _cache["data"] = db.load_dataset()
            _cache["version"] = version
        return _cache["data"]


class PortalHandler(http.server.BaseHTTPRequestHandler):
    server_version = "GDRPortal/1.0"

    # Solo richieste verso l'host locale (difesa da DNS rebinding).
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self.send_error(403)
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path

        if path in ("/", "/index.html"):
            return self._send_file(TEMPLATE, "text/html; charset=utf-8")
        if path == "/api/dataset":
            return self._json(get_dataset())
        m = re.fullmatch(r"/api/day/(\d{4}-\d{2}-\d{2})", path)
        if m:
            return self._json(db.day_documents(m.group(1)))
        m = re.fullmatch(r"/reports/(\d{4}-\d{2}-\d{2})/dashboard\.html", path)
        if m:
            return self._send_file(db.REPORTS_DIR / m.group(1) / "dashboard.html",
                                   "text/html; charset=utf-8")
        if path == "/api/diff":
            diff = db.commit_diff(qs.get("repo", ""), qs.get("hash", ""))
            if not diff:
                diff = pd.git_show(db.repo_path(qs.get("repo", "")), qs.get("hash", ""))
            return self._json({"diff": diff})
        if path == "/api/gitgraph":
            rp = db.repo_path(qs.get("repo", ""))
            if not rp:
                return self._json({"error": "repository sconosciuto", "commits": [], "lanes": 0})
            limit = min(int(qs.get("limit", "800") or 800), 3000)
            return self._json(pd.git_graph(rp, qs.get("since", ""), qs.get("until", ""), limit))
        if path == "/api/releases":
            rp = db.repo_path(qs.get("repo", ""))
            return self._json({"tags": pd.release_timeline(rp) if rp else []})
        if path == "/api/kb":
            return self._json({"suggestions": db.kb_suggestions()})
        if path == "/api/dashboards":
            return self._json({"dashboards": db.load_dashboards()})
        return self.send_error(404)

    def do_PUT(self):
        if not self._host_ok():
            return self.send_error(403)
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return self.send_error(403)
        if urlparse(self.path).path != "/api/dashboards":
            return self.send_error(404)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self.send_error(413)
        try:
            body = json.loads(self.rfile.read(length))
            dashboards = body["dashboards"]
            assert isinstance(dashboards, list)
        except (ValueError, KeyError, AssertionError):
            return self.send_error(400)
        db.save_dashboards(dashboards)
        return self._json({"ok": True})

    def _send_file(self, path: Path, ctype: str):
        if not path.exists():
            return self.send_error(404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="Portale web locale delle daily review")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    res = db.sync()
    _cache["synced"] = time.monotonic()
    st = db.stats()
    print(f"🗄  Base dati: {st['db']}")
    print(f"   {st['days']} giorni ({st['from']} → {st['to']}), {st['commits']} commit, "
          f"{st['reviews']} review · importati ora: {len(res['days'])} giorni")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), PortalHandler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"🌐 Portale daily review: {url}")
    print("   Ctrl+C per fermare\n")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Portale fermato.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
