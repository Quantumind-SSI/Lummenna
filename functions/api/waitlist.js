/**
 * Download waitlist endpoint (Cloudflare Pages Function).
 *
 * Lummenna 1.0 is not publicly distributed yet: it is absent from PyPI, has no
 * git tag, and no published container image. This collects addresses so the
 * download link can be sent when that changes.
 *
 * The form posts natively, with no JavaScript, so the site keeps its
 * `script-src 'none'; connect-src 'none'` policy. `form-action 'self'` permits
 * this because the endpoint is same-origin.
 *
 * Reliability: the database write is the source of truth and the notification
 * email is best-effort. A mail failure must never lose a signup, so the send is
 * attempted after the row is committed and its failure is swallowed
 * deliberately (and only there).
 *
 * Location matters: wrangler resolves `functions/` relative to the working
 * directory it is invoked from, not relative to the asset directory passed to
 * `pages deploy`. Kept at the repository root so the documented command,
 * `wrangler pages deploy ./website` run from the root, picks it up. Placing it
 * under website/ made it invisible to the deploy and left /api/waitlist a 404.
 *
 * Required Pages bindings:
 *   DB          D1 database, schema in infra/cloudflare/waitlist_schema.sql
 *   SEND_EMAIL  Email Routing send binding (optional; absence disables notify)
 *   NOTIFY_TO   plain text var, defaults to licensing@quantumindssi.com
 *   NOTIFY_FROM plain text var, must be on a zone you control
 */

/** Longest address accepted. RFC 5321 caps the path at 254 characters. */
const MAX_EMAIL_LENGTH = 254;

/** Deliberately conservative: one @, no whitespace, a dotted domain. */
const EMAIL_PATTERN = /^[^\s@,;:<>()[\]\\]+@[^\s@.]+(\.[^\s@.]+)+$/;

/** Cap the body so a large POST cannot be used to burn CPU. */
const MAX_BODY_BYTES = 4096;

/** Where to send a visitor after a decision, kept relative for same-origin. */
const REDIRECT_OK = "/thanks";
const REDIRECT_INVALID = "/thanks?state=invalid";
const REDIRECT_DUPLICATE = "/thanks?state=already";

/**
 * Redirect with 303 so the browser re-issues a GET and a refresh cannot
 * resubmit the form.
 *
 * @param {string} location Same-origin path.
 * @returns {Response}
 */
function seeOther(location) {
  return new Response(null, {
    status: 303,
    headers: { Location: location, "Cache-Control": "no-store" },
  });
}

/**
 * Normalise and validate a submitted address.
 *
 * @param {unknown} raw Value straight from the form.
 * @returns {string|null} Lower-cased address, or null when unusable.
 */
export function normaliseEmail(raw) {
  if (typeof raw !== "string") return null;
  const email = raw.trim().toLowerCase();
  if (email.length === 0 || email.length > MAX_EMAIL_LENGTH) return null;
  if (!EMAIL_PATTERN.test(email)) return null;
  return email;
}

/**
 * Build an RFC 5322 message without pulling in a MIME dependency.
 *
 * Header values are stripped of CR and LF, because a newline inside a header
 * would let submitted text inject additional headers.
 *
 * @param {{from: string, to: string, subject: string, body: string}} parts
 * @returns {string} Raw message suitable for an EmailMessage.
 */
export function buildMessage(parts) {
  const clean = (value) => String(value).replace(/[\r\n]+/g, " ").trim();
  const headers = [
    `From: ${clean(parts.from)}`,
    `To: ${clean(parts.to)}`,
    `Subject: ${clean(parts.subject)}`,
    "MIME-Version: 1.0",
    'Content-Type: text/plain; charset="utf-8"',
  ];
  return `${headers.join("\r\n")}\r\n\r\n${parts.body}`;
}

/**
 * Notify the mailbox that a new address joined. Best effort by design.
 *
 * @param {Record<string, any>} env Pages environment bindings.
 * @param {string} email The address that signed up.
 * @param {string} source Page the signup came from.
 * @returns {Promise<boolean>} True when the message was handed to the binding.
 */
