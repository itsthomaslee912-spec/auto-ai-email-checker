import { useCallback, useEffect, useState } from "react";
import App from "../App";
import { AUTH_EXPIRED_EVENT, fetchSession, signOut, type SessionUser } from "../api";
import SignInPage from "../pages/SignInPage";

type GateState =
  | { status: "checking" }
  | { status: "signed-out"; notice: string | null }
  | { status: "signed-in"; user: SessionUser }
  | { status: "timing-out"; user: SessionUser };

const SESSION_TIMEOUT_MESSAGE = "Your sign-in session timed out.";
const SIGNOUT_AFTER_MESSAGE_MS = 2800;

export default function AuthGate() {
  const [state, setState] = useState<GateState>({ status: "checking" });

  useEffect(() => {
    let cancelled = false;
    fetchSession()
      .then((user) => {
        if (!cancelled) setState(user ? { status: "signed-in", user } : { status: "signed-out", notice: null });
      })
      .catch((err) => {
        if (!cancelled) setState({ status: "signed-out", notice: err instanceof Error ? err.message : null });
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const onExpired = () => {
      setState((current) => current.status === "signed-in"
        ? { status: "timing-out", user: current.user }
        : current);
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired);
  }, []);

  useEffect(() => {
    if (state.status !== "timing-out") return;
    const timer = window.setTimeout(() => {
      void signOut().finally(() => {
        setState({ status: "signed-out", notice: "Your session timed out. Sign in again." });
      });
    }, SIGNOUT_AFTER_MESSAGE_MS);
    return () => window.clearTimeout(timer);
  }, [state.status]);

  const handleSignOut = useCallback(async () => {
    try {
      await signOut();
    } finally {
      setState({ status: "signed-out", notice: "You signed out." });
    }
  }, []);

  if (state.status === "checking") {
    return <div className="signin-screen" aria-busy="true"><div className="signin-checking">Checking sign-in…</div></div>;
  }
  if (state.status === "timing-out") {
    return (
      <div className="signin-screen" role="alertdialog" aria-labelledby="session-timeout-title" aria-describedby="session-timeout-detail">
        <div className="signin-card session-timeout-card">
          <h1 id="session-timeout-title">Session timed out</h1>
          <p id="session-timeout-detail" className="signin-notice" role="status">{SESSION_TIMEOUT_MESSAGE}</p>
          <p className="signin-subtitle">Signing you out…</p>
        </div>
      </div>
    );
  }
  if (state.status === "signed-out") {
    return <SignInPage notice={state.notice} onSignedIn={(user) => setState({ status: "signed-in", user })} />;
  }
  return <App key={state.user.id} sessionUser={state.user} onSignOut={handleSignOut} />;
}
