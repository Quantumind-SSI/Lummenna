# Web Deployment Runbook

How lummenna.com is actually served, and the work still outstanding.

`DEPLOYMENT.md` covers self-hosting the Python application on your own hardware.
It does not describe the public website at all, which is why the production
wiring below went unrecorded and a binding was able to go missing unnoticed.
This file closes that gap.

> **Execution constraint, 2026-08-31.** Everything under "Outstanding work" must
> be run from the clean host, not from `Oluwaferanmis-MacBook-Air.local`. That
> machine is subject to `incident-20260827`, still requires rebuild, and was
> reinfected on 2026-08-30 (finding F-17). Its remotes are deliberately
> neutralised and a machine-wide git forge block is in place. Do not undo either
> on that host. See `~/incident-20260827/INCIDENT-REPORT.md` and
> `quarantine-qweb-reinfection-20260831T163521Z/remote-neutralisation-record.txt`.

---

## 1. Production topology, as verified

Production is **Cloudflare Pages**. Azure serves nothing for the public site.

| Item | Value |
|---|---|
| Pages project | `lummenna-site` |
| Cloudflare account | `09087d66cc4fb84c559132f29c11f17b` |
| Domains | `lummenna.com`, `www.lummenna.com`, `lummenna-site.pages.dev` |
| Git integration | **None.** Deploys are direct uploads. |
| Static assets | `website/` |
| Pages Function | `functions/api/waitlist.js` |
| D1 database | `lummenna-waitlist` (`bca688b9-d172-45cc-85f9-1bed82f17fc2`) |
| D1 table | `waitlist_signup`, schema in `infra/cloudflare/waitlist_schema.sql` |
| Config source of truth | `wrangler.toml` at the repository root |

### Deploying

Run from the repository **root**, with no directory argument:

```bash
wrangler pages deploy                  # preview, branch inferred from git
wrangler pages deploy --branch main    # production (lummenna.com)
```

The working directory matters. Wrangler resolves `functions/` relative to where
it is invoked, not relative to `pages_build_output_dir`. Deploying from inside
`website/` leaves the Function undiscovered and `/api/waitlist` returns 404.
This is what commit `35b59fb` fixed.

Wrangler infers the branch from git. On any branch other than `main` you get a
preview deployment, not production. Pass `--branch main` explicitly when you
intend to publish.

### Configuration is declarative, not dashboard

`wrangler.toml` is the source of truth. Per Cloudflare's documentation the same
fields become read-only in the dashboard once the file is in use. A binding
added in the dashboard and not added to the file will be dropped by the next
deploy. **Add bindings to `wrangler.toml`, never to the dashboard.**

### Verifying a deployment

```bash
# every page resolves
for p in "" features pricing docs thanks; do
  printf '/%-10s %s\n' "$p" "$(curl -sL -o /dev/null -w '%{http_code}' https://lummenna.com/$p)"
done

# security headers survived (website/_headers)
for h in strict-transport-security content-security-policy \
         x-content-type-options x-frame-options referrer-policy; do
  curl -sI https://lummenna.com/ | grep -qi "^$h:" && echo "OK   $h" || echo "MISS $h"
done

# the waitlist actually persists, rather than merely returning a redirect
curl -s -o /dev/null -w '%{http_code} -> %{redirect_url}\n' -X POST \
  https://lummenna.com/api/waitlist \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data 'email=verify@example.invalid&consent=yes'
```

A `303` to `/thanks` is success. A `503` means the `DB` binding is absent: the
Function fails closed rather than silently discarding an address. Remove any
verification row afterwards:

```bash
wrangler d1 execute lummenna-waitlist --remote \
  --command "DELETE FROM waitlist_signup WHERE email LIKE '%@example.invalid';"
```

---

## 2. Fixed on 2026-08-31

**The waitlist was rejecting every signup.** `POST /api/waitlist` returned
`503 Waitlist storage is not configured.` The database and its schema were both
correct; the `DB` binding had never been attached to the Pages project, so
`functions/api/waitlist.js` took its `!env.DB` branch. The table was empty,
confirming no signup had ever been recorded.

Fixed by declaring the binding in `wrangler.toml` rather than clicking it into
the dashboard, so it is reviewable and cannot go missing on a rebuild.
Redeployed and verified end to end: insert, duplicate, consent withheld, and
honeypot paths all behave correctly, and the row reaches D1.

---

## 3. Outstanding work

Ordered. Items 1 and 2 are prerequisites for everything after them.

### 3.1 Rotate credentials exposed on the compromised host

Do this first, from the clean host.

- Revoke the GitHub OAuth token minted by `gh` on the affected machine
  (`repo`, `workflow`, `gist`, `read:org` scopes).
- Rotate the Cloudflare wrangler OAuth token. Finding F-04 documents a clipboard
  stage that specifically recognises source-control and package-registry secret
  formats, so treat it as exposed.
- Exposure assessment starts from **2026-08-11** per finding F-15, not from the
  incident date.

