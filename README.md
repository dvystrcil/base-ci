# base-ci

Shared CI guards for every `dvystrcil` repo, behind one thin caller per repo.
A new shared guard is added here once and reaches every repo when `v1` moves,
instead of being wired into each repo by hand.

Two reusable workflows (`on: workflow_call`):

| workflow | runs | what it does |
|---|---|---|
| [`base-ci.yaml`](.github/workflows/base-ci.yaml) | every PR and push | `gitleaks` via [`dvystrcil/secret-scan-action@v1`](https://github.com/dvystrcil/secret-scan-action), and on pushes to `main` the direct-push detector below |
| [`detect-bot-direct-push.yml`](.github/workflows/detect-bot-direct-push.yml) | pushes to `main` | fails the run when a pushed commit is in no merged PR and isn't one of the two known automated writers (a release-action `deploy:` commit or an argocd-image-updater write-back, each changing only `newTag:` lines); optionally alerts Discord |

The repo is public so public repos can call it: a public repository can only
call reusable workflows that live in public repositories
([GitHub docs](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#access-to-reusable-workflows)).

## Use it

Add one file to the repo, for example `.github/workflows/base.yaml`.

**Public repo** (GitHub-hosted runner, no alert):

```yaml
name: base
on:
  pull_request:
  push:
    branches: [main]

jobs:
  base:
    # The ceiling for every job inside: a called workflow can only narrow
    # these permissions, never widen them.
    permissions:
      contents: read
      pull-requests: read
    uses: dvystrcil/base-ci/.github/workflows/base-ci.yaml@v1
```

**Private repo** with its own self-hosted runner and a Discord alert:

```yaml
name: base
on:
  pull_request:
  push:
    branches: [main]

jobs:
  base:
    permissions:
      contents: read
      pull-requests: read
      id-token: write            # the alert logs in to Infisical by GitHub OIDC
    uses: dvystrcil/base-ci/.github/workflows/base-ci.yaml@v1
    with:
      runner-label: <repo>-runner
      infisical-domain: ${{ vars.INFISICAL_DOMAIN }}
      infisical-project-slug: ${{ vars.INFISICAL_PROJECT_SLUG }}
      infisical-identity-id: ${{ vars.INFISICAL_IDENTITY_ID }}
```

Pass no `secrets:`. Nothing here reads a caller secret, and `secrets: inherit`
would hand the repo's whole secret store to a workflow that doesn't need it.

### Inputs

| input | default | meaning |
|---|---|---|
| `runner-label` | `ubuntu-latest` | `runs-on` for every job. Public repos keep the default. A self-hosted scale set serves one repo, so a wrong label queues forever instead of failing. |
| `infisical-domain` | `''` | Infisical API base URL (ending in `/api`). It must be reachable **from the runner**. |
| `infisical-project-slug` | `''` | Project holding `DISCORD_WEBHOOK_CRITICAL`. |
| `infisical-identity-id` | `''` | Machine identity with OIDC auth that trusts the **caller** repo's token. |

The alert needs all three `infisical-*` inputs. If any is empty, a detected
push still fails the run, prints the offending commits and sends nothing. The
OIDC token minted inside these workflows carries the caller's `repository`
and `repository_id` claims; only `job_workflow_ref` names this repo
([GitHub docs](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-with-reusable-workflows)).
So an identity bound to the caller's `repository_id` works unchanged.

### Check names

A called job reports as `<caller job> / <called job>`. With the caller job id
`base`:

| check | when |
|---|---|
| `base / gitleaks` | every PR and push |
| `base / detect-bot-direct-push / check` | push to `main` only; `skipped` on PRs, so never make it a required check |

A standalone `secret-scan.yml` reports plain `gitleaks`. If a ruleset requires
`gitleaks`, run both workflows side by side until `base / gitleaks` is
required, then remove `gitleaks` from the ruleset and delete `secret-scan.yml`
together. Swapping them in one step blocks every merge.

## Versioning

Callers pin `@v1`, a moving major tag, and `base-ci.yaml` calls the detector
at `dvystrcil/base-ci/...@v1` too. **Moving `v1` is the release** to every
caller; merging to `main` releases nothing.

```sh
gh workflow run release.yaml --repo dvystrcil/base-ci -f bump=patch   # vX.Y.Z + move v1
```

A breaking change (a renamed job, so a renamed check; a new required input)
is a `major` bump: it creates `v2` and leaves `v1` alone. Rollback:
`git tag -f v1 <previous sha> && git push --force origin refs/tags/v1`.

This repo's own [`ci.yaml`](.github/workflows/ci.yaml) runs `base-ci.yaml`
from the PR's commit, plus actionlint and the detector's tests
(`python3 -m unittest -v tests.test_detect_bot_direct_push`, needs PyYAML).
Its nested detector call is `@v1`, so the `base` job needs the tag to exist.

## License

[MIT](LICENSE)
