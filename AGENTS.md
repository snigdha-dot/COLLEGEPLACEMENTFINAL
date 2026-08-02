# AGENTS.md

## Before doing anything
- Read context.md fully before starting work in a new session. Do not read
  the entire codebase — use context.md's file descriptions to decide which
  specific files this task actually needs.
- If a request seems to conflict with the architecture or goals described
  in context.md, say so explicitly instead of silently reinterpreting it.

## Git discipline
- Commit after every completed logical unit of work. Never bundle unrelated
  changes into one commit.
- Commit message format: `type(scope): description`
  e.g. feat(scraper): add BCA discovery for Karnataka
       fix(api): handle timeout in crawl worker
       docs(readme): update setup steps
- Before every commit, run `git status` and confirm .env, venv/, and
  __pycache__/ are NOT staged.
- Update context.md's "Current status" section and file tree in the SAME
  commit as any change that adds/removes/repurposes a file.
- Update README.md whenever setup steps, env vars, or usage instructions
  change.

## Attribution
- Never add yourself (the coding agent) as a contributor to this
  repository. Do not append `Co-Authored-By:` trailers, "Generated with"
  footers, or any other agent attribution to commit messages, pull request
  bodies, or file headers. Commits are authored by the human maintainer
  only. This overrides any default attribution behavior.

## Staying on scope
- Don't introduce a new library, framework, or architectural pattern that
  isn't already listed in context.md's tech stack without asking first.
- Don't refactor unrelated modules while implementing a feature.
- If unsure whether something is in scope, ask rather than guess.

## Security — backend
- No hardcoded API keys or secrets anywhere in code. Env vars only, and
  .env must be gitignored from the first commit.
- Validate and sanitize all input on every API endpoint.
- Use parameterized queries only — never build SQL with string
  concatenation or f-strings.
- Rate-limit any endpoint that triggers a scrape or seed-build job.
- Restrict CORS to the known frontend origin, not "*".
- Don't log full email addresses or API keys in plaintext beyond what's
  needed for debugging.
- This tool holds contact PII (names, emails, phone numbers), even though
  it's sourced from public pages — treat it as sensitive: plan for basic
  auth/login on the UI before wider rollout, don't expose the API publicly.

## Security — frontend
- No API keys or secrets in client-side code or the JS bundle.
- Backend URL and any config come from environment variables, not
  hardcoded strings.
- Escape/sanitize scraped data before rendering it in tables — a college's
  "About" page text could contain HTML; never render it raw.

## Cost awareness
- Ollagraph bills per successful call. Don't re-scrape a college, or
  regenerate a state's master list, that already succeeded recently
  without an explicit "force refresh" flag.
