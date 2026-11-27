import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { api, onUnauthorized } from "./api";
import { initials, setRegionalSettings } from "./format";
import { IconLogOut, IconLogo, Spinner } from "./icons";
import type { CompanyProfile, User } from "./types";

const Console = lazy(() => import("./components/AdminConsole").then((module) => ({ default: module.Console })));
const CompanySetupPage = lazy(() => import("./components/CompanySetupPage").then((module) => ({ default: module.CompanySetupPage })));
const LoginPage = lazy(() => import("./components/LoginPage").then((module) => ({ default: module.LoginPage })));
const AccountSecurityPage = lazy(() => import("./components/AccountSecurityPage").then((module) => ({ default: module.AccountSecurityPage })));
const OnboardingPage = lazy(() => import("./components/OnboardingPage").then((module) => ({ default: module.OnboardingPage })));
const PartnerPortalPage = lazy(() => import("./components/PartnerPortalPage").then((module) => ({ default: module.PartnerPortalPage })));

export function App() {
  const [hash, setHash] = useState(() => window.location.hash);

  useEffect(() => {
    const onHash = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (hash === "#/partner" || hash.startsWith("#/partner?") || hash.startsWith("#/partner/")) {
    return <Suspense fallback={<LoadingScreen />}><PartnerPortalPage /></Suspense>;
  }
  return <Suspense fallback={<LoadingScreen />}><InternalApp /></Suspense>;
}

function LoadingScreen() {
  return <div className="fullscreen-center"><Spinner size={22} /></div>;
}

function InternalApp() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [brandName, setBrandName] = useState("InfraSpine AI");
  const [companyProfile, setCompanyProfile] = useState<CompanyProfile | null>(null);
  const [profileLoading, setProfileLoading] = useState(true);
  const [invitationToken, setInvitationToken] = useState(() => readInvitationToken());

  const refreshCompanyProfile = useCallback(async (showLoader = true) => {
    if (showLoader) setProfileLoading(true);
    try {
      const profile = await api.companyProfile();
      setRegionalSettings(profile);
      setCompanyProfile(profile);
      if (profile.setup_complete) {
        setBrandName(profile.brand_name);
        document.title = profile.brand_name;
        document.documentElement.style.setProperty("--brand", profile.primary_color);
      }
    } catch {
      // Keep the existing shell available when the optional public profile endpoint is unavailable.
      setCompanyProfile(null);
    } finally {
      if (showLoader) setProfileLoading(false);
    }
  }, []);

  useEffect(() => {
    onUnauthorized(() => setUser(null));
    api
      .me()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setChecking(false));
    void refreshCompanyProfile();
  }, [refreshCompanyProfile]);

  useEffect(() => {
    const onHash = () => setInvitationToken(readInvitationToken());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  async function signOut() {
    await api.logout().catch(() => undefined);
    setUser(null);
  }

  async function signedIn(signedInUser: User) {
    setUser(signedInUser);
    await refreshCompanyProfile();
  }

  async function companyProfileSaved() {
    const firstRunSetup = companyProfile?.setup_complete === false;
    if (firstRunSetup) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#/`);
    }
    // A normal profile edit should refresh branding without unmounting the Admin page.
    // First-run setup still uses the loader to switch into the configured workspace.
    await refreshCompanyProfile(firstRunSetup);
  }

  if (invitationToken) return <OnboardingPage token={invitationToken} onAccepted={() => {
    setInvitationToken(null);
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#/`);
  }} />;
  if (checking) {
    return (
      <div className="fullscreen-center">
        <Spinner size={22} />
      </div>
    );
  }
  if (!user) return <LoginPage brandName={brandName} onSignedIn={signedIn} />;
  if (user.mfa_reauthentication_required) {
    return <div className="first-run-setup">
      <div className="first-run-setup-header"><span className="brand"><span className="brand-mark"><IconLogo size={16} /></span>{brandName}</span><UserChip user={user} onSignOut={signOut} /></div>
      <section className="card first-run-waiting-card"><h1>Verify MFA to continue</h1><p className="muted">Organization policy changed after this session was created. Sign out and sign in again so your authenticator is checked.</p><button className="btn btn-primary" onClick={() => void signOut()}>Sign out and verify</button></section>
    </div>;
  }
  if (user.mfa_enrollment_required) {
    return <div className="first-run-setup">
      <div className="first-run-setup-header"><span className="brand"><span className="brand-mark"><IconLogo size={16} /></span>{brandName}</span><UserChip user={user} onSignOut={signOut} /></div>
      <AccountSecurityPage user={user} onMfaEnabled={async () => setUser(await api.me())} />
    </div>;
  }
  if (profileLoading) {
    return (
      <div className="fullscreen-center">
        <Spinner size={22} />
      </div>
    );
  }
  if (companyProfile?.setup_complete === false) {
    if (user.role === "admin") {
      return (
        <div className="first-run-setup">
          <div className="first-run-setup-header">
            <span className="brand"><span className="brand-mark"><IconLogo size={16} /></span>InfraSpine AI setup</span>
            <UserChip user={user} onSignOut={signOut} />
          </div>
          <CompanySetupPage onProfileSaved={companyProfileSaved} />
        </div>
      );
    }
    return (
      <div className="first-run-waiting">
        <div className="card first-run-waiting-card">
          <h1>Company setup is required</h1>
          <p className="muted">An administrator needs to finish the company setup before this workspace is available.</p>
          <UserChip user={user} onSignOut={signOut} />
        </div>
      </div>
    );
  }
  return <Console brandName={brandName} user={user} onSignOut={signOut} onCompanyProfileSaved={companyProfileSaved} />;
}

function readInvitationToken(): string | null {
  const hash = window.location.hash;
  if (!hash.startsWith("#/onboarding?")) return null;
  return new URLSearchParams(hash.slice(hash.indexOf("?") + 1)).get("token");
}

export function UserChip({ user, onSignOut }: { user: User; onSignOut: () => void }) {
  const [isRTL, setIsRTL] = useState(() => {
    const saved = localStorage.getItem("infraspine_direction");
    if (saved) {
      document.documentElement.dir = saved;
      return saved === "rtl";
    }
    return document.documentElement.dir === "rtl";
  });

  const toggleDirection = () => {
    const next = !isRTL;
    setIsRTL(next);
    const dir = next ? "rtl" : "ltr";
    document.documentElement.dir = dir;
    localStorage.setItem("infraspine_direction", dir);
  };

  return (
    <div className="user-chip">
      <span className="avatar">{initials(user.email)}</span>
      <div className="user-chip-text">
        <span className="user-chip-email" title={user.email}>
          {user.email}
        </span>
        <span className="user-chip-role">{user.role === "admin" ? "Administrator" : user.role === "manager" ? "Department manager" : "Employee"}</span>
      </div>
      <button
        className="icon-btn"
        onClick={toggleDirection}
        aria-label={isRTL ? "Switch to Left-to-Right layout" : "Switch to Right-to-Left (Arabic/Urdu) layout"}
        title={isRTL ? "Switch to LTR" : "Switch to RTL"}
        style={{ fontSize: "10px", fontWeight: "bold" }}
      >
        {isRTL ? "LTR" : "RTL"}
      </button>
      <button className="icon-btn" onClick={onSignOut} aria-label="Sign out" title="Sign out">
        <IconLogOut size={16} />
      </button>
    </div>
  );
}
