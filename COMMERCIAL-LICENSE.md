# Lumena Commercial License (Pro Edition)

**Copyright (c) 2025-2026 QuantumindSSI. All rights reserved.**

This document describes the **commercial license** option for Lumena. It is one
half of Lumena's dual-license model (the other being AGPL-3.0-or-later for the
Community Edition; see `LICENSE`).

> This is a plain-language summary and license-intent document. The binding
> commercial terms are set out in the signed order form / master agreement
> provided by QuantumindSSI. For a quote or the full agreement, contact
> **licensing@quantumindssi.com**.

---

## 1. Why a commercial license?

The Community Edition is licensed under **AGPL-3.0-or-later**. AGPL requires that
if you run a modified version of Lumena as a network service, you make the
complete corresponding source code available to the users of that service.

A **commercial license** from QuantumindSSI:

1. **Removes the AGPL copyleft / network-use obligation** — you may embed and
   deploy Lumena in closed-source and SaaS products without disclosing your
   source.
2. **Unlocks the Lumena Pro Edition** — the commercial-only capabilities that
   are **not** part of the AGPL Community Edition (see §3).
3. **Includes commercial terms** — warranty, indemnification, support, and SLA
   options per your agreement.

---

## 2. Who needs it

You need a commercial license if any of the following apply:

- You want to use Lumena in a **proprietary / closed-source** product or a
  **hosted/SaaS** service without complying with AGPL-3.0's source-disclosure
  requirement.
- You want to use any **Pro Edition** feature (§3).
- Your organization's policy prohibits AGPL-licensed dependencies.

You do **not** need a commercial license if you use only the Community Edition
and comply with AGPL-3.0 (including making your source available to network
users).

---

## 3. Pro Edition — commercial-only capabilities

The following capabilities are licensed **only** under this commercial license
and are **not** covered by the AGPL Community grant.

### 3.1 Available now

These capabilities are **built, shipping, and license-enforced today** (each is
gated by Lumena's entitlement engine, `lumena.licensing.PRO_FEATURES`).

| Ref | Pro capability |
|-----|----------------|
| P1 | Compliance: right-to-be-forgotten (RTBF), audit trails, custom PII governance |
| P2 | Encryption-at-rest (SQLCipher/Fernet) + key management / rotation |
| P4 | Real (storage-layer) multi-tenant isolation |
| P6 | Advanced P2P (key-rotation, persistent replay store, multi-node mesh) |
| V5 | Priority support + SLA (commercial support commitment) |
| V8 | Long-term support (LTS) releases (commercial release commitment) |

### 3.2 On the commercial roadmap — NOT yet available

These capabilities are **planned and not yet available**. They are listed here so
prospective customers can see the direction of the Pro edition, but they are
**not** part of any current license grant and must not be relied upon until
delivered. Their acceptance criteria and status are tracked in `ROADMAP.md`
("Enterprise Pro Roadmap"); they are registered in `PLANNED_PRO_FEATURES`,
separate from the enforceable registry, so they cannot be gated as present
capabilities.

| Ref | Planned Pro capability | Status |
|-----|------------------------|--------|
| P3 | Advanced API auth: RBAC, OIDC/JWT, per-user API keys | Not yet available |
| P5 | Scale-out backend (Postgres/pgvector), replication, high availability | Not yet available |
| V1 | Team dashboard & analytics | Not yet available |
| V2 | Managed key vault / HSM integration | Not yet available |
| V3 | Compliance reporting & exports (GDPR/SOC2-ready) | Not yet available |
| V4 | SSO / directory sync (OIDC, SAML, SCIM) | Not yet available |
| V6 | Backup/restore orchestration & point-in-time recovery | Not yet available |
| V7 | Horizontal read scaling & HA topology | Not yet available |

Access to Pro features is enabled by a valid **license key** issued by
QuantumindSSI and validated by Lumena's entitlement engine
(`lumena.licensing`). A license key never unlocks a roadmap (§3.2) feature,
because those capabilities do not exist in the software yet.

---

## 4. License grant (summary)

Subject to a signed commercial agreement and payment of applicable fees,
QuantumindSSI grants the licensee a non-exclusive, non-transferable license to
use the Lumena Pro Edition within the scope (seats, tenants, nodes, or
deployments) defined in the order form, for the license term.

Restrictions (unless expressly permitted in your agreement): no redistribution
of the Pro modules as source, no sublicensing, no removal of license/entitlement
enforcement, no use beyond the licensed scope.

---

## 5. Editions & pricing

Current tiers, price points, and formal quotes are provided per order form.
Contact **licensing@quantumindssi.com**.

---

## 6. Contact

- **Commercial licensing & quotes:** licensing@quantumindssi.com
- **Security:** security@quantumindssi.com
- **General:** admin@quantumindssi.com

*This document does not itself grant any license. A commercial license is
granted only by a signed agreement with QuantumindSSI.*
