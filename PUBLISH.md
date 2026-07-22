# Publishing checklist (first push to GitHub)

Run these from the project root. Delete this file after publishing.

## 0. License (required before the first commit)

The canonical AGPL-3.0 text must be byte-exact, so download it from GNU:

```bash
curl -fsSL https://www.gnu.org/licenses/agpl-3.0.txt -o LICENSE
```

(Alternative: create the repo on github.com and pick "GNU AGPL v3.0" in the
license dropdown — then skip this step and pull before pushing.)

## 1. Sanity check — nothing personal/client-related goes public

```bash
# These must all return NOTHING:
grep -ri "edison" --exclude-dir=node_modules --exclude-dir=.git . || echo OK
ls config/config.yaml 2>/dev/null && echo "REMOVE config.yaml!" || echo OK
ls reports/*/ 2>/dev/null && echo "REMOVE reports!" || echo OK
cat .env 2>/dev/null && echo "REMOVE .env!" || echo OK
```

## 2. Build the MCP server once (verifies it compiles)

```bash
cd mcp && npm install && npm run build && cd ..
```

## 3. Init and push

```bash
git init -b main
git add .
git commit -m "Initial public release: AI daily code review with MCP server (AGPL-3.0)"

# With GitHub CLI:
gh repo create git-daily-review --public --source=. --push

# Or manually: create the repo on github.com, then:
# git remote add origin git@github.com:YOUR_USER/git-daily-review.git
# git push -u origin main
```

## 4. Repo settings (recommended)

- Description: "Automated daily AI code review for any Git repo — CLI, scheduler, and MCP server. AGPL-3.0."
- Topics: `code-review`, `mcp`, `mcp-server`, `ai`, `git`, `ollama`, `claude`, `developer-tools`
- Enable Issues; protect `main`.
