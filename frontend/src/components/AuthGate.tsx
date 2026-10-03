import { useCallback, useEffect, useState } from "react";
import App from "../App";
import { AUTH_EXPIRED_EVENT, fetchSession, signOut, type SessionUser } from "../api";
import SignInPage from "../pages/SignInPage";

type GateState =
  | { status: "checking" }
  | { status: "signed-out"; notice: string | null }
  | { status: "signed-in"; user: SessionUser };

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
        ? { status: "signed-out", notice: "Your session ended. Sign in again." }
        : current);
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired);
  }, []);

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
  if (state.status === "signed-out") {
    return <SignInPage notice={state.notice} onSignedIn={(user) => setState({ status: "signed-in", user })} />;
  }
  return <App key={state.user.id} sessionUser={state.user} onSignOut={handleSignOut} />;
}
