## 🐞 BUG REPRODUCTION PROTOCOL (this is a Bug-report task)

This task is a client-reported UI bug. Before you evaluate the acceptance
criteria, you MUST first reproduce the reported bug in a real browser and
capture evidence. You have the Playwright browser tools available
(`mcp__playwright__browser_*`). Follow these steps IN ORDER.

### Step 1 — Determine the target URL

- **Preferred:** if the `PREVIEW_URL` environment variable is set, use it as the
  base URL. Check it with: `echo "$PREVIEW_URL"`. A running preview is already
  serving the app there — do NOT start another server. **Verify it responds
  before using it** (the preview may have stopped since the build started):
  `curl -s -o /dev/null -w "%{http_code}" "$PREVIEW_URL"` — any HTTP status code
  (2xx/3xx/4xx) means the server is alive; connection refused / `000` means it
  is dead, so fall back to starting the app locally as below.
- **Fallback:** if `PREVIEW_URL` is empty or not responding, start the app yourself (e.g.
  `./init.sh`, `npm run dev`, framework dev server) and use its local URL. Only
  fall back to a hardcoded `http://localhost:3000` if nothing else is available.

### Step 2 — Reproduce the bug (capture BEFORE evidence)

1. `mcp__playwright__browser_navigate` to the target URL (+ the relevant route).
2. Follow the client's **Steps to reproduce** exactly (see CLIENT BUG REPORT
   above and any CLIENT-ATTACHED SCREENSHOTS). Use `browser_click`,
   `browser_fill_form`, `browser_select_option`, `browser_hover`,
   `browser_press_key`, `browser_wait_for` as needed.
3. When you reach the state where the bug manifests, capture a screenshot with
   `mcp__playwright__browser_take_screenshot`. If the tool accepts a `filename`
   (and/or a `type: png`) parameter, save it as
   `<spec_dir>/evidence/before-<n>.png`. If it cannot write to disk directly,
   take the screenshot inline and then use the Write/Bash tools to copy/save the
   captured image into `<spec_dir>/evidence/` — otherwise describe precisely what
   you observed in the report.
4. Collect console output with `mcp__playwright__browser_console_messages` and
   note any errors/warnings/failed network requests.

Create the `<spec_dir>/evidence/` directory first (Bash: `mkdir -p`) so the
screenshots have a home.

### Step 3 — Write the reproduction report

Write `<spec_dir>/reproduction_report.md` using the Write tool with EXACTLY these
sections (this is a contract other tooling relies on):

```markdown
# Reproduction Report

## Client report
<the client's steps / expected / actual, summarized>

## Reproduction steps performed
1. <what you actually did in the browser>
2. ...

## Observed result
<what happened; reference screenshots by relative path, e.g. evidence/before-1.png>

## Console errors
<console errors/warnings, or "None">

## Root-cause hypothesis
<your best hypothesis for what in the code causes this>
```

- If you CANNOT reproduce the bug, still write the report: document what you
  tried under "Reproduction steps performed" and state clearly under "Observed
  result" that the bug did not reproduce, with your reasoning.

### Step 4 — Evaluate acceptance criteria as usual

Only after the reproduction report exists, continue with the normal QA
validation of the acceptance criteria (tests, browser verification, etc.). When
you have verified the fix in a real browser, set `browser_verified: true` in the
`qa_signoff` object of `implementation_plan.json` (in addition to the usual
`status`/`issues_found` fields).
