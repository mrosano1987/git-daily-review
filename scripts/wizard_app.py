"""
wizard_app.py — Web-based setup wizard for Git Daily Review.

Serves a single-page web app that walks the user through configuration.
On submit, generates config.yaml and knowledge-base.yaml.
"""

import http.server
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import webbrowser
import yaml
from pathlib import Path
from urllib.parse import parse_qs

ROOT_DIR = ""  # Set by run_wizard()


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def detect_git_repos(scan_dir: str, max_depth: int = 3) -> list[dict]:
    """Scan a directory for git repositories."""
    repos = []
    scan_path = Path(os.path.expanduser(scan_dir))
    if not scan_path.exists():
        return repos

    for depth in range(1, max_depth + 1):
        pattern = "/".join(["*"] * depth) + "/.git"
        for git_dir in scan_path.glob(pattern):
            repo_path = git_dir.parent
            try:
                # Get remote URL
                result = subprocess.run(
                    ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
                    capture_output=True, text=True, timeout=5
                )
                remote_url = result.stdout.strip() if result.returncode == 0 else ""

                # Get current branch
                result = subprocess.run(
                    ["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, timeout=5
                )
                current_branch = result.stdout.strip() if result.returncode == 0 else ""

                # Detect language/framework from files
                tech_stack = detect_tech_stack(str(repo_path))

                repos.append({
                    "path": str(repo_path),
                    "name": repo_path.name,
                    "remote_url": remote_url,
                    "current_branch": current_branch,
                    "tech_stack": tech_stack,
                })
            except Exception:
                continue

    return repos


def detect_tech_stack(repo_path: str) -> dict:
    """Auto-detect the tech stack of a repository."""
    path = Path(repo_path)
    stack = {"languages": [], "frameworks": [], "tools": []}

    markers = {
        "package.json":    {"lang": "TypeScript/JavaScript"},
        "tsconfig.json":   {"lang": "TypeScript"},
        "angular.json":    {"fw": "Angular"},
        "next.config.js":  {"fw": "Next.js"},
        "next.config.ts":  {"fw": "Next.js"},
        "nuxt.config.ts":  {"fw": "Nuxt"},
        "vite.config.ts":  {"fw": "Vite"},
        "vue.config.js":   {"fw": "Vue"},
        "capacitor.config.ts": {"fw": "Capacitor"},
        "capacitor.config.json": {"fw": "Capacitor"},
        "requirements.txt":{"lang": "Python"},
        "pyproject.toml":  {"lang": "Python"},
        "setup.py":        {"lang": "Python"},
        "Pipfile":         {"lang": "Python"},
        "manage.py":       {"fw": "Django"},
        "pom.xml":         {"lang": "Java", "fw": "Maven"},
        "build.gradle":    {"lang": "Java/Kotlin", "fw": "Gradle"},
        "build.gradle.kts":{"lang": "Kotlin", "fw": "Gradle"},
        "go.mod":          {"lang": "Go"},
        "Cargo.toml":      {"lang": "Rust"},
        "Gemfile":         {"lang": "Ruby"},
        "composer.json":   {"lang": "PHP"},
        "Package.swift":   {"lang": "Swift"},
        "*.csproj":        {"lang": "C#"},
        "*.sln":           {"lang": "C#", "fw": ".NET"},
        "Dockerfile":      {"tool": "Docker"},
        "docker-compose.yml": {"tool": "Docker Compose"},
        ".github/workflows": {"tool": "GitHub Actions"},
        "Jenkinsfile":     {"tool": "Jenkins"},
        ".gitlab-ci.yml":  {"tool": "GitLab CI"},
        "bitbucket-pipelines.yml": {"tool": "Bitbucket Pipelines"},
    }

    for marker, info in markers.items():
        if "*" in marker:
            if list(path.glob(marker)):
                _add_tech(stack, info)
        elif (path / marker).exists():
            _add_tech(stack, info)

    return stack


def _add_tech(stack, info):
    if "lang" in info and info["lang"] not in stack["languages"]:
        stack["languages"].append(info["lang"])
    if "fw" in info and info["fw"] not in stack["frameworks"]:
        stack["frameworks"].append(info["fw"])
    if "tool" in info and info["tool"] not in stack["tools"]:
        stack["tools"].append(info["tool"])


def generate_config(data: dict) -> str:
    """Generate config.yaml from wizard data."""
    config = {
        "repositories": {},
        "schedule": {
            "hour": int(data.get("schedule_hour", 17)),
            "minute": int(data.get("schedule_minute", 0)),
            "timezone": data.get("timezone", "UTC"),
        },
        "ai": {
            "provider":    data.get("ai_provider", "anthropic"),
            "model":       data.get("ai_model", ""),
            "api_key_env": _provider_env_var(data.get("ai_provider", "anthropic")),
            "base_url":    data.get("ai_base_url", ""),
            "max_tokens":  4096,
            "min_diff_lines_for_review": int(data.get("min_diff_lines", 5)),
        },
        "reports": {
            "output_dir": "./reports",
            "trend_days": 7,
            "language": data.get("report_language", "en"),
        },
        "notifications": {
            "slack": {"enabled": False, "webhook_url": "", "channel": ""},
            "email": {"enabled": False},
        },
    }

    # RAG service — only write the key if user provided a URL
    rag_url = data.get("rag_url", "").strip()
    if rag_url:
        config["rag_url"] = rag_url

    repos = data.get("repositories", [])
    for i, repo in enumerate(repos):
        key = repo.get("key", f"repo_{i}")
        config["repositories"][key] = {
            "name": repo.get("name", key),
            "path": repo.get("path", ""),
            "remote": repo.get("remote", "origin"),
            "branches": [],
            "exclude_branches": repo.get("exclude_branches", ["archive/*"]),
        }

    return yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _clean_text(value: str) -> str:
    """
    Normalize free-text coming from the wizard frontend: collapse literal
    escaped newlines ("\\n") that some browsers/serializers produce, and
    strip surrounding whitespace. Prevents corrupted multi-line YAML strings.
    """
    if not isinstance(value, str):
        return value
    return value.replace("\\n", "\n").replace("\r\n", "\n").strip()


def _dedupe(items: list) -> list:
    """Order-preserving deduplication for tech-stack lists."""
    return list(dict.fromkeys(items or []))


def generate_knowledge_base(data: dict) -> str:
    """Generate knowledge-base.yaml from wizard data."""
    kb = {
        "project": {
            "name": _clean_text(data.get("project_name", "My Project")),
            "description": _clean_text(data.get("project_description", "")),
            "team": {
                "structure": data.get("team_structure", ""),
            },
        },
        "architecture": {
            "repositories": {},
            "patterns": [],
        },
        "conventions": {},
        "quality_gates": {
            "critical": [],
            "errors": [],
            "warnings": [],
            "info": [],
        },
        "domain": {
            "description": _clean_text(data.get("domain_description", "")),
            "business_rules": [],
        },
        "integrations": [],
        "release": {
            "cadence": data.get("release_cadence", ""),
            "platforms": {},
        },
        "metadata": {
            "version": "1.0.0",
            "last_updated": "",
            "changelog": [],
        },
    }

    # Populate architecture from repos
    repos = data.get("repositories", [])
    for repo in repos:
        key = repo.get("key", repo.get("name", "repo"))
        tech_stack = dict(repo.get("tech_stack", {}) or {})
        for list_key in ("languages", "frameworks", "tools"):
            if list_key in tech_stack:
                tech_stack[list_key] = _dedupe(tech_stack[list_key])
        kb["architecture"]["repositories"][key] = {
            "name": repo.get("name", ""),
            "description": _clean_text(repo.get("description", "")),
            "tech_stack": tech_stack,
        }

    # Populate conventions from selected presets
    presets = data.get("convention_presets", [])
    for preset in presets:
        conventions = get_convention_preset(preset)
        kb["conventions"].update(conventions)

    # Populate quality gates from selected gates
    selected_gates = data.get("quality_gates", {})
    for severity in ("critical", "errors", "warnings", "info"):
        kb["quality_gates"][severity] = selected_gates.get(severity, [])

    # Custom conventions
    custom_conv = data.get("custom_conventions", [])
    if custom_conv:
        kb["conventions"]["project_specific"] = {
            "rules": custom_conv
        }

    return yaml.dump(kb, default_flow_style=False, allow_unicode=True, sort_keys=False)


def get_convention_preset(preset_name: str) -> dict:
    """Return convention rules for a known tech stack preset."""
    presets = {
        "typescript": {
            "typescript": {
                "rules": [
                    "Avoid explicit `any` — use specific types or generics",
                    "Use interfaces for data shapes, types for unions",
                    "Prefer optional chaining (?.) over verbose null checks",
                    "Use readonly where possible",
                    "Strict mode enabled",
                ]
            }
        },
        "angular": {
            "angular": {
                "rules": [
                    "OnPush change detection where possible",
                    "async pipe for RxJS subscriptions in templates",
                    "takeUntil + Subject for manual subscriptions",
                    "Smart (container) vs dumb (presentation) components",
                    "Lazy loading for feature modules",
                    "FormBuilder for reactive forms",
                ]
            },
            "rxjs": {
                "rules": [
                    "Every subscribe() MUST have an unsubscribe mechanism",
                    "Prefer declarative operators (map, filter, switchMap)",
                    "switchMap for cancellable HTTP requests",
                    "exhaustMap for form submissions",
                    "catchError with fallback — never empty error handlers",
                ]
            },
        },
        "react": {
            "react": {
                "rules": [
                    "Functional components with hooks",
                    "Custom hooks for shared logic",
                    "useMemo/useCallback for expensive computations",
                    "Avoid prop drilling — use context or state management",
                    "Error boundaries for graceful failure",
                ]
            }
        },
        "python": {
            "python": {
                "rules": [
                    "Type hints for function signatures",
                    "Docstrings for public functions (Google or NumPy style)",
                    "No bare except clauses",
                    "Use pathlib over os.path",
                    "F-strings for string formatting",
                    "Context managers for resource handling",
                ]
            }
        },
        "java": {
            "java": {
                "rules": [
                    "Follow naming conventions (camelCase methods, PascalCase classes)",
                    "Use Optional instead of null returns",
                    "Prefer immutable objects",
                    "Close resources with try-with-resources",
                    "Document public API with Javadoc",
                ]
            }
        },
        "go": {
            "go": {
                "rules": [
                    "Error handling — always check returned errors",
                    "Short variable names for small scopes",
                    "Interfaces accepted, structs returned",
                    "Use context.Context for cancellation",
                    "Run go vet and golint",
                ]
            }
        },
        "mobile_capacitor": {
            "capacitor": {
                "rules": [
                    "Platform check: Capacitor.isNativePlatform()",
                    "Plugin wrappers in dedicated services",
                    "Web fallback for every native feature",
                    "Runtime permission handling",
                ]
            }
        },
        "mobile_react_native": {
            "react_native": {
                "rules": [
                    "Platform-specific code via Platform.OS or .ios.ts/.android.ts",
                    "Native modules wrapped in JS services",
                    "FlatList for long lists (not ScrollView)",
                    "Avoid inline styles — use StyleSheet.create",
                ]
            }
        },
        "dotnet": {
            "dotnet": {
                "rules": [
                    "Use async/await for I/O operations",
                    "Dependency injection via constructor",
                    "IDisposable pattern for resource cleanup",
                    "Nullable reference types enabled",
                    "Use LINQ for collection operations",
                ]
            }
        },
    }
    return presets.get(preset_name, {})


# ── Quality Gate Presets ──

UNIVERSAL_GATES = {
    "critical": [
        {
            "id": "SEC-001",
            "name": "No hardcoded secrets",
            "description": "No secrets, tokens, passwords, or API keys in code",
            "check_type": "pattern",
            "patterns": ["password\\s*[=:]", "secret\\s*[=:]", "api[_-]?key\\s*[=:]",
                         "Bearer\\s+", "BEGIN (PRIVATE KEY|CERTIFICATE)"],
        },
        {
            "id": "SEC-002",
            "name": "No hardcoded URLs",
            "description": "API URLs must use environment/config, not hardcoded strings",
            "check_type": "pattern",
            "patterns": ["https?://[a-zA-Z]"],
            "exclude_files": ["*.spec.*", "*.test.*", "*.md", "environment*", "config*"],
        },
    ],
    "errors": [
        {
            "id": "GIT-003",
            "name": "No direct commits on protected branches",
            "description": "No direct commits on main/master/develop",
            "check_type": "git_metadata",
            "protected_branches": ["main", "master", "develop"],
        },
    ],
    "warnings": [
        {
            "id": "GIT-001",
            "name": "Vague commit message",
            "description": "Commit messages must be descriptive (>10 chars, no generic words)",
            "check_type": "git_metadata",
            "min_length": 10,
            "bad_patterns": ["^fix$", "^update$", "^wip$", "^test$", "^\\.$"],
        },
        {
            "id": "GIT-002",
            "name": "Branch naming convention",
            "description": "Branches should follow feature/|bugfix/|hotfix/ pattern",
            "check_type": "git_metadata",
            "allowed_prefixes": ["feature/", "bugfix/", "hotfix/", "release/",
                                 "develop", "main", "master"],
        },
    ],
    "info": [
        {
            "id": "DOC-001",
            "name": "Missing documentation",
            "description": "New public APIs or services should have documentation",
            "check_type": "heuristic",
        },
        {
            "id": "TEST-001",
            "name": "Missing tests for new code",
            "description": "New files should have corresponding test files",
            "check_type": "heuristic",
        },
    ],
}

LANGUAGE_GATES = {
    "typescript": {
        "errors": [
            {"id": "TS-001", "name": "No explicit any",
             "description": "Avoid explicit 'any' type"},
            {"id": "TS-002", "name": "No console.log in production",
             "description": "Remove console.log from production code"},
        ],
    },
    "python": {
        "errors": [
            {"id": "PY-001", "name": "No bare except",
             "description": "Use specific exception types, not bare except"},
            {"id": "PY-002", "name": "No print in production",
             "description": "Use logging module, not print()"},
        ],
    },
    "java": {
        "errors": [
            {"id": "JV-001", "name": "No System.out.println",
             "description": "Use a logging framework, not System.out"},
            {"id": "JV-002", "name": "Null check required",
             "description": "Use Optional or null checks for nullable references"},
        ],
    },
    "go": {
        "errors": [
            {"id": "GO-001", "name": "Unchecked error",
             "description": "All returned errors must be checked"},
            {"id": "GO-002", "name": "No fmt.Println in production",
             "description": "Use a structured logger, not fmt.Println"},
        ],
    },
}


# ── HTTP Request Handler ──

class WizardHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/":
            self.send_html()
        elif self.path == "/api/detect-os":
            self.send_json({"os": platform.system(), "home": str(Path.home())})
        elif self.path.startswith("/api/scan-repos"):
            qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            scan_dir = qs.get("dir", [str(Path.home())])[0]
            repos = detect_git_repos(scan_dir)
            self.send_json({"repos": repos})
        elif self.path == "/api/presets":
            self.send_json({
                "conventions": list(get_convention_preset("typescript").keys())
                               + ["angular", "react", "python", "java", "go",
                                  "mobile_capacitor", "mobile_react_native", "dotnet"],
                "gates": {"universal": UNIVERSAL_GATES, "language": LANGUAGE_GATES},
            })
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/save":
            content_length = int(self.headers["Content-Length"])
            body = self.rfile.read(content_length)
            data = json.loads(body)
            result = save_configuration(data)
            self.send_json(result)
        elif self.path == "/api/test-api-key":
            content_length = int(self.headers["Content-Length"])
            body = self.rfile.read(content_length)
            data = json.loads(body)
            result = test_api_key(data.get("key", ""))
            self.send_json(result)
        elif self.path == "/api/ollama-models":
            content_length = int(self.headers["Content-Length"])
            body = self.rfile.read(content_length)
            data = json.loads(body)
            result = get_ollama_models(data.get("url", "http://localhost:11434"))
            self.send_json(result)
        elif self.path == "/api/test-provider":
            content_length = int(self.headers["Content-Length"])
            body = self.rfile.read(content_length)
            data = json.loads(body)
            result = test_provider_connection(data)
            self.send_json(result)
        elif self.path == "/api/test-rag":
            content_length = int(self.headers["Content-Length"])
            body = self.rfile.read(content_length)
            data = json.loads(body)
            result = test_rag_service(data.get("url", ""))
            self.send_json(result)
        elif self.path == "/api/shutdown":
            self.send_json({"ok": True})
            threading.Thread(target=self.server.shutdown).start()
        else:
            self.send_error(404)

    def send_html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html_path = Path(__file__).parent.parent / "templates" / "wizard.html"
        if html_path.exists():
            self.wfile.write(html_path.read_bytes())
        else:
            self.wfile.write(b"<h1>Wizard template not found</h1>")

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logging


def _provider_env_var(provider: str) -> str:
    """Return the conventional env var name for a provider's API key."""
    return {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai":    "OPENAI_API_KEY",
        "gemini":    "GEMINI_API_KEY",
        "ollama":    "",
    }.get(provider, "ANTHROPIC_API_KEY")


def get_ollama_models(base_url: str) -> dict:
    """Fetch the list of locally available models from an Ollama instance."""
    import urllib.request
    import urllib.error
    try:
        url = base_url.rstrip("/") + "/api/tags"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
            models = [m["name"] for m in body.get("models", [])]
            return {"ok": True, "models": models}
    except urllib.error.URLError as e:
        return {"ok": False, "models": [], "message": str(e.reason)}
    except Exception as e:
        return {"ok": False, "models": [], "message": str(e)}


def test_rag_service(url: str) -> dict:
    """Check if the RAG service /health endpoint is reachable."""
    import urllib.request
    import urllib.error
    if not url:
        return {"ok": False, "message": "No URL provided"}
    try:
        health_url = url.rstrip("/") + "/health"
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                return {"ok": True}
            return {"ok": False, "message": f"HTTP {resp.status}"}
    except urllib.error.URLError as e:
        return {"ok": False, "message": str(e.reason)}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def test_provider_connection(data: dict) -> dict:
    """Test a provider config from the wizard by attempting a real completion."""
    import sys
    import os
    # Temporarily set the key in env so create_provider can find it
    provider_name = data.get("provider", "anthropic")
    api_key  = data.get("api_key", "").strip()
    base_url = data.get("base_url", "").strip()
    model    = data.get("model", "")

    env_var  = _provider_env_var(provider_name)
    original = os.environ.get(env_var, "")
    if api_key and env_var:
        os.environ[env_var] = api_key

    try:
        # Import from scripts dir
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from ai_provider import test_provider
        cfg = {
            "provider":    provider_name,
            "model":       model,
            "base_url":    base_url,
            "api_key_env": env_var,
            "max_tokens":  64,
        }
        return test_provider(cfg)
    except Exception as e:
        return {"ok": False, "message": str(e)}
    finally:
        # Restore original env
        if env_var:
            if original:
                os.environ[env_var] = original
            elif env_var in os.environ:
                del os.environ[env_var]


def test_api_key(key: str) -> dict:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "Say OK"}]
        )
        return {"valid": True, "message": "API key is valid"}
    except Exception as e:
        return {"valid": False, "message": str(e)}


