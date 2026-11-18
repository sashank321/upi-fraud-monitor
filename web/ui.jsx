import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import SpotlightCard from "./react-bits/SpotlightCard.jsx";
import GradientText from "./react-bits/GradientText.jsx";
import "./app.css";
import "./workspace.css";

const views = { sender: "Sender", receiver: "Receiver", bank: "Bank operations", guide: "How it works" };
const icons = { sender: "↗", receiver: "↙", bank: "◎", guide: "≡" };
const role = location.pathname.slice(1) || "login";
const parameters = new URLSearchParams(location.search);
const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" });
const displayMoney = (amount) => amount === undefined ? "—" : money.format(amount / 100);
const timestamp = (seconds) => new Date(seconds * 1000).toLocaleTimeString("en-IN");
const statuses = { screening: "Screening", held: "Held for agent review", settled: "Settled",
  cancelled: "Reversed", expired: "Expired and refunded" };
const layaModel = "convaiinnovations/laya";
const isPreviousDecision = (turn) => turn.agent === "laya" && turn.model !== layaModel;

async function api(path, body, { signal, headers } = {}) {
  const response = await fetch(path, { signal, ...(body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body),
  }) });
  const data = await response.json();
  if (!response.ok) throw Object.assign(new Error(data.error || "Request failed."), { status: response.status });
  return data;
}

function useFeed(path, interval = 0) {
  const [value, setValue] = useState({ data: null, error: "", path });
  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    let timer;
    async function poll() {
      try {
        const data = await api(path, undefined, { signal: controller.signal });
        if (!controller.signal.aborted) setValue({ data, error: "", path });
      } catch (error) {
        if (controller.signal.aborted) return;
        if ([401, 403].includes(error.status)) location.replace("/");
        else setValue((current) => ({ data: current.path === path ? current.data : null, error: error.message, path }));
      }
      if (interval && !controller.signal.aborted) timer = setTimeout(poll, interval);
    }
    poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, interval]);
  return value.path === path ? value : { data: null, error: "" };
}

function Card({ children, dark = false, className = "", ...props }) {
  return <SpotlightCard theme={dark ? "dark" : "light"} spotlightColor={dark ? "#8979f2" : "#a294ff"}
    intensity={.3} proximity={28} className={`panel bit-panel ${dark ? "bit-dark" : ""} ${className}`} {...props}>
    {children}
  </SpotlightCard>;
}

function Intro({ title, children, action }) {
  return <Card className="react-intro"><div><h1><GradientText colors={["#1b2245", "#593ca8", "#263d83"]} animationSpeed={20}>{title}</GradientText></h1>
    <p>{children}</p></div>{action}</Card>;
}

function Notice({ children, error = false }) {
  return children ? <p className={`inline-notice ${error ? "notice-error" : ""}`} role="status">{children}</p> : null;
}

function Badge({ status }) {
  return <span className={`badge ${status}`}>{statuses[status] || status}</span>;
}

function Login({ accounts, choice }) {
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState(choice || "bank");
  useEffect(() => { if (choice) { setUsername(choice); setPassword(""); } }, [choice]);
  async function signIn(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const { user } = await api("/api/login", { username, password, remember });
      const next = parameters.get("next");
      location.replace(user.role === "bank" && /^\/bank\?case=[a-f0-9]{32}$/i.test(next || "") ? next : "/" + user.role);
    } catch (error) {
      setError(error.message);
      setBusy(false);
    }
  }
  return <div className="access-panel react-login">
    <Card dark className="access-info"><div><span className="mark" aria-hidden="true"><span /><span /><span /></span>
      <h1><GradientText colors={["#ffffff", "#cbb9ff", "#95f5df"]}>UPI Fraud Monitor</GradientText></h1>
      <p>Send a transfer and follow the agents from first assessment to settlement or reversal.</p></div><a href="/guide">See how it works ↗</a></Card>
    <div className="access-form"><h2>Sign in</h2><p>Sign in as the bank operator to use the role switcher.</p>
      <form onSubmit={signIn}>
        <label htmlFor="username">Demo account</label>
        <select id="username" name="username" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)}>
          {accounts.map((account) => <option key={account.username} value={account.username}>{account.name} · {account.username}</option>)}
        </select>
        <label htmlFor="password">Password</label>
        <input id="password" name="password" type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
        <label className="checkbox"><input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} /> Remember me on this browser</label>
        <button className="primary full-width" disabled={busy || !accounts.length}>Sign in <span aria-hidden="true">↗</span></button>
      </form><Notice error>{error}</Notice></div>
  </div>;
}

