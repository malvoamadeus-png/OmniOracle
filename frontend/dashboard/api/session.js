import {
  getSessionSecret,
  hasPasswordConfig,
  readSessionFromCookieHeader,
  sendJson,
} from "./_session.js";

export default function handler(req, res) {
  if (req.method !== "GET") {
    sendJson(res, 405, { role: null, configured: false, error: "Method Not Allowed" });
    return;
  }

  const secret = getSessionSecret();
  const configured = Boolean(secret) && hasPasswordConfig();
  if (!configured) {
    sendJson(res, 200, { role: null, configured: false });
    return;
  }

  const session = readSessionFromCookieHeader(req.headers.cookie || "", secret);
  sendJson(res, 200, {
    role: session?.role ?? null,
    configured: true,
  });
}
