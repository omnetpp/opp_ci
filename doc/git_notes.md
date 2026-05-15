# Git Notes Integration

opp_ci delivers test result summaries to developers as **git notes** attached
to each tested commit. Results appear locally in `git log` and lazygit after a
regular `git fetch`.

## How it works

1. opp_ci runs tests and stores results in its database.
2. When a batch of runs finishes for a repo, opp_ci triggers a
   `workflow_dispatch` on the repo's `ci-notes.yml` GitHub Action.
3. The Action fetches note data from `GET /api/notes/{owner}/{repo}`, writes
   git notes under `refs/notes/ci`, and pushes them.
4. Developers see the notes on their next `git fetch`.

## Developer setup (one-time)

Add the notes refspec so `git fetch` pulls CI notes automatically:

```sh
git config --add remote.origin.fetch "+refs/notes/ci:refs/notes/ci"
```

After this, every `git fetch` or `git pull` will update the local CI notes.

## Viewing notes

### git log

```sh
git log --notes=ci
```

Each tested commit will show a one-line summary like:

```
Notes (ci):
    ✅ smoke PASS | ❌ fingerprint 46 PASS, 2 FAIL | https://ci.omnetpp.org/runs/42
```

### lazygit

Add a custom command to `~/.config/lazygit/config.yml`:

```yaml
customCommands:
  - key: "N"
    context: "commits"
    command: "git notes --ref=ci show {{.SelectedLocalCommit.Hash}} 2>/dev/null || echo 'No CI results'"
    description: "Show CI results"
    showOutput: true
```

Press `N` on any commit to see its CI results.

## Repository setup

To enable notes delivery for a repository:

1. Copy `doc/ci-notes.yml` to `.github/workflows/ci-notes.yml` in the target
   repo.
2. Set these repository (or organization) secrets:
   - `OPP_CI_API_URL` — the opp_ci coordinator URL (e.g. `https://ci.omnetpp.org`)
   - `OPP_CI_API_TOKEN` — a readonly API token from opp_ci
3. Create a fine-grained GitHub PAT with **Actions: Write** permission for the
   target repo, and configure it as `OPP_CI_GITHUB_ACTIONS_TOKEN` on the
   opp_ci coordinator (env var or `~/.ssh/opp_ci_github_actions_token` file).

## Note format

Each note is a single line with pipe-separated sections:

```
<icon> <test_type> <summary> | <icon> <test_type> <summary> | <url>
```

Examples:
- `✅ smoke PASS | https://ci.omnetpp.org/runs/42`
- `✅ smoke PASS | ❌ fingerprint 46 PASS, 2 FAIL | https://ci.omnetpp.org/runs/42`
- `✅ smoke PASS | ✅ fingerprint 48/48 PASS | https://ci.omnetpp.org/runs/42`

## Configuration

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_GITHUB_ACTIONS_TOKEN` | *(empty)* | Fine-grained PAT with Actions:Write scope |
| `OPP_CI_GITHUB_ACTIONS_TOKEN_FILE` | `~/.ssh/opp_ci_github_actions_token` | File path to read the token from |

## Permission model

opp_ci does **not** push notes directly — it holds no `Contents: Write` token
for target repos. Instead it uses a minimal `Actions: Write` PAT to trigger the
`ci-notes.yml` workflow, which uses the built-in `GITHUB_TOKEN` to push notes.
This ensures opp_ci cannot modify branches, tags, or file contents.