function Switcher({ dialog, user, accounts, filter, setChoice }) {
  const [error, setError] = useState("");
  async function activate(account) {
    setError("");
    try {
      if (user?.username === account.username) location.assign("/" + account.role);
      else if (user?.can_switch) {
        const { user: active } = await api("/api/session/switch", { username: account.username });
        location.assign("/" + active.role);
      } else if (role === "login") {
        setChoice(account.username);
        dialog.current.close();
      } else {
        location.assign("/login?" + new URLSearchParams({ account: account.username, switch: "1" }));
      }
    } catch (error) { setError(error.message); }
  }
  async function signOut() {
    try { await api("/api/logout", {}); location.replace("/login"); }
    catch (error) { setError(error.message); }
  }
  return <dialog ref={dialog} className="modal" aria-labelledby="switch-title"><Card className="modal-box">
    <div className="modal-heading"><h2 id="switch-title">{user ? "Switch account" : "Choose an account"}</h2>
      <form method="dialog"><button className="quiet modal-close" aria-label="Close account picker">×</button></form></div>
    {Object.keys(views).filter((target) => target !== "guide" && (!filter || target === filter)).map((target) =>
      <div className="account-group" key={target}><h3>{views[target]}</h3>
        {accounts.filter((account) => account.role === target).map((account) => <button key={account.username} type="button"
          className="account-option" onClick={() => activate(account)}><span className="account-avatar" aria-hidden="true">{icons[target]}</span>
          <span className="account-option-text"><strong>{account.name}</strong><small>{account.username}</small></span>
          {account.username === user?.username && <span className="current-account">Current</span>}</button>)}
      </div>)}
    {user && <div className="modal-footer"><button className="quiet" type="button" onClick={signOut}>Log out</button></div>}
    <Notice error>{error}</Notice>
  </Card><form method="dialog" className="modal-backdrop" aria-hidden="true"><button tabIndex={-1} /></form></dialog>;
}

function Transfers({ items, current, choose, empty = "No payments yet." }) {
  if (!items.length) return <p className="empty-state">{empty}</p>;
  return <div className="transfer-list">{items.map((item) => {
    const content = <><span><strong>{item.recipient}</strong><small>{timestamp(item.created_at)}</small></span>
      <span className="transfer-side"><strong>{displayMoney(item.amount)}</strong><Badge status={item.status} /></span></>;
    return choose ? <button className={`transfer-row ${current === item.id ? "selected" : ""}`} aria-pressed={current === item.id}
      key={item.id} onClick={() => choose(item.id)}>{content}</button> :
      <div className="transfer-row" key={item.id}>{content}</div>;
  })}</div>;
}

