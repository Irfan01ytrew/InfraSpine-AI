#!/usr/bin/env node
/** API acceptance for two clean synthetic customer installations from one local image. */
import { createPrivateKey, sign } from "node:crypto";
import { readFileSync } from "node:fs";

const fixturePath = process.env.INFRASPINE_CUSTOMER_INSTALL_FIXTURE;
if (!fixturePath) throw new Error("Set INFRASPINE_CUSTOMER_INSTALL_FIXTURE to the protected synthetic fixture.");
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const customers = [fixture.customer_a, fixture.customer_b];
if (customers.some((x) => !x?.base_url || !x?.email || !x?.password || !x?.name || !x?.brand)) {
  throw new Error("The protected fixture is incomplete.");
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  }
  return value;
}

async function call(base, method, route, { body, cookie } = {}) {
  const response = await fetch(new URL(route, base), {
    method,
    headers: {
      "X-Requested-With": "XMLHttpRequest",
      ...(cookie ? { Cookie: cookie } : {}),
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  let payload = null;
  try { payload = await response.json(); } catch {}
  return { response, payload };
}

async function login(customer) {
  const { response } = await call(customer.base_url, "POST", "/api/auth/login", {
    body: { email: customer.email, password: customer.password },
  });
  if (!response.ok) throw new Error(`Synthetic administrator login failed for ${customer.name} (HTTP ${response.status}).`);
  const setCookies = response.headers.getSetCookie?.() ?? [response.headers.get("set-cookie") ?? ""];
  const cookie = setCookies.map((line) => line.split(";", 1)[0]).filter(Boolean).join("; ");
  if (!cookie) throw new Error(`The ${customer.name} deployment did not set an authenticated session cookie.`);
  const me = await call(customer.base_url, "GET", "/api/auth/me", { cookie });
  if (!me.response.ok || me.payload?.email !== customer.email || me.payload?.role !== "admin") {
    throw new Error(`The ${customer.name} deployment authenticated the wrong user.`);
  }
  return cookie;
}

async function signedLicense(customer, deploymentId, features) {
  const issued = new Date();
  issued.setUTCMilliseconds(0);
  const expires = new Date(issued.getTime() + 30 * 24 * 60 * 60 * 1000);
  const iso = (date) => date.toISOString().replace(".000Z", "Z");
  const payload = {
    product: "infraspine-ai",
    license_id: `qa-${customer.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}-${deploymentId.slice(0, 8)}`,
    customer: customer.name,
    deployment_id: deploymentId,
    issued_at: iso(issued),
    expires_at: iso(expires),
    seat_limit: 5,
    features,
  };
  const signature = sign(null, Buffer.from(JSON.stringify(canonical(payload))), createPrivateKey(fixture.issuer_private_key_pem));
  return { payload, signature: signature.toString("base64") };
}

const profiles = [];
const identities = [];
const licenses = [];
const cookies = [];
for (let index = 0; index < customers.length; index++) {
  const customer = customers[index];
  const initial = await call(customer.base_url, "GET", "/api/deployment/profile");
  const expectedInitialState = index === 0 ? true : false;
  if (!initial.response.ok || initial.payload?.setup_complete !== expectedInitialState) {
    throw new Error(`The clean ${customer.name} installation did not start with first-run setup required.`);
  }
  const cookie = await login(customer);
  cookies.push(cookie);
  const profile = {
    company_name: customer.name,
    industry: customer.industry,
    brand_name: customer.brand,
    primary_color: customer.primary_color,
    setup_complete: true,
  };
  if (index === 0) {
    if (initial.payload?.company_name !== customer.name || initial.payload?.brand_name !== customer.brand) {
      throw new Error(`The ${customer.name} browser wizard did not persist its own branding.`);
    }
  } else {
    const saved = await call(customer.base_url, "PUT", "/api/admin/deployment/company", { cookie, body: profile });
    if (!saved.response.ok || saved.payload?.brand_name !== customer.brand) {
      throw new Error(`The ${customer.name} setup API did not persist its own branding.`);
    }
  }
  const publicProfile = await call(customer.base_url, "GET", "/api/deployment/profile");
  if (!publicProfile.response.ok || publicProfile.payload?.company_name !== customer.name ||
      publicProfile.payload?.brand_name !== customer.brand) {
    throw new Error(`The ${customer.name} branding was not isolated in its installation data.`);
  }
  profiles.push(publicProfile.payload);

  const overview = await call(customer.base_url, "GET", "/api/admin/deployment", { cookie });
  const deploymentId = overview.payload?.license?.deployment_id;
  if (!overview.response.ok || typeof deploymentId !== "string") {
    throw new Error(`The ${customer.name} installation did not create a persistent deployment identity.`);
  }
  identities.push(deploymentId);
  const features = customer === customers[0] ? ["research"] : ["reporting"];
  licenses.push(await signedLicense(customer, deploymentId, features));
}

if (identities[0] === identities[1]) throw new Error("The two installations share a deployment identity.");
if (profiles[0].brand_name === profiles[1].brand_name || profiles[0].company_name === profiles[1].company_name) {
  throw new Error("The two installations share company branding state.");
}

const crossLogin = await call(customers[1].base_url, "POST", "/api/auth/login", {
  body: { email: customers[0].email, password: customers[0].password },
});
if (crossLogin.response.ok) throw new Error("Customer A's administrator authenticated in Customer B.");

const rejected = await call(customers[1].base_url, "PUT", "/api/admin/deployment/license", {
  cookie: cookies[1], body: licenses[0],
});
if (rejected.response.ok) throw new Error("Customer A's signed license was accepted by Customer B.");
const bBefore = await call(customers[1].base_url, "GET", "/api/admin/deployment", { cookie: cookies[1] });
if (bBefore.payload?.license?.status !== "missing") {
  throw new Error("The cross-customer license attempt changed Customer B's license state.");
}

for (let index = 0; index < customers.length; index++) {
  const installed = await call(customers[index].base_url, "PUT", "/api/admin/deployment/license", {
    cookie: cookies[index], body: licenses[index],
  });
  if (!installed.response.ok || installed.payload?.status !== "valid" ||
      installed.payload?.deployment_id !== identities[index]) {
    throw new Error(`The signed offline license did not activate for ${customers[index].name}.`);
  }
}
for (let index = 0; index < customers.length; index++) {
  const customer = customers[index];
  const users = await call(customer.base_url, "GET", "/api/admin/users", { cookie: cookies[index] });
  const expectedEmails = index === 0 ? [customer.email, fixture.customer_a.employee_email] : [customer.email];
  if (!users.response.ok || !Array.isArray(users.payload?.users) || users.payload.users.length !== expectedEmails.length ||
      expectedEmails.some((email) => !users.payload.users.some((user) => user.email === email))) {
    throw new Error(`The ${customer.name} installation did not contain only its expected synthetic users.`);
  }
  const documents = await call(customer.base_url, "GET", "/api/rag/documents", { cookie: cookies[index] });
  if (!documents.response.ok || !Array.isArray(documents.payload?.documents) || documents.payload.documents.length !== 0) {
    throw new Error(`The clean ${customer.name} installation did not have an empty isolated knowledge base.`);
  }
}
const finalA = await call(customers[0].base_url, "GET", "/api/admin/deployment", { cookie: cookies[0] });
const finalB = await call(customers[1].base_url, "GET", "/api/admin/deployment", { cookie: cookies[1] });
if (finalA.payload?.license?.license_id === finalB.payload?.license?.license_id ||
    finalA.payload?.license?.features?.join(",") !== "research" ||
    finalB.payload?.license?.features?.join(",") !== "reporting") {
  throw new Error("Per-customer offline license records or entitlements crossed installations.");
}

console.log("CUSTOMER_API_LOGIN_AND_USER_ISOLATION=PASS");
console.log("CUSTOMER_SETUP_BRANDING_AND_DEPLOYMENT_ID_ISOLATION=PASS");
console.log("CROSS_CUSTOMER_LOGIN_AND_LICENSE_REJECTION=PASS");
console.log("SIGNED_OFFLINE_LICENSE_AND_ENTITLEMENTS_ISOLATION=PASS");
