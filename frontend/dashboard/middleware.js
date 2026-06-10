const SESSION_COOKIE_NAME = "polysport_session";
const ADVANCED_PATH_PREFIXES = ["/leader-attribution"];

let cachedSecret = null;
let cachedKeyPromise = null;

function parseCookies(cookieHeader = "") {
  const out = {};
  const parts = cookieHeader.split(";");
  for (const part of parts) {
    const idx = part.indexOf("=");
    if (idx <= 0) continue;
    const k = part.slice(0, idx).trim();
    const v = part.slice(idx + 1).trim();
    if (!k) continue;
    try {
      out[k] = decodeURIComponent(v);
    } catch {
      out[k] = v;
    }
  }
  return out;
}

function isStaticAsset(pathname) {
  return pathname !== "/index.html" && /\.[a-zA-Z0-9]+$/.test(pathname);
}

function shouldSkip(pathname) {
  if (pathname === "/login") return true;
  if (pathname.startsWith("/api/")) return true;
  if (pathname.startsWith("/_vercel/")) return true;
  if (pathname.startsWith("/assets/")) return true;
  if (pathname.startsWith("/.well-known/")) return true;
  if (pathname === "/favicon.ico" || pathname === "/robots.txt" || pathname === "/sitemap.xml") return true;
  if (isStaticAsset(pathname)) return true;
  return false;
}

function requiredRoleForPath(pathname) {
  if (ADVANCED_PATH_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`))) {
    return "advanced";
  }
  return "basic";
}

function hasRequiredRole(role, requiredRole) {
  if (!role) return false;
  if (requiredRole === "basic") return role === "basic" || role === "advanced";
  return role === "advanced";
}

function toBase64Url(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function fromBase64Url(base64url) {
  const normalized = base64url.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4 || 4)) % 4);
  const binary = atob(padded);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    out[i] = binary.charCodeAt(i);
  }
  return out;
}

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

async function getHmacKey(secret) {
  if (cachedSecret === secret && cachedKeyPromise) return cachedKeyPromise;
  cachedSecret = secret;
  cachedKeyPromise = crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  return cachedKeyPromise;
}

async function signPayload(payloadBase64, secret) {
  const key = await getHmacKey(secret);
  const sigBuffer = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(payloadBase64));
  return toBase64Url(new Uint8Array(sigBuffer));
}

async function verifySessionToken(token, secret) {
  if (!token || !secret) return null;
  const parts = token.split(".");
  if (parts.length !== 2) return null;
  const [payloadBase64, signature] = parts;

  const expected = await signPayload(payloadBase64, secret);
  if (!safeEqual(signature, expected)) return null;

  try {
    const payloadBytes = fromBase64Url(payloadBase64);
    const payload = JSON.parse(new TextDecoder().decode(payloadBytes));
    if (!payload || typeof payload !== "object") return null;
    if (payload.role !== "basic" && payload.role !== "advanced") return null;
    if (typeof payload.exp !== "number" || Date.now() > payload.exp) return null;
    return payload;
  } catch {
    return null;
  }
}

function buildLoginUrl(currentUrl, requiredRole) {
  const url = new URL("/login", currentUrl);
  url.searchParams.set("required", requiredRole);
  url.searchParams.set("next", `${currentUrl.pathname}${currentUrl.search}`);
  return url;
}

export default async function middleware(request) {
  const currentUrl = new URL(request.url);
  const pathname = currentUrl.pathname;
  if (shouldSkip(pathname)) return;

  const secret = process.env.SESSION_SECRET || "";
  const requiredRole = requiredRoleForPath(pathname);
  if (!secret) {
    return Response.redirect(buildLoginUrl(currentUrl, requiredRole), 307);
  }

  const cookies = parseCookies(request.headers.get("cookie") || "");
  const token = cookies[SESSION_COOKIE_NAME];
  const session = await verifySessionToken(token, secret);

  if (!session || !hasRequiredRole(session.role, requiredRole)) {
    return Response.redirect(buildLoginUrl(currentUrl, requiredRole), 307);
  }
}
