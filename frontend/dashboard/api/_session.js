import crypto from "node:crypto";

export const SESSION_COOKIE_NAME = "polysport_session";
const SESSION_TTL_SECONDS = 60 * 60 * 24 * 14;

function toBase64Url(input) {
  return Buffer.from(input)
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function fromBase64Url(input) {
  const normalized = input.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4 || 4)) % 4);
  return Buffer.from(padded, "base64");
}

function safeEqualStrings(a, b) {
  const aBuf = Buffer.from(a, "utf8");
  const bBuf = Buffer.from(b, "utf8");
  if (aBuf.length !== bBuf.length) return false;
  return crypto.timingSafeEqual(aBuf, bBuf);
}

function signPayload(payloadB64, secret) {
  return toBase64Url(crypto.createHmac("sha256", secret).update(payloadB64).digest());
}

export function createSessionToken(role, secret) {
  const payloadJson = JSON.stringify({
    role,
    exp: Date.now() + SESSION_TTL_SECONDS * 1000,
  });
  const payloadB64 = toBase64Url(payloadJson);
  const signature = signPayload(payloadB64, secret);
  return `${payloadB64}.${signature}`;
}

export function verifySessionToken(token, secret) {
  if (!token || typeof token !== "string" || !secret) return null;
  const parts = token.split(".");
  if (parts.length !== 2) return null;
  const [payloadB64, signature] = parts;
  const expectedSig = signPayload(payloadB64, secret);
  if (!safeEqualStrings(signature, expectedSig)) return null;

  try {
    const payload = JSON.parse(fromBase64Url(payloadB64).toString("utf8"));
    if (!payload || typeof payload !== "object") return null;
    if (payload.role !== "basic" && payload.role !== "advanced") return null;
    if (typeof payload.exp !== "number" || Date.now() > payload.exp) return null;
    return { role: payload.role, exp: payload.exp };
  } catch {
    return null;
  }
}

export function parseCookies(cookieHeader = "") {
  const out = {};
  const segments = cookieHeader.split(";");
  for (const segment of segments) {
    const idx = segment.indexOf("=");
    if (idx <= 0) continue;
    const k = segment.slice(0, idx).trim();
    const v = segment.slice(idx + 1).trim();
    if (k) out[k] = decodeURIComponent(v);
  }
  return out;
}

export function readSessionFromCookieHeader(cookieHeader, secret) {
  const cookies = parseCookies(cookieHeader || "");
  const raw = cookies[SESSION_COOKIE_NAME];
  return verifySessionToken(raw, secret);
}

export function getSessionSecret() {
  return process.env.SESSION_SECRET || "";
}

export function getPasswordConfig() {
  const basic =
    process.env.ACCESS_PASSWORD_BASIC ||
    process.env.PASSWORD_1 ||
    process.env.BASIC_PASSWORD ||
    "";
  const advanced =
    process.env.ACCESS_PASSWORD_ADVANCED ||
    process.env.PASSWORD_2 ||
    process.env.ADVANCED_PASSWORD ||
    "";
  return { basic, advanced };
}

export function hasPasswordConfig() {
  const cfg = getPasswordConfig();
  return Boolean(cfg.basic && cfg.advanced);
}

export function resolveRoleByPassword(password) {
  if (!password) return null;
  const cfg = getPasswordConfig();
  if (!cfg.basic || !cfg.advanced) return null;
  if (safeEqualStrings(password, cfg.advanced)) return "advanced";
  if (safeEqualStrings(password, cfg.basic)) return "basic";
  return null;
}

export function hasRequiredRole(role, required) {
  if (!role) return false;
  if (required === "basic") return role === "basic" || role === "advanced";
  return role === "advanced";
}

export function sanitizeNextPath(rawNext) {
  if (!rawNext || typeof rawNext !== "string") return "/";
  if (!rawNext.startsWith("/")) return "/";
  if (rawNext.startsWith("//")) return "/";
  return rawNext;
}

function isSecureCookie() {
  return process.env.NODE_ENV !== "development";
}

export function setSessionCookie(res, token) {
  const attrs = [
    `${SESSION_COOKIE_NAME}=${encodeURIComponent(token)}`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
    `Max-Age=${SESSION_TTL_SECONDS}`,
  ];
  if (isSecureCookie()) attrs.push("Secure");
  res.setHeader("Set-Cookie", attrs.join("; "));
}

export function clearSessionCookie(res) {
  const attrs = [
    `${SESSION_COOKIE_NAME}=`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
    "Max-Age=0",
  ];
  if (isSecureCookie()) attrs.push("Secure");
  res.setHeader("Set-Cookie", attrs.join("; "));
}

export function sendJson(res, statusCode, body) {
  res.statusCode = statusCode;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(body));
}

export async function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
    });
    req.on("end", () => {
      if (!raw) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(raw));
      } catch (err) {
        reject(err);
      }
    });
    req.on("error", reject);
  });
}
