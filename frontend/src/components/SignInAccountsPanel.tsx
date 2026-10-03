import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  changeOwnPassword,
  createSignInAccount,
  deleteSignInAccount,
  fetchSignInAccounts,
  updateSignInAccount,
  type SessionUser,
  type SignInAccount,
} from "../api";

const MIN_PASSWORD = 3;

function formatWhen(value: string | null): string {
  if (!value) return "Never";
  return new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
}

export default function SignInAccountsPanel({ sessionUser, onSignOut }: { sessionUser: SessionUser; onSignOut: () => void }) {
  return (
    <div className="settings-panel">
      <OwnAccountBlock sessionUser={sessionUser} onSignOut={onSignOut} />
      {sessionUser.is_admin && <ManageAccountsBlock sessionUser={sessionUser} />}
    </div>
  );
}

function OwnAccountBlock({ sessionUser, onSignOut }: { sessionUser: SessionUser; onSignOut: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (next.length < MIN_PASSWORD) { setMessage({ kind: "error", text: `New password must be at least ${MIN_PASSWORD} characters.` }); return; }
    if (next !== confirm) { setMessage({ kind: "error", text: "New passwords do not match." }); return; }
    setSaving(true);
    setMessage(null);
    try {
      await changeOwnPassword(current, next);
      setCurrent(""); setNext(""); setConfirm("");
      setMessage({ kind: "ok", text: "Password changed. Other devices were signed out." });
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : "Could not change password" });
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="settings-block">
      <div className="settings-block-heading">
        <div>
          <h3>Your sign-in account</h3>
          <p className="settings-help">Email accounts, mail, and calendar items you see belong to this sign-in account.</p>
        </div>
        <button type="button" className="action-btn" onClick={onSignOut}>Sign out</button>
      </div>
      <div className="signin-self">
        <span className="signin-self-avatar" aria-hidden="true">{(sessionUser.display_name || sessionUser.email).slice(0, 1).toUpperCase()}</span>
        <span className="signin-self-text">
          <strong>{sessionUser.display_name || sessionUser.email}</strong>
          <span>{sessionUser.email}</span>
        </span>
        <span className={`signin-role${sessionUser.is_admin ? " is-admin" : ""}`}>{sessionUser.is_admin ? "Admin" : "Member"}</span>
      </div>
      <form className="signin-form-grid" onSubmit={submit}>
        <label><span>Current password</span><input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required /></label>
        <label><span>New password</span><input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required minLength={MIN_PASSWORD} /></label>
        <label><span>Confirm new password</span><input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required /></label>
        <button type="submit" className="action-btn primary" disabled={saving || !current || !next || !confirm}>{saving ? "Saving…" : "Change password"}</button>
      </form>
      {message && <p className={message.kind === "ok" ? "signin-form-ok" : "signin-form-error"} role="status">{message.text}</p>}
    </section>
  );
}

