# Lumena Pro License Signing-Key Custody Runbook

This runbook governs the **Ed25519 signing key** that mints Lumena Pro license
keys. That key is the root of trust for all commercial entitlements: anyone who
holds it can issue licenses that Lumena will accept. Treat it like a code-signing
key or a root CA key.

- **Tooling:** `scripts/issue_license.py` (`keygen` and `issue` subcommands).
- **Verification:** the matching **public** key is embedded in
  `lummenna/licensing.py` as `_QUANTUMINDSSI_PUBLIC_KEY_B64` and used for fully
  offline verification (no phone-home).
- **The private key is NEVER committed to this repository** and never appears in
  logs, CI, issues, or chat.

---

## 1. Roles

- **Key Custodian** — the individual (or role account) authorised to hold the
  private signing key and mint licenses. There should be at least two custodians
  for continuity (bus-factor > 1), each with an independent encrypted backup.

---

## 2. Generate the signing keypair (one-time, offline)

Do this on an **air-gapped or otherwise trusted offline machine** — not in CI,
not on a shared server.

```bash
# Writes the private key to a 0600 file and prints the PUBLIC key to stdout.
python scripts/issue_license.py keygen --out quantumindssi-license-private-key.b64
```

- The command creates the private-key file with `0600` (owner-only) permissions.
- The **public** key is printed to stdout; the private key path and a reminder
  are printed to stderr.
- To print the private key to stderr instead of a file (e.g. to paste straight
  into a vault), omit `--out`.

`keygen` refuses to overwrite an existing `--out` file unless `--force` is given,
so you cannot silently clobber a live signing key.

---

## 3. Embed the public key

Paste the printed public key into `lummenna/licensing.py`:

```python
_QUANTUMINDSSI_PUBLIC_KEY_B64: str = "<public key from keygen>"
```

Commit this change (the public half is safe to commit). Ship it in the release
that customers will verify their licenses against.

**Confirm the pairing before issuing any real license:** mint a throwaway key and
let the built-in self-verification pass (see §5). A mismatched keypair fails
loudly rather than shipping an unusable license.

---

## 4. Safeguard the private key

- **Storage:** an offline secrets vault or hardware token. Acceptable options:
  a hardware security key/HSM, a password-manager vault with the file as a secure
  attachment, or an encrypted volume on an air-gapped machine.
- **Backups:** at least two geographically separate, **encrypted** backups held
  by different custodians. Test that a backup restores.
- **Never:** commit it, email it in plaintext, paste it into chat/tickets, store
  it on a build server, or bake it into a container image.
- **Working copies:** after generating or using the key, delete any temporary
  working copies from disk (`shred`/secure-delete where available).
- **CI:** CI must never have access to the private key. Licenses are minted
  manually by a custodian; CI only verifies against the public key.

---

## 5. Issue (mint) a license

Provide the private key via `--private-key-file` or the
`LUMENA_SIGNING_PRIVATE_KEY_B64` environment variable.

```bash
python scripts/issue_license.py issue \
    --subject "Acme Corp" \
    --features "*" \
    --seats 10 \
    --expires 2027-08-15 \
    --private-key-file quantumindssi-license-private-key.b64
```

- `--seats` is enforced as the maximum number of concurrent non-default tenants
  (`0` or omitted = unlimited). See `COMMERCIAL-LICENSE.md` §4.
- `--expires` bounds the license; short-dated licenses limit the blast radius of
  a leaked key. Omit for a perpetual license.
- Each license gets a unique `id` (uuid4 unless `--license-id` is given). **Record
  the id, subject, and expiry** in your license register — the id is what you add
  to the revocation list if the key must be killed (see §7).
- Before printing, the tool **self-verifies** the minted key against the embedded
  public key, so a wrong keypair fails immediately.

The historical form without a subcommand still works and is treated as `issue`:

```bash
python scripts/issue_license.py --subject "Acme" --features compliance --expires 2027-01-01
```

---

## 6. Rotate the signing key

Rotate on a fixed schedule and immediately on any suspected compromise.

1. Generate a **new** keypair on the offline machine (`keygen`, §2).
2. Embed the new public key in `lummenna/licensing.py` and cut a release. From that
   release on, **only** licenses signed by the new key verify.
3. **Re-issue** current customers' licenses with the new key (same subject,
   features, seats, expiry), and distribute them.
4. Revoke the old licenses if warranted (see §7), and destroy all copies of the
   old private key once every active customer has been re-issued.

> Note: because verification uses a single embedded public key, a release
> verifies licenses from exactly one signing key. Plan the re-issue window so
> customers are not locked out between the release and receiving their new key
> (issue new keys first, then ship the release).

---

## 7. Revoke a compromised or refunded license

Revocation is offline (there is no phone-home):

1. Add the license `id` to `REVOKED_LICENSE_IDS` in `lummenna/licensing.py`, with a
   comment noting the date and reason.
2. Cut a release. From that release on, the revoked key resolves to Community
   even though its signature is still valid.

Short `--expires` dates are the first line of defence; the revocation list handles
keys that must be killed before their natural expiry.

---

## 8. Incident response (suspected private-key compromise)

1. **Rotate** the signing key immediately (§6).
2. **Revoke** any licenses known or suspected to be affected (§7).
3. Cut an emergency release embedding the new public key and the revocation
   entries.
4. Re-issue legitimate customers on the new key.
5. Record the incident (what leaked, when, remediation) in your security log.
