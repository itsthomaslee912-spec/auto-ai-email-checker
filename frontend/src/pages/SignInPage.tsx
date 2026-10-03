import { useState, type FormEvent } from "react";
import { signIn, type SessionUser } from "../api";

export default function SignInPage({ notice, onSignedIn }: { notice?: string | null; onSignedIn: (user: SessionUser) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      onSignedIn(await signIn(email.trim(), password));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
      setPassword("");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="signin-screen">
      <form className="signin-card" onSubmit={submit} noValidate>
        <div className="signin-brand">
          <img src="/favicon.svg" alt="" />
          <span>Auto Email Check</span>
        </div>
        <h1>Sign in</h1>
        <p className="signin-subtitle">Use your sign-in account to open your email accounts and calendar.</p>
        {notice && !error && <p className="signin-notice" role="status">{notice}</p>}
        {error && <p className="signin-error" role="alert">{error}</p>}
        <label className="signin-field">
          <span>Email</span>
          <input
            type="email"
            autoComplete="username"
            autoFocus
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@example.com"
          />
        </label>
        <label className="signin-field">
          <span>Password</span>
          <span className="signin-password">
            <input
              type={showPassword ? "text" : "password"}
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
            <button type="button" className="signin-reveal" onClick={() => setShowPassword((value) => !value)} aria-label={showPassword ? "Hide password" : "Show password"}>
              {showPassword ? "Hide" : "Show"}
            </button>
          </span>
        </label>
        <button type="submit" className="signin-submit" disabled={submitting || !email.trim() || !password}>
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