function ManageAccountsBlock({ sessionUser }: { sessionUser: SessionUser }) {
  const [accounts, setAccounts] = useState<SignInAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [newEmail, setNewEmail] = useState("");
  const [newName, setNewName] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newAdmin, setNewAdmin] = useState(false);
  const [creating, setCreating] = useState(false);
  const [resetFor, setResetFor] = useState<number | null>(null);
  const [resetPassword, setResetPassword] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      setAccounts(await fetchSignInAccounts());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load sign-in accounts");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const run = async (id: number | null, action: () => Promise<string>) => {
    setBusyId(id);
    setError(null);
    setNotice(null);
    try {
      setNotice(await action());
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusyId(null);
    }
  };

  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (newPassword.length < MIN_PASSWORD) { setError(`Password must be at least ${MIN_PASSWORD} characters.`); return; }
    setCreating(true);
    await run(null, async () => {
      const created = await createSignInAccount({ email: newEmail.trim(), password: newPassword, display_name: newName.trim() || undefined, is_admin: newAdmin });
      setNewEmail(""); setNewName(""); setNewPassword(""); setNewAdmin(false);
      return `Added ${created.email}. They can sign in now and add their own email accounts.`;
    });
    setCreating(false);
  };

  return (
    <section className="settings-block">
      <div className="settings-block-heading">
        <div>
          <h3>Sign-in accounts</h3>
          <p className="settings-help">People who can open this app. Each sign-in account has its own list of email accounts; nobody sees another account’s mail.</p>
        </div>
      </div>

      <form className="signin-form-grid signin-add-form" onSubmit={create}>
        <label><span>Email</span><input type="email" value={newEmail} onChange={(e) => setNewEmail(e.target.value)} placeholder="person@example.com" required autoComplete="off" /></label>
        <label><span>Name (optional)</span><input type="text" value={newName} onChange={(e) => setNewName(e.target.value)} autoComplete="off" /></label>
        <label><span>Password</span><input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} required minLength={MIN_PASSWORD} autoComplete="new-password" /></label>
        <label className="signin-check"><input type="checkbox" checked={newAdmin} onChange={(e) => setNewAdmin(e.target.checked)} /><span>Admin</span></label>
        <button type="submit" className="action-btn primary" disabled={creating || !newEmail.trim() || !newPassword}>{creating ? "Adding…" : "+ Add sign-in account"}</button>
      </form>

      {error && <p className="signin-form-error" role="alert">{error}</p>}
      {notice && <p className="signin-form-ok" role="status">{notice}</p>}

      {loading ? <p className="settings-help">Loading…</p> : (
        <ul className="signin-account-list">
          {accounts.map((account) => {
            const self = account.id === sessionUser.id;
            const busy = busyId === account.id;
            return (
              <li key={account.id} className="signin-account-row">
                <span className="signin-self-avatar" aria-hidden="true">{(account.display_name || account.email).slice(0, 1).toUpperCase()}</span>
                <span className="signin-account-main">
                  <strong>{account.display_name || account.email}{self && <em> (you)</em>}</strong>
                  <span>{account.email}</span>
                </span>
                <span className={`signin-role${account.is_admin ? " is-admin" : ""}`}>{account.is_admin ? "Admin" : "Member"}</span>
                <span className="signin-account-meta">
                  <span>{account.mailbox_count} email account{account.mailbox_count === 1 ? "" : "s"}</span>
                  <span>Last sign-in: {formatWhen(account.last_login_at)}</span>
                </span>
                <span className="signin-account-actions">
                  {resetFor === account.id ? (
                    <form className="signin-inline-form" onSubmit={(event) => {
                      event.preventDefault();
                      if (resetPassword.length < MIN_PASSWORD) { setError(`Password must be at least ${MIN_PASSWORD} characters.`); return; }
                      void run(account.id, async () => {
                        await updateSignInAccount(account.id, { password: resetPassword });
                        setResetFor(null); setResetPassword("");
                        return `Password updated for ${account.email}.`;
                      });
                    }}>
                      <input type="password" value={resetPassword} onChange={(e) => setResetPassword(e.target.value)} placeholder="New password" autoFocus autoComplete="new-password" aria-label={`New password for ${account.email}`} />
                      <button type="submit" className="action-btn primary" disabled={busy || !resetPassword}>Save</button>
                      <button type="button" className="action-btn" onClick={() => { setResetFor(null); setResetPassword(""); }}>Cancel</button>
                    </form>
                  ) : confirmDelete === account.id ? (
                    <>
                      <span className="signin-confirm-text">Delete and remove {account.mailbox_count} email account{account.mailbox_count === 1 ? "" : "s"}?</span>
                      <button type="button" className="action-btn danger" disabled={busy} onClick={() => void run(account.id, async () => {
                        const result = await deleteSignInAccount(account.id);
                        setConfirmDelete(null);
                        return `Deleted ${account.email}${result.removed_mailboxes ? ` and removed ${result.removed_mailboxes} email account(s)` : ""}.`;
                      })}>{busy ? "Deleting…" : "Delete"}</button>
                      <button type="button" className="action-btn" onClick={() => setConfirmDelete(null)}>Cancel</button>
                    </>
                  ) : (
                    <>
                      <button type="button" className="action-btn" onClick={() => { setResetFor(account.id); setConfirmDelete(null); setResetPassword(""); }}>Reset password</button>
                      {!self && (
                        <button type="button" className="action-btn" disabled={busy} onClick={() => void run(account.id, async () => {
                          await updateSignInAccount(account.id, { is_admin: !account.is_admin });
                          return `${account.email} is now ${account.is_admin ? "a member" : "an admin"}.`;
                        })}>{account.is_admin ? "Make member" : "Make admin"}</button>
                      )}
                      {!self && <button type="button" className="action-btn danger" onClick={() => { setConfirmDelete(account.id); setResetFor(null); }}>Delete</button>}
                    </>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