async function notify(env, email, source) {
  if (!env.SEND_EMAIL) return false;
  const to = env.NOTIFY_TO || "licensing@quantumindssi.com";
  const from = env.NOTIFY_FROM;
  if (!from) return false;
  try {
    const { EmailMessage } = await import("cloudflare:email");
    const raw = buildMessage({
      from,
      to,
      subject: "Lummenna download waitlist signup",
      body:
        `A new address joined the download waitlist.\n\n` +
        `Address: ${email}\n` +
        `Source:  ${source}\n` +
        `Time:    ${new Date().toISOString()}\n\n` +
        `Consent was given explicitly on the form.\n`,
    });
    await env.SEND_EMAIL.send(new EmailMessage(from, to, raw));
    return true;
  } catch (error) {
    // Swallowed on purpose: the signup is already committed, and failing the
    // request here would tell the visitor their address was not recorded when
    // it was. This is the only place a failure is not surfaced.
    console.error("waitlist notify failed", error instanceof Error ? error.message : error);
    return false;
  }
}

/**
 * Insert a signup, reporting whether the address was already present.
 *
 * @param {D1Database} db Bound D1 database.
 * @param {string} email Normalised address.
 * @param {string} source Page the signup came from.
 * @returns {Promise<"inserted"|"duplicate">}
 * @throws {Error} When the write fails for any reason other than duplication.
 */
async function insertSignup(db, email, source) {
  const statement = db
    .prepare(
      "INSERT INTO waitlist_signup (email, consent, created_at, source) " +
        "VALUES (?1, 1, ?2, ?3) ON CONFLICT(email) DO NOTHING"
    )
    .bind(email, Math.floor(Date.now() / 1000), source);
  const result = await statement.run();
  const changes = result?.meta?.changes ?? 0;
  return changes > 0 ? "inserted" : "duplicate";
}

/**
 * Handle a waitlist submission.
 *
 * @param {{request: Request, env: Record<string, any>}} context
 * @returns {Promise<Response>}
 */
export async function onRequestPost(context) {
  const { request, env } = context;

  const contentType = request.headers.get("content-type") || "";
  if (!contentType.includes("application/x-www-form-urlencoded")) {
    return seeOther(REDIRECT_INVALID);
  }

  const body = await request.text();
  if (body.length > MAX_BODY_BYTES) {
    return seeOther(REDIRECT_INVALID);
  }
  const form = new URLSearchParams(body);

  // Honeypot: a field hidden from humans and left empty by them. Bots fill
  // every input they find. Answer as if accepted so the bot learns nothing.
  if ((form.get("company") || "").trim() !== "") {
    return seeOther(REDIRECT_OK);
  }

  const email = normaliseEmail(form.get("email"));
  if (email === null) {
    return seeOther(REDIRECT_INVALID);
  }

  // Consent is refused rather than defaulted: an unticked box means no lawful
  // basis to store the address, so nothing is written.
  if (form.get("consent") !== "yes") {
    return seeOther(REDIRECT_INVALID);
  }

  if (!env.DB) {
    // Fail loudly. Silently discarding a signup would be worse than an error,
    // because the visitor would believe they had been recorded.
    console.error("waitlist DB binding is not configured");
    return new Response("Waitlist storage is not configured.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    });
  }

  const source = new URL(request.url).searchParams.get("source") || "/";

  let outcome;
  try {
    outcome = await insertSignup(env.DB, email, source.slice(0, 64));
  } catch (error) {
    console.error("waitlist insert failed", error instanceof Error ? error.message : error);
    return new Response("Could not record your address. Please try again.", {
      status: 500,
      headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    });
  }

  if (outcome === "duplicate") {
    return seeOther(REDIRECT_DUPLICATE);
  }

  await notify(env, email, source);
  return seeOther(REDIRECT_OK);
}

/**
 * A GET on this path means someone opened it directly; send them to the form
 * rather than showing a bare error.
 *
 * @returns {Response}
 */
export function onRequestGet() {
  return seeOther("/#download");
}