def save_configuration(data: dict) -> dict:
    """Save generated config files."""
    config_dir = os.path.join(ROOT_DIR, "config")
    os.makedirs(config_dir, exist_ok=True)

    try:
        # Generate and save config.yaml
        config_yaml = generate_config(data)
        config_path = os.path.join(config_dir, "config.yaml")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(config_yaml)

        # Generate and save knowledge-base.yaml
        kb_yaml = generate_knowledge_base(data)
        kb_path = os.path.join(config_dir, "knowledge-base.yaml")
        with open(kb_path, "w", encoding="utf-8") as f:
            f.write(kb_yaml)

        # API key env var instructions (provider-aware)
        api_key  = data.get("ai_api_key", "")
        provider = data.get("ai_provider", "anthropic")
        env_var  = _provider_env_var(provider)
        env_instructions = ""
        if api_key and env_var:
            system = platform.system()
            if system == "Windows":
                env_instructions = (
                    f'setx {env_var} "{api_key}"\n'
                    "Then restart your terminal."
                )
            else:
                shell_rc = "~/.zshrc" if system == "Darwin" else "~/.bashrc"
                env_instructions = (
                    f"echo 'export {env_var}=\"{api_key}\"' >> {shell_rc}\n"
                    f"source {shell_rc}"
                )
        elif not env_var:
            env_instructions = "# Ollama: no API key needed.\nollama serve"

        return {
            "success": True,
            "config_path": config_path,
            "kb_path": kb_path,
            "env_instructions": env_instructions,
            "rag_enabled": bool(data.get("rag_url", "").strip()),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def run_wizard(root_dir: str):
    """Start the wizard web server and open the browser."""
    global ROOT_DIR
    ROOT_DIR = root_dir

    port = find_free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), WizardHandler)

    url = f"http://127.0.0.1:{port}"
    print(f"🌐 Setup wizard running at {url}")
    print(f"   Press Ctrl+C to stop\n")

    # Open browser after a short delay
    def open_browser():
        import time
        time.sleep(0.5)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Wizard stopped.")
    finally:
        server.server_close()