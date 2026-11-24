"""detect-bot-direct-push.yml's shell steps, run for real.

The old check matched `github.actor` against `*-bot|*[Bb]ot`. A GitHub App
pushes as `name[bot]`, and `[Bb]` is a bracket expression, so App pushes
were never flagged and ran green.

The new check asks GitHub, per pushed commit, whether a merged PR contains
it. A direct commit is allowed only from the two known writers, and only
when it changes nothing but `newTag:` lines:

  - release-action's deploy step: `deploy: ...`, author
    homelab-bump-bot, pushed by dvystrcil-ci-cd[bot]
  - argocd-image-updater: `build: automatic update of ...`, author
    argocd-image-updater, pushed by dvystrcil-argocd[bot]

These tests take the steps' `run:` blocks out of the workflow file
(read-only) and run them under bash with a stub `curl` that serves fixture
API responses. No network. Run:

    python3 -m unittest -v tests.test_detect_bot_direct_push
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/detect-bot-direct-push.yml"
BASE = ROOT / ".github/workflows/base-ci.yaml"

API = "https://api.test"
REPO = "dvystrcil/example"
CICD = "dvystrcil-ci-cd[bot]"
ARGOCD = "dvystrcil-argocd[bot]"

NEWTAG_PATCH = "@@ -2,4 +2,4 @@\n images:\n   - name: registry.example/x\n-    newTag: 0.0.1\n+    newTag: 0.0.2"
OTHER_PATCH = "@@ -2,4 +2,4 @@\n images:\n-    newTag: 0.0.1\n+    newTag: 0.0.2\n-replicas: 1\n+replicas: 9"

# Stub curl: the URL (last argument) picks a fixture file. A POST anywhere
# else is the Discord webhook: its body is recorded. A missing fixture
# behaves like `curl -f` on a 404 (exit 22). Pull lookups can be sequenced:
# pulls-<sha>.1.json, .2.json, ... are served in order, then pulls-<sha>.json.
FAKE_CURL = textwrap.dedent("""\
    #!/usr/bin/env bash
    url="${@: -1}"; data=""
    prev=""; for a in "$@"; do [ "$prev" = "-d" ] && data="$a"; prev="$a"; done
    echo "$url" >> "$FAKE_API/calls"
    case "$url" in
      "$API_URL"/repos/*/compare/*) f=compare.json ;;
      "$API_URL"/repos/*/commits/*/pulls)
        sha=${url%/pulls}; sha=${sha##*/}
        n=$(( $(cat "$FAKE_API/n-$sha" 2>/dev/null || echo 0) + 1 )); echo $n > "$FAKE_API/n-$sha"
        if [ -f "$FAKE_API/pulls-$sha.$n.json" ]; then f=pulls-$sha.$n.json; else f=pulls-$sha.json; fi ;;
      "$API_URL"/repos/*/commits/*) f=commit-${url##*/}.json ;;
      *) printf '%s\\n' "$data" >> "$FAKE_API/posts"; exit 0 ;;
    esac
    [ -f "$FAKE_API/$f" ] || { echo "curl: (22) The requested URL returned error: 404" >&2; exit 22; }
    cat "$FAKE_API/$f"
""")


def step(name_prefix: str) -> dict:
    wf = yaml.safe_load(WORKFLOW.read_text())
    for s in wf["jobs"]["check"]["steps"]:
        if s.get("name", "").startswith(name_prefix):
            return s
    raise AssertionError(f"no step starting {name_prefix!r} in {WORKFLOW}")


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="detect-direct-push-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.api = self.tmp / "api"
        self.api.mkdir()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "curl").write_text(FAKE_CURL)
        (bindir / "curl").chmod(0o755)
        self.path = f"{bindir}:{os.environ['PATH']}"
        self.commits: list[str] = []

    # --- fixtures -------------------------------------------------------
    def commit(self, sha, *, message, author, patch=NEWTAG_PATCH, merged_pr=False):
        self.commits.append(sha)
        (self.api / f"commit-{sha}.json").write_text(json.dumps({
            "sha": sha,
            "commit": {"message": message, "author": {"name": author}},
            "files": [{"filename": "x/kustomization.yaml", "patch": patch}],
        }))
        pulls = [{"number": 7, "merged_at": "2026-11-16T00:00:00Z",
                  "base": {"ref": "main"}}] if merged_pr else []
        (self.api / f"pulls-{sha}.json").write_text(json.dumps(pulls))

    def write_compare(self):
        (self.api / "compare.json").write_text(json.dumps({
            "commits": [{"sha": s} for s in self.commits]}))

    # --- runners --------------------------------------------------------
    def classify(self, actor):
        self.write_compare()
        out = self.tmp / "github_output"
        out.write_text("")
        s = step("Classify the pushed commits")
        env = {**os.environ, **{k: str(v) for k, v in s.get("env", {}).items()}}
        env.update(PATH=self.path, FAKE_API=str(self.api), API_URL=API,
                   REPO=REPO, ACTOR=actor, BEFORE="b" * 40,
                   AFTER=self.commits[-1], GH_TOKEN="t",
                   GITHUB_OUTPUT=str(out), DETECT_RETRY_SLEEP="0")
        p = subprocess.run(["bash", "-e", "-c", s["run"]], env=env,
                           capture_output=True, text=True, timeout=60)
        return p, self._outputs(out.read_text())

    @staticmethod
    def _outputs(text):
        outs, lines, i = {}, text.splitlines(), 0
        while i < len(lines):
            line = lines[i]
            if "<<" in line:
                k, delim = line.split("<<", 1)
                i += 1; buf = []
                while lines[i] != delim:
                    buf.append(lines[i]); i += 1
                outs[k] = "\n".join(buf)
            elif "=" in line:
                k, v = line.split("=", 1); outs[k] = v
            i += 1
        return outs

    def assertFlagged(self, actor, *shas):
        p, o = self.classify(actor)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(o.get("found"), "true", (o, p.stdout))
        lines = o["violations"].splitlines()
        for sha in shas:
            self.assertEqual(sum(sha[:7] in l for l in lines), 1,
                             f"{sha[:7]} must be reported exactly once: {lines}")
        return o

    def assertClean(self, actor):
        p, o = self.classify(actor)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(o.get("found"), "false", (o, p.stdout))
        return o


class Classify(Harness):
    def test_direct_push_from_github_app_is_flagged(self):
        """An App login ends in [bot] and was missed by the old glob."""
        self.commit("a" * 40, message="fix: tweak a thing", author="homelab-bump-bot")
        self.assertFlagged(CICD, "a" * 40)

    def test_pr_merge_is_not_flagged_whoever_pushes_it(self):
        """Auto-merged bump PRs are merged by dvystrcil-ci-cd[bot] too."""
        self.commit("a" * 40, message="chore: bump x (#12)", author="dvystrcil-ci-cd[bot]",
                    patch=OTHER_PATCH, merged_pr=True)
        self.assertClean(CICD)

    def test_release_action_deploy_commit_is_not_flagged(self):
        """release-action's deploy step."""
        self.commit("a" * 40, message="deploy: example-app 0.0.384", author="homelab-bump-bot")
        self.assertClean(CICD)

    def test_image_updater_commit_is_not_flagged(self):
        """argocd-image-updater's write-back."""
        self.commit("a" * 40, message="build: automatic update of example-app",
                    author="argocd-image-updater")
        self.assertClean(ARGOCD)

    def test_deploy_commit_that_changes_more_than_newtag_is_flagged(self):
        self.commit("a" * 40, message="deploy: example-app 0.0.384", author="homelab-bump-bot",
                    patch=OTHER_PATCH)
        o = self.assertFlagged(CICD, "a" * 40)
        self.assertIn("newTag", o["violations"])

    def test_deploy_commit_pushed_by_another_identity_is_flagged(self):
        self.commit("a" * 40, message="deploy: example-app 0.0.384", author="homelab-bump-bot")
        self.assertFlagged(ARGOCD, "a" * 40)

    def test_deploy_message_with_another_author_is_flagged(self):
        self.commit("a" * 40, message="deploy: example-app 0.0.384", author="someone")
        self.assertFlagged(CICD, "a" * 40)

    def test_direct_push_by_a_person_is_flagged(self):
        """The original incident: an agent pushing with the owner's credentials."""
        self.commit("a" * 40, message="fix: real fix", author="dvystrcil", patch=OTHER_PATCH)
        self.assertFlagged("dvystrcil", "a" * 40)

    def test_every_commit_in_the_push_is_checked(self):
        self.commit("a" * 40, message="feat: x (#3)", author="dvystrcil", merged_pr=True)
        self.commit("c" * 40, message="sneak", author="dvystrcil")
        o = self.assertFlagged("dvystrcil", "c" * 40)
        self.assertNotIn("a" * 7, o["violations"])

    def test_a_direct_commit_under_a_pr_merge_is_caught(self):
        """The head commit is a PR merge; the direct one is underneath it."""
        self.commit("c" * 40, message="sneak", author="dvystrcil")
        self.commit("a" * 40, message="feat: x (#3)", author="dvystrcil", merged_pr=True)
        self.assertFlagged("dvystrcil", "c" * 40)

    def test_a_line_that_only_mentions_newtag_is_not_a_newtag_line(self):
        self.commit("a" * 40, message="deploy: example-app 0.0.384", author="homelab-bump-bot",
                    patch="@@ -1 +1 @@\n-    image: x # newTag: 1\n+    image: evil # newTag: 2")
        self.assertFlagged(CICD, "a" * 40)

    def test_image_updater_message_with_another_author_is_flagged(self):
        self.commit("a" * 40, message="build: automatic update of x", author="someone")
        self.assertFlagged(ARGOCD, "a" * 40)

    def test_pr_merged_into_another_branch_does_not_count(self):
        sha = "a" * 40
        self.commit(sha, message="feat: x", author="dvystrcil")
        (self.api / f"pulls-{sha}.json").write_text(json.dumps(
            [{"number": 9, "merged_at": "2026-11-16T00:00:00Z", "base": {"ref": "release"}}]))
        self.assertFlagged("dvystrcil", sha)

    def test_a_file_without_a_patch_is_not_newtag_only(self):
        """GitHub omits `patch` for binary or very large files."""
        sha = "a" * 40
        self.commit(sha, message="deploy: example-app 0.0.384", author="homelab-bump-bot")
        c = json.loads((self.api / f"commit-{sha}.json").read_text())
        c["files"].append({"filename": "blob.bin"})
        (self.api / f"commit-{sha}.json").write_text(json.dumps(c))
        self.assertFlagged(CICD, sha)

    def test_a_lookalike_app_login_is_not_the_writer(self):
        self.commit("a" * 40, message="build: automatic update of x", author="argocd-image-updater")
        self.assertFlagged("dvystrcil-argocd-x[bot]", "a" * 40)

    def test_a_branch_deletion_is_not_judged(self):
        self.commit("a" * 40, message="x", author="dvystrcil")
        self.write_compare()
        out = self.tmp / "o"; out.write_text("")
        s = step("Classify the pushed commits")
        env = {**os.environ, "PATH": self.path, "FAKE_API": str(self.api), "API_URL": API,
               "REPO": REPO, "ACTOR": "dvystrcil", "BEFORE": "a" * 40, "AFTER": "0" * 40,
               "GH_TOKEN": "t", "GITHUB_OUTPUT": str(out), "DETECT_RETRY_SLEEP": "0"}
        p = subprocess.run(["bash", "-e", "-c", s["run"]], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self._outputs(out.read_text()).get("found"), "false")

    def test_pr_link_that_appears_late_is_waited_for(self):
        """GitHub can index the commit-to-PR link a moment after the push."""
        sha = "a" * 40
        self.commit(sha, message="feat: x (#3)", author="dvystrcil", merged_pr=True)
        (self.api / f"pulls-{sha}.1.json").write_text("[]")
        self.assertClean("dvystrcil")

    def test_unmerged_pr_does_not_count(self):
        sha = "a" * 40
        self.commit(sha, message="wip", author="dvystrcil")
        (self.api / f"pulls-{sha}.json").write_text(json.dumps(
            [{"number": 9, "merged_at": None, "base": {"ref": "main"}}]))
        self.assertFlagged("dvystrcil", sha)

    def test_api_failure_fails_the_step(self):
        """Fail closed: a check that cannot see must not report green."""
        self.commit("a" * 40, message="fix: x", author="dvystrcil")
        (self.api / f"commit-{'a' * 40}.json").unlink()
        (self.api / f"pulls-{'a' * 40}.json").unlink()
        p, _ = self.classify("dvystrcil")
        self.assertNotEqual(p.returncode, 0)


    def test_pr_lookup_failure_fails_the_step(self):
        """A merged commit whose PR lookup errors must not turn into a verdict."""
        sha = "a" * 40
        self.commit(sha, message="feat: x (#3)", author="dvystrcil", merged_pr=True)
        (self.api / f"pulls-{sha}.json").unlink()
        p, o = self.classify("dvystrcil")
        self.assertNotEqual(p.returncode, 0, o)


class Alert(Harness):
    def run_alert(self, webhook):
        s = step("Alert on direct push to main")
        env = {**os.environ, "PATH": self.path, "FAKE_API": str(self.api),
               "API_URL": API, "VIOLATIONS": "aaaaaaa fix: x (dvystrcil): not from a merged PR",
               "REPO": REPO, "ACTOR": "dvystrcil", "SERVER_URL": "https://github.com",
               "SHA": "a" * 40}
        if webhook:
            env["DISCORD_WEBHOOK_CRITICAL"] = "https://discord.test/hook"
        else:
            env.pop("DISCORD_WEBHOOK_CRITICAL", None)
        return subprocess.run(["bash", "-e", "-c", s["run"]], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_alert_posts_and_exits_1(self):
        """A detected direct push turns the run red after alerting."""
        p = self.run_alert(webhook=True)
        self.assertEqual(p.returncode, 1, p.stderr)
        posts = (self.api / "posts").read_text()
        self.assertIn("aaaaaaa", posts)
        self.assertIn(REPO, posts)

    def test_alert_without_webhook_still_exits_1(self):
        p = self.run_alert(webhook=False)
        self.assertEqual(p.returncode, 1)


class Wiring(unittest.TestCase):
    def setUp(self):
        self.wf = yaml.safe_load(WORKFLOW.read_text())
        self.steps = self.wf["jobs"]["check"]["steps"]

    def test_it_is_a_reusable_workflow_with_a_runner_input(self):
        """A rewrite once shipped this file without its `on: workflow_call`
        block (it split the file at `jobs:` inside a header comment). GitHub
        then rejected every caller's run with 0 jobs."""
        on = self.wf.get("on", self.wf.get(True))
        self.assertIsInstance(on, dict, "no `on:` block")
        call = on.get("workflow_call")
        self.assertIsInstance(call, dict, "not callable: no workflow_call")
        self.assertEqual(call["inputs"]["runner-label"]["default"], "ubuntu-latest")
        self.assertEqual(self.wf["jobs"]["check"]["runs-on"], "${{ inputs.runner-label }}")

    def test_alert_steps_run_only_when_something_is_found(self):
        for s in self.steps[1:]:
            self.assertIn("steps.classify.outputs.found == 'true'", s.get("if", ""), s.get("name"))

    def test_infisical_fetches_only_the_webhook(self):
        """Without secret-name the action exports the whole prod store."""
        inf = [s for s in self.steps if "Infisical/secrets-action" in s.get("uses", "")]
        self.assertEqual(len(inf), 1)
        self.assertEqual(inf[0]["with"].get("secret-name"), "DISCORD_WEBHOOK_CRITICAL")

    def test_no_event_text_is_spliced_into_shell(self):
        """Commit messages are attacker-controlled: pass via env, never ${{ }} in run."""
        for s in self.steps:
            self.assertNotIn("${{", s.get("run", ""), s.get("name"))

    def test_infisical_login_is_oidc_from_caller_inputs(self):
        """This repo is public and the alert's Infisical host, project and
        identity belong to the caller: they come in as inputs, and the login
        is GitHub OIDC, so no stored client secret is read."""
        inf = next(s for s in self.steps if "Infisical/secrets-action" in s.get("uses", ""))
        w = inf["with"]
        self.assertEqual(w.get("method"), "oidc")
        self.assertNotIn("client-id", w)
        self.assertNotIn("client-secret", w)
        self.assertEqual(w["domain"], "${{ inputs.infisical-domain }}")
        self.assertEqual(w["project-slug"], "${{ inputs.infisical-project-slug }}")
        self.assertEqual(w["identity-id"], "${{ inputs.infisical-identity-id }}")
        for key in ("infisical-domain", "infisical-project-slug", "infisical-identity-id"):
            self.assertIn(f"inputs.{key} != ''", inf["if"], key)

    def test_violations_are_reported_even_without_an_alert(self):
        """No alerting configured, or the Infisical fetch failed: the alert
        step must still run, print the violations and fail the run."""
        alert = next(s for s in self.steps if s.get("name", "").startswith("Alert"))
        self.assertIn("!cancelled()", alert["if"])

    def test_no_job_level_permissions(self):
        """id-token: write here would force it on callers that don't alert;
        the caller job's grants are the ceiling instead."""
        self.assertNotIn("permissions", self.wf["jobs"]["check"])


class BaseCi(unittest.TestCase):
    def setUp(self):
        self.wf = yaml.safe_load(BASE.read_text())
        self.det = yaml.safe_load(WORKFLOW.read_text())

    def test_nested_call_is_this_repo_at_v1(self):
        job = self.wf["jobs"]["detect-bot-direct-push"]
        self.assertEqual(
            job["uses"], "dvystrcil/base-ci/.github/workflows/detect-bot-direct-push.yml@v1")
        self.assertEqual(job["if"], "github.event_name == 'push' && github.ref == 'refs/heads/main'")

    def test_no_secrets_are_passed_on(self):
        """Nothing reads a caller secret; `inherit` would hand a public
        caller's whole secret store to the nested workflow for nothing."""
        for name, job in self.wf["jobs"].items():
            self.assertNotIn("secrets", job, name)

    def test_every_forwarded_input_exists_on_both_sides(self):
        det_inputs = self.det.get("on", self.det.get(True))["workflow_call"]["inputs"]
        base_inputs = self.wf.get("on", self.wf.get(True))["workflow_call"]["inputs"]
        fwd = self.wf["jobs"]["detect-bot-direct-push"]["with"]
        for k, v in fwd.items():
            self.assertIn(k, det_inputs, k)
            self.assertIn(k, base_inputs, k)
            self.assertEqual(v, "${{ inputs.%s }}" % k)

    def test_gitleaks_job_keeps_its_id_and_narrow_permissions(self):
        """`base / gitleaks` is the required-check name callers rely on."""
        job = self.wf["jobs"]["gitleaks"]
        self.assertEqual(job["permissions"], {"contents": "read", "pull-requests": "read"})
        self.assertEqual(job["steps"], [{"uses": "dvystrcil/secret-scan-action@v1"}])


class PublicRepoHygiene(unittest.TestCase):
    """This repo is public: no internal address may ride along in a file.

    Generic patterns always; owner-specific names (internal domains, project
    slugs) come from BASE_CI_PRIVATE_NEEDLES (comma-separated) so they are
    never written into this public file themselves."""

    PATTERNS = [r"\.svc\.cluster\.local", r"\b192\.168\.\d+\.\d+",
                r"\b10\.\d+\.\d+\.\d+\b", r"\b172\.(1[6-9]|2\d|3[01])\.\d+\.\d+"]

    def test_no_internal_addresses(self):
        import re
        needles = [n for n in os.environ.get("BASE_CI_PRIVATE_NEEDLES", "").split(",") if n]
        for path in sorted(ROOT.rglob("*")):
            if {".git", "__pycache__"} & set(path.parts) or not path.is_file():
                continue
            text = path.read_text(errors="replace")
            where = path.relative_to(ROOT)
            for pat in self.PATTERNS:
                self.assertFalse(re.search(pat, text), f"{where}: /{pat}/")
            for n in needles:
                self.assertFalse(n.lower() in text.lower(), f"{where}: a private needle")


if __name__ == "__main__":
    unittest.main()