function Sender() {
  const { data, error } = useFeed("/api/sender", 900);
  const [payee, setPayee] = useState("");
  const [amount, setAmount] = useState("");
  const [errorText, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [receipt, setReceipt] = useState("");
  const idempotency = useRef("");
  async function send(event) {
    event.preventDefault();
    if (busy) return;
    if (!/^\d{1,6}(\.\d{1,2})?$/.test(amount.trim())) return setError("Enter a rupee amount with up to two decimal places.");
    const [whole, fraction = ""] = amount.trim().split(".");
    idempotency.current ||= Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
    setBusy(true);
    setError("");
    try {
      const { id } = await api("/api/transfers", { payee, amount: Number(whole) * 100 + Number(fraction.padEnd(2, "0")) },
        { headers: { "Idempotency-Key": idempotency.current } });
      idempotency.current = "";
      setAmount("");
      setReceipt(id);
    } catch (error) { setError(error.message); }
    finally { setBusy(false); }
  }
  const payment = data?.transfers.find((item) => item.id === receipt);
  return <><Intro title="Send a payment">Track the transfer as the agents screen and resolve it.</Intro><Notice error>{error}</Notice>
    <div className="columns">
      <Card><h2>New transfer</h2><form className="transfer-form" onSubmit={send}>
        <label htmlFor="payee">Recipient</label><select id="payee" value={payee} required onChange={(event) => { setPayee(event.target.value); idempotency.current = ""; }}>
          <option value="">Choose a recipient</option>{data?.recipients.map((account) => <option value={account.id} key={account.id}>{account.name} · {account.address}</option>)}
        </select>
        <label htmlFor="amount">Amount (₹)</label><input id="amount" value={amount} inputMode="decimal" maxLength={10} placeholder="0.00" required
          onChange={(event) => { setAmount(event.target.value); idempotency.current = ""; }} />
        <button className="primary full-width" disabled={busy || !data}>{busy ? "Submitting…" : "Submit payment"} <span aria-hidden="true">↗</span></button>
      </form><Notice error>{errorText}</Notice>
      {receipt && <div className="live-receipt" role="status"><Badge status={payment?.status || "screening"} />
        {payment && <span>{displayMoney(payment.amount)} to {payment.recipient}</span>}</div>}</Card>
      <Card dark className="wallet-card"><h2>Sender balance</h2><dl className="balances"><div><dt>Available</dt><dd>{displayMoney(data?.account.available)}</dd></div>
        <div><dt>Reserved</dt><dd>{displayMoney(data?.account.reserved)}</dd></div></dl><p>Screening reserves the amount until the agents settle or reverse it.</p></Card>
    </div><Card className="timeline"><h2>Outgoing payments</h2><Transfers items={data?.transfers || []} /></Card></>;
}

function Receiver({ user }) {
  const { data, error } = useFeed("/api/receiver", 900);
  const account = data?.account?.id === user.account ? data.account : null;
  const [notice, setNotice] = useState("");
  useEffect(() => { if (data && !account) location.reload(); }, [data, account]);
  return <><Intro title="Receive payments">{account?.name || user.name}</Intro><Notice error>{error}</Notice><div className="columns">
    <Card><h2>Receiver profile</h2><p className="profile-address">{account?.address || "Loading…"}</p>
      <button className="quiet" disabled={!account} onClick={async () => {
        try { await navigator.clipboard.writeText(account.address); setNotice("UPI ID copied."); }
        catch { setNotice(account.address); }
      }}>Copy UPI ID</button><Notice>{notice}</Notice></Card>
    <Card dark className="wallet-card"><h2>Credited balance</h2><dl className="balances"><div><dt>Available</dt><dd>{displayMoney(account?.available)}</dd></div></dl>
      <p>The receiver is credited when an automatic settlement completes.</p></Card></div>
    <Card className="timeline"><h2>Incoming payments</h2><Transfers items={account ? data.transfers : []} /></Card></>;
}

function AgentFlow({ payment, turns = [], agents = [] }) {
  if (payment && !payment.phase) return <p>This is a historical payment from before agent orchestration.</p>;
  const live = ["screening", "held"].includes(payment?.status);
  const held = payment?.considered_hold;
  const reviewers = agents.filter((agent) => agent.id !== "laya");
  const panelComplete = reviewers.length > 0 && reviewers.every((agent) =>
    turns.some((turn) => turn.stage === "review" && turn.agent === agent.id));
  const screening = turns.find((turn) => turn.stage === "screen");
  const final = turns.find((turn) => turn.stage === "final");
  const waitingForDecision = payment?.status === "held" && panelComplete;
  return <ol className="automation-flow" aria-label="Payment and agent flow">
    {[
      [screening && isPreviousDecision(screening) ? "Hosted agent screens" : "Laya screens",
        "Sender and recipient history", payment?.status === "screening" ? "active" : payment ? "done" : ""],
      ["Agent discussion", "Coordinator and five specialists",
        payment?.status === "held" && !panelComplete ? "active" : held ? "done" : ""],
      ["Final decision", final && isPreviousDecision(final) ? "Hosted agent weighs the evidence" : "Laya weighs the panel evidence",
        waitingForDecision ? "active" : !live && held ? "done" : ""],
      ["Ledger posts", "Settle or reverse the reservation", payment && !live ? "done" : ""],
    ].map(([name, detail, state], index) => <li className={state} key={name}>
      <span className="automation-dot" aria-hidden="true">{state === "done" ? "✓" : index + 1}</span><strong>{name}</strong><small>{detail}</small>
    </li>)}
  </ol>;
}

function openBotLink(base, agent = "fraud-investigator") {
  if (!base) return "";
  const url = new URL("/channel/new", base);
  url.hostname = location.hostname;
  url.searchParams.set("agent", agent);
  return url.href;
}

function Conversation({ payment, detail, agents, openbot }) {
  const [copied, setCopied] = useState(false);
  const thread = useRef(null);
  const follow = useRef(true);
  useEffect(() => setCopied(false), [payment.id]);
  useEffect(() => {
    if (follow.current && thread.current) thread.current.scrollTop = thread.current.scrollHeight;
  }, [detail?.turns.length]);
  const actor = (turn) => isPreviousDecision(turn) ? "Previous decision agent" :
    agents.find((agent) => agent.id === turn.agent)?.name || (turn.agent === "system" ? "Ledger protection" : "Recorded agent");
  const previousFinal = detail?.turns.some((turn) => turn.stage === "final" && isPreviousDecision(turn));
  const prompt = `Watch ${payment.id}`;
  return <Card className="conversation-card">
    <div className="panel-title"><h2>Case conversation</h2><Badge status={payment.status} /></div>
    <div className="case-head"><div><strong>{displayMoney(payment.amount)}</strong><p>{payment.recipient}</p></div></div>
    {openbot && <div className="openbot-actions">
      <a className="quiet link-button" target="_blank" rel="noopener" href={openBotLink(openbot)}>Watch in OpenBot ↗</a>
      <button className="quiet" onClick={async () => {
        try { await navigator.clipboard.writeText(prompt); setCopied(true); }
        catch { document.getElementById("watch-prompt").select(); }
      }}>{copied ? "Copied" : "Copy watch prompt"}</button>
      <input id="watch-prompt" aria-label="OpenBot watch prompt" readOnly value={prompt} onFocus={(event) => event.target.select()} />
    </div>}
    <ol ref={thread} className="agent-thread" aria-label="Agent messages" tabIndex={0} onScroll={(event) => {
      const box = event.currentTarget;
      follow.current = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
    }}>{detail?.turns.map((turn) => <li key={turn.sequence}>
      <span className="thread-avatar" aria-hidden="true">{actor(turn).slice(0, 1)}</span><div>
        <div className="thread-meta"><strong>{actor(turn)} → {previousFinal && turn.audience === "Laya" ? "Previous decision agent" : turn.audience}</strong><small>{timestamp(turn.created_at)}</small></div>
        <code>{turn.model || "Ledger"}</code><p><strong className="turn-decision">{turn.stage === "review" ? "Advisory · " : ""}{turn.decision}</strong> · {turn.summary}</p>
        {turn.assessment?.checkpoint_revision && <small className="fallback-label">
          Checkpoint <code title={turn.assessment.checkpoint_revision}>{turn.assessment.checkpoint_revision.slice(0, 12)}</code>
          {Object.entries(turn.assessment.probabilities || {}).map(([choice, probability]) =>
            <span key={choice}> · {choice} {(probability * 100).toFixed(1)}%</span>)}
        </small>}
      </div></li>)}</ol>
    {detail && !detail.turns.length && <p className="muted">{payment.phase ? "Waiting for Laya’s assessment." : payment.review_note || "This payment predates the agent conversation."}</p>}
    {detail?.review_note && !detail.turns.length && <p className="inline-notice">{detail.review_note}</p>}
  </Card>;
}

function SenderBalance({ account, reset }) {
  const dialog = useRef(null);
  const [confirmedBalance, setConfirmedBalance] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  if (!account || !reset) return null;
  const blocked = account.reserved > 0;
  const atTarget = account.available === reset.target;
  async function restore(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api("/api/bank/reset-sender-balance", { expected_available: confirmedBalance });
      setMessage(result.changed ? `Sender balance reset to ${displayMoney(result.available)}.` : "The balance was already at the starting amount.");
      dialog.current.close();
    } catch (error) {
      setError(error.message);
    } finally {
      setBusy(false);
    }
  }
  return <Card className="sender-balance-card">
    <div className="sender-balance-row"><div><h2>Sender demo balance</h2>
      <p><strong>{displayMoney(account.available)} available</strong> · {displayMoney(account.reserved)} reserved</p></div>
      <button type="button" className="quiet" disabled={busy || blocked || atTarget} onClick={() => {
        setConfirmedBalance(account.available);
        setError("");
        setMessage("");
        dialog.current.showModal();
      }}>Reset sender balance</button>
    </div>
    {blocked ? <p className="muted">Wait for reserved payments to finish before resetting.</p> :
      atTarget && !message ? <p className="muted">The sender is at the starting balance.</p> : null}
    {reset.last && <p className="muted">Last reset: {displayMoney(reset.last.previous_available)} → {displayMoney(reset.last.available)} · {new Date(reset.last.created_at * 1000).toLocaleString("en-IN")} · {reset.last.actor}</p>}
    <Notice>{message}</Notice>
    <dialog ref={dialog} className="modal" aria-labelledby="reset-title" onCancel={(event) => { if (busy) event.preventDefault(); }}>
      <Card className="modal-box"><div className="modal-heading"><h2 id="reset-title">Reset sender balance?</h2></div>
        <p>Set the available demo balance from {confirmedBalance === null ? "—" : displayMoney(confirmedBalance)} to {displayMoney(reset.target)}. Receiver balances and payment history stay the same.</p>
        <form className="balance-reset-actions" onSubmit={restore}>
          <button type="button" className="quiet" autoFocus disabled={busy} onClick={() => dialog.current.close()}>Cancel</button>
          <button type="submit" className="primary" disabled={busy}>{busy ? "Resetting…" : "Confirm balance reset"}</button>
        </form><Notice error>{error}</Notice>
      </Card>
    </dialog>
  </Card>;
}

function Bank() {
  const [selected, setSelected] = useState(/^[a-f0-9]{32}$/i.test(parameters.get("case") || "") ? parameters.get("case").toLowerCase() : "");
  const { data, error } = useFeed(selected ? "/api/bank?case=" + selected : "/api/bank", 900);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const transfers = data?.transfers || [];
  const payment = transfers.find((item) => item.id === selected) || transfers[0];
  const { data: detail, error: detailError } = useFeed(payment ? "/api/bank/case/" + payment.id : null, 750);
  const shown = transfers.filter((item) => (filter === "all" ||
    (filter === "active" ? ["held", "screening"].includes(item.status) : item.status === filter)) &&
    `${item.recipient} ${item.id} ${item.amount / 100}`.toLowerCase().includes(search.trim().toLowerCase()));
  function choose(id) { setSelected(id); history.replaceState(null, "", "/bank?case=" + id); }
  return <><Intro title="Bank operations">Follow the autonomous decisions and read what each agent contributed.</Intro>
    <Notice error>{error || detailError}</Notice>
    <div className="bank-stats">{[["Screening", "screening"], ["Held for agents", "held"], ["Settled", "settled"]].map(([label, value]) =>
      <Card className="react-stat" key={value}><span className="stat-title">{label}</span>
        <strong className="stat-value">{transfers.filter((item) => item.status === value).length}</strong></Card>)}</div>
    <SenderBalance account={data?.accounts.find((account) => account.id === "demo-payer")} reset={data?.sender_balance_reset} />
    <Card dark className="flow-card"><div className="panel-title"><h2>Live payment flow</h2>{payment && <Badge status={payment.status} />}</div>
      <AgentFlow payment={payment} turns={detail?.turns} agents={data?.agents} /></Card>
    <div className="columns bank-columns">
      <Card><div className="panel-title"><h2>Transfer queue</h2><span className="count">{shown.length}</span></div>
        <input type="search" aria-label="Find a transfer" placeholder="Search recipient, amount, or case ID" value={search}
          onChange={(event) => setSearch(event.target.value)} />
        <div className="queue-filters" aria-label="Filter transfers">{[["all", "All"], ["active", "In progress"], ["settled", "Settled"], ["cancelled", "Reversed"]]
          .map(([value, label]) => <button key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</button>)}</div>
        <Transfers items={shown} current={payment?.id} choose={choose} empty="No transfers match this view." /></Card>
      {payment ? <Conversation payment={payment} detail={detail} agents={data.agents} openbot={data.openbot} /> :
        <Card><h2>Case conversation</h2><p className="muted">Select a transfer when it arrives.</p></Card>}
    </div><details className="panel timeline agent-roster"><summary>Agents and models</summary>
      <ol className="roster">{data?.agents.map((agent) => <li key={agent.id}><strong>{agent.name}</strong>
        <span>{agent.model || "Awaiting configuration"}</span></li>)}</ol></details></>;
}

