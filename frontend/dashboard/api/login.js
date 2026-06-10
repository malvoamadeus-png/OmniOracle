import {
  createSessionToken,
  getSessionSecret,
  hasPasswordConfig,
  hasRequiredRole,
  readJsonBody,
  resolveRoleByPassword,
  sanitizeNextPath,
  sendJson,
  setSessionCookie,
} from "./_session.js";

export default async function handler(req, res) {
  if (req.method !== "POST") {
    sendJson(res, 405, { ok: false, error: "Method Not Allowed" });
    return;
  }

  const secret = getSessionSecret();
  if (!secret || !hasPasswordConfig()) {
    sendJson(res, 500, { ok: false, error: "Server is not configured" });
    return;
  }

  let body = {};
  try {
    body = await readJsonBody(req);
  } catch {
    sendJson(res, 400, { ok: false, error: "Invalid JSON body" });
    return;
  }

  const password = typeof body.password === "string" ? body.password : "";
  if (!password) {
    sendJson(res, 400, { ok: false, error: "Password is required" });
    return;
  }

  const role = resolveRoleByPassword(password);
  if (!role) {
    sendJson(res, 401, { ok: false, error: "密码不正确" });
    return;
  }

  const requiredRole = body.required === "advanced" ? "advanced" : "basic";
  const requestedNext = sanitizeNextPath(typeof body.next === "string" ? body.next : "/");
  const redirectTo = hasRequiredRole(role, requiredRole)
    ? requestedNext
    : `/login?required=advanced&next=${encodeURIComponent(requestedNext)}`;

  const token = createSessionToken(role, secret);
  setSessionCookie(res, token);
  sendJson(res, 200, { ok: true, role, redirectTo });
}