### 3.2 Rewrite the contaminated remotes

Per `INCIDENT-REPORT.md`, finding F-05 is `Local clean, remotes open`. The
GitHub remotes still carry the three malicious blobs and require history rewrite
**on a clean machine**. `QuantmindSSI/Qweb.git` is the fifth remote identified in
F-17 and was never in the original inventory.

Only after this is complete should `origin` be restored, using the exact URLs in
`remote-neutralisation-record.txt`.

Combine this with the internal-document purge in 3.3. Both are blob-level strips
across all refs, and doing them in one pass means one force-push and one round of
invalidating forks, caches, CI artefacts and any existing clones, rather than two.

### 3.3 Purge internal documents from history

Nine internal documents were tracked on `feat/p2p-durable-replay-store` and were
untracked on 2026-09-01. Untracking removes them from future commits only. Thirty
`docs/` paths remain reachable in history and would be recoverable by anyone who
clones the repository once it is public.

The exposure is not hypothetical. Before untracking, the key-custody runbook was
readable straight out of the object store:

```bash
git cat-file -p $(git rev-list --all --objects \
  | grep 'docs/LICENSE_SIGNING_KEY_CUSTODY.md' | head -1 | cut -d' ' -f1) | head -1
# -> # Lummenna Pro License Signing-Key Custody Runbook
```

It contains no key material (verified: no private-key blobs exist anywhere in
history), but it does document roles, generation, safeguarding, minting,
rotation, revocation and compromise response for the Pro licensing system.
`docs/cla/ownership-attestation-oluwaferanmi-alias.md` links a git identity to a
legal alias. Neither belongs in a public repository.

Run on the clean host, on a fresh mirror, never on a working clone:

```bash
# 1. Preserve an evidence clone BEFORE rewriting. Required by the incident
#    procedure and the only way to answer questions afterwards.
git clone --mirror https://github.com/QuantmindSSI/lummenna.git lummenna-evidence.git

# 2. Rewrite a separate mirror.
git clone --mirror https://github.com/QuantmindSSI/lummenna.git lummenna-rewrite.git
cd lummenna-rewrite.git

# 3. Strip the directory from every ref. git-filter-repo is the supported tool;
#    filter-branch is deprecated and mishandles merge commits, which matters
#    because at least one contaminated path arrived through a merge.
git filter-repo --invert-paths --path docs/ --force

# 4. Verify: all three commands must print nothing.
git log --all --name-only --pretty=format: -- 'docs/*' | grep '^docs/' | sort -u
git rev-list --all --objects | grep '^.\{40\} docs/'
git log --all --oneline -S 'License Signing-Key Custody'
```

Verify the malicious blobs are gone in the same pass, using the ids in
`~/incident-20260827/clean-host-kit-20260829T083327Z/runbook/bad-blobs.txt`:

```bash
for b in 5e226620d2e360205cc8634e3c581a008d382561 \
         941dd7282dce8a2619b208559453277d61b6f041 \
         934d55548c36ff0e330f2a2ba69bf74b10a7dcba; do
  git cat-file -e "${b}^{blob}" 2>/dev/null && echo "STILL PRESENT $b" || echo "absent $b"
done
```

`stage4-verify.sh` from the clean-host kit gates this from a fresh clone and is
the authoritative check. Only force-push once it passes.

After the force-push, the old objects survive in forks, caches, pull-request
refs (`refs/pull/*`, which a mirror does not rewrite) and any clone taken
earlier. Ask GitHub Support to garbage-collect the stale objects, and treat
anything in those documents as disclosed if the repository was ever shared.

### 3.4 Push the website work

Five commits exist on `feat/p2p-durable-replay-store` and on no remote. They are
the entire public site, including everything currently serving production:

```
35b59fb  fix(web): move the Pages Function to the root so it actually deploys
bbf5349  feat(web): download waitlist
b51d4b5  fix(web): drop figure captions
d072525  fix(web): stop linking the public site at a private repository
5d60451  feat(web): ship the marketing site and app frontend
```

Transfer them as files, not as a push from the affected host:

```
~/lummenna-backup-20260831-204328.bundle          all branches, restore-tested
~/lummenna-uncommitted-20260831-204354.patch      33 modified files
~/lummenna-untracked-20260831-204354.tar.gz       wrangler.toml, test_bench_results.py
```

On the clean host:

```bash
git clone -b feat/p2p-durable-replay-store lummenna-backup-20260831-204328.bundle lummenna
cd lummenna
git apply ../lummenna-uncommitted-20260831-204354.patch
tar -xzf ../lummenna-untracked-20260831-204354.tar.gz
```

`main` is at `95f9bad` and contains neither `website/` nor
`.github/workflows/deploy.yml`. Both exist only on the feature branch.

### 3.5 Restore the contact addresses

Cloudflare **Email Address Obfuscation** (Scrape Shield, zone level) rewrites
`mailto:` links into `/cdn-cgi/l/email-protection` and injects
`email-decode.min.js`. The site's own CSP is `script-src 'none'`, so the browser
blocks the decoder:

```
Loading '/cdn-cgi/scripts/.../email-decode.min.js' violates the following
Content Security Policy directive: "script-src 'none'". The action has been blocked.
```

Live effect: the privacy note renders as "Removal on request to
`[email protected]`", and the footer "Source access" and "Commercial contact"
links point at a path that returns 404. There are zero working `mailto:` links
on the page.

Fix: Cloudflare dashboard, zone `lummenna.com`, Scrape Shield, **Email Address
Obfuscation, off**. This needs `Zone Settings: Edit`; the wrangler OAuth token
carries `zone (read)` only. Verify afterwards that
`document.querySelectorAll('a[href^="mailto:"]')` is non-empty and no
`.__cf_email__` elements remain.

### 3.6 Azure as the development stage

Azure resources cannot be renamed. There is no `az staticwebapp rename`, and
`update` changes properties rather than the name. The dev stage therefore has to
be created new and the old resource deleted.

Current state, which is a stale fourth hosting path rather than a dev stage:

| Property | Value |
|---|---|
| Resource | `lumena-site` (old single-M brand) |
| Resource group | `rg-lumena-leaderboard` (contains nothing else) |
| Region / SKU | East US 2, Free |
| Provider | `SwaCli`, no repo, no branch, no custom domain |
| Hostname | `polite-hill-067c81c0f.7.azurestaticapps.net` |
| Serving | A **pre-waitlist build**, publicly reachable, 8,857 bytes vs 10,526 local |

No tracked file references that hostname, so nothing depends on it.

Static Web Apps are available in Central US, East US 2, West US 2, West Europe,
and East Asia. West Europe is closest to the existing UK footprint. The current
naming convention elsewhere in the subscription is
`rg-<project>-<stage>-<region>`.

```bash
az group create --name rg-lummenna-dev-westeurope --location westeurope

az staticwebapp create \
  --name lummenna-site-dev \
  --resource-group rg-lummenna-dev-westeurope \
  --location westeurope \
  --sku Free

TOKEN=$(az staticwebapp secrets list \
  --name lummenna-site-dev --resource-group rg-lummenna-dev-westeurope \
  --query "properties.apiKey" -o tsv)

swa deploy ./website --deployment-token "$TOKEN" --env production --no-use-keychain
```

Then remove the stale public copy:

```bash
az group delete --name rg-lumena-leaderboard --yes
```

Note the platform difference before relying on this tier. Azure Static Web Apps
reads `website/staticwebapp.config.json`; Cloudflare Pages reads
`website/_headers` and `website/_redirects` and ignores the JSON.
`tests/test_website.py` asserts the two representations stay identical, so edit
the JSON and regenerate, never one of the pair alone.

A dev stage on Azure cannot execute `functions/api/waitlist.js` and cannot bind
D1. The waitlist outage described in section 2 would not have been visible in
such a stage. Treat Azure as a static content preview only, and verify anything
touching Functions or D1 against a Cloudflare preview deployment.

### 3.7 Reconcile `.github/workflows/deploy.yml`

The workflow builds a container, applies `infra/azure/main.bicep`, and pushes
`website/` to Azure Static Web Apps, gated on `refs/heads/main`.

It has never run. `main` does not contain the file. The Azure infrastructure it
targets does not exist: there is no Lummenna container app and no Lummenna
container registry in the subscription. The only Lummenna resource present is
the orphaned static site in 3.6. The file also has uncommitted local edits
changing the verification endpoint from `/v1/memories` to `/v1/status`.

Before it can run at all, a remote must exist (3.2). When wiring Azure as dev
and Cloudflare as production, the changes required are:

- Retarget the `site` job at `lummenna-site-dev` rather than discovering the
  first static web app in the resource group with `[0].name`, which is
  positional and will silently pick the wrong resource once there is more than
  one.
- Add a production job that runs `wrangler pages deploy --branch main`, using a
  `CLOUDFLARE_API_TOKEN` repository secret scoped to Pages edit and D1.
- Keep the existing authentication assertion in the `verify` job. It asserts
  that an unauthenticated request is rejected with 401, which is the regression
  test for the `LUMENA_` versus `LUMMENNA_` environment prefix defect. Do not
  weaken it to a health check.
- Add an equivalent assertion for the waitlist, so the 503 condition fixed in
  section 2 cannot recur undetected.

### 3.8 Fix the CI badge

`README.md:16` builds the workflow badge from `QuantumindSSI/lummenna`. That is
the personal user account. The repository belongs to the organisation
`QuantmindSSI` (no `u`), as declared in `pyproject.toml:26`, the Dockerfile OCI
source label, and `LICENSE`. The badge is broken.

Both accounts are real, which is why this is easy to get wrong:

| Login | ID | Type |
|---|---|---|
| `QuantmindSSI` | 297810579 | Organization, owns the repository |
| `QuantumindSSI` | 277343853 | User, the account `.githooks/allowed-identity.sh` enforces for commits |
