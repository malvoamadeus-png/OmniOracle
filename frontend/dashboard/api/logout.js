import { clearSessionCookie, sendJson } from "./_session.js";

export default function handler(req, res) {
  if (req.method !== "POST") {
    sendJson(res, 405, { ok: false, error: "Method Not Allowed" });
    return;
  }

  clearSessionCookie(res);
  sendJson(res, 200, { ok: true });
}