function Guide() {
  const { data, error } = useFeed("/api/guide");
  return <><Intro title="How the monitor works">The simulation records each decision and the evidence passed between agents.</Intro><Notice error>{error}</Notice>
    <Card dark className="flow-card"><h2>Agent communication</h2><AgentFlow /></Card>
    <ol className="agent-grid" aria-label="Order of agent messages">{data?.agents.map((agent) => <li key={agent.id}><Card>
      <h2>{agent.name}</h2><code>{agent.model || "Awaiting provider configuration"}</code>
      {agent.fallback_model && <small className="fallback-label">Fallback · {agent.fallback_model}</small>}<p>{agent.role}</p>
      {agent.source && <a className="fallback-label" href={agent.source} target="_blank" rel="noopener noreferrer">View Laya source ↗</a>}
    </Card></li>)}</ol><Card className="timeline"><h2>Evidence and settlement</h2>
      <p>The local Laya model screens each transfer using the simulated sender and recipient history. For a hold, the hosted Investigation Coordinator reviews the screening evidence, then five hosted specialists build on each other’s findings. Laya uses the findings to decide whether to settle or reverse.</p>
      <p>The case conversation records which checkpoint made each Laya decision. Its choice probabilities describe the model’s answer; they are not a measured probability of fraud.</p>
      <p>The ledger reserves funds during screening. An incomplete review or expired decision window returns the reservation to the sender.</p>
    </Card></>;
}

function App() {
  const { data, error } = useFeed("/api/session");
  const user = data?.user;
  const accounts = data?.accounts || [];
  const dialog = useRef(null);
  const [filter, setFilter] = useState("");
  const [choice, setChoice] = useState(parameters.get("account") || (parameters.get("next") === "/sender" ? "sender" : "bank"));
  const showPicker = (group = "") => {
    setFilter(group);
    if (!dialog.current?.open && accounts.length) dialog.current?.showModal();
  };
  useEffect(() => {
    function shortcut(event) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && accounts.length) {
        event.preventDefault();
        showPicker();
      }
    }
    document.addEventListener("keydown", shortcut);
    return () => document.removeEventListener("keydown", shortcut);
  }, [accounts.length]);
  useEffect(() => {
    if (role === "login" && user && !parameters.has("switch") && !parameters.has("account")) location.replace("/" + user.role);
  }, [user]);
  return <div className="react-shell"><a className="skip-link" href="#main-content">Skip to content</a>
    <aside className="workspace-menu"><a className="brand" href="/"><span className="mark" aria-hidden="true"><span /><span /><span /></span>
      <span>UPI Fraud Monitor</span></a><nav className="workspace-nav" aria-label="Interfaces">{Object.entries(views).map(([id, name]) =>
        <a key={id} href={"/" + id} aria-current={role === id ? "page" : undefined} onClick={(event) => {
          if (id === "guide" || user?.role === id || !accounts.length || event.ctrlKey || event.metaKey || event.shiftKey) return;
          event.preventDefault(); showPicker(id);
        }}><span className="nav-icon" aria-hidden="true">{icons[id]}</span><span>{name}</span></a>)}</nav></aside>
    <div><header className="workspace-bar"><strong>{views[role] || "Account access"}</strong>
      <div className="workspace-actions"><span className="environment">Simulated funds</span><button className="account-trigger" aria-haspopup="dialog" onClick={() => showPicker()}>
        <span className="account-avatar" aria-hidden="true">⇄</span><span><strong>{user?.name || "Choose account"}</strong><small>{user ? "Switch account" : "Sign in"}</small></span>
      </button></div></header>
      <main id="main-content"><Notice error>{error}</Notice>{role === "guide" ? <Guide /> :
        role === "login" ? <Login accounts={accounts} choice={choice} /> :
          user?.role === role ? ({ sender: <Sender />, receiver: <Receiver user={user} />, bank: <Bank /> })[role] : null}</main>
    </div><Switcher dialog={dialog} user={user} accounts={accounts} filter={filter} setChoice={setChoice} /></div>;
}

createRoot(document.getElementById("root")).render(<App />);
