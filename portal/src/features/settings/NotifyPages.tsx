import { useState } from "react";
import { usePageTitle } from "../../app/usePageTitle";
import { ErrorCallout, Loading } from "../../components/Feedback";
import { formatDateTime } from "../../lib/format";
import { Fact, StatusBadge, TestOutcome } from "./common";
import { useEmailSettings, useTeamsSettings, useTestCard, useTestEmail } from "./queries";

export function TeamsPage() {
  usePageTitle("Settings: Teams");
  const teams = useTeamsSettings();
  const send = useTestCard();
  const [testId, setTestId] = useState<string | null>(null);
  if (teams.isPending) return <Loading />;
  if (teams.isError) return <ErrorCallout error={teams.error} />;
  const t = teams.data;
  return (
    <section className="stack" aria-labelledby="teams-h">
      <h2 id="teams-h">Teams</h2>
      <p className="lede">
        Recall cards go to people in Teams from the OmniFile bot, which signs in with its own
        managed identity.
      </p>
      <div className="card card-pad stack">
        <h3>Bot health</h3>
        <p>
          <StatusBadge status={t.health.status} /> {t.health.message}
        </p>
        <dl className="kv">
          <Fact label="Teams app id">
            <span className="mono">{t.teams_app_id ?? "not set"}</span>
          </Fact>
          <Fact label="Bot app id">
            <span className="mono">{t.bot_app_id ?? "not set"}</span>
          </Fact>
          <Fact label="Cards sent (24 h)">{t.health.cards_sent_24h}</Fact>
          <Fact label="Cards failed (24 h)">{t.health.cards_failed_24h}</Fact>
          <Fact label="Last card">{formatDateTime(t.health.last_sent_at)}</Fact>
          <Fact label="Last error">{t.health.last_error ?? "none"}</Fact>
          <Fact label="Chats blocked by the tenant">{t.health.conversations_blocked}</Fact>
        </dl>
      </div>
      <div className="card card-pad stack">
        <h3>App package</h3>
        {t.package.available ? (
          <p>
            <a
              className="btn"
              href="/v1/settings/teams/package"
              download={t.package.file_name ?? true}
            >
              Download {t.package.file_name ?? "the Teams app package"}
            </a>
          </p>
        ) : null}
        {t.package.instructions.length ? (
          <ol>
            {t.package.instructions.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ol>
        ) : null}
      </div>
      <div className="card card-pad stack">
        <h3>Test</h3>
        <p>Sends a card to you, and only you, through the same path recall cards take.</p>
        <div>
          <button
            type="button"
            className="btn btn-primary"
            disabled={send.isPending}
            onClick={() => send.mutate(undefined, { onSuccess: (r) => setTestId(r.test_id) })}
          >
            {send.isPending ? "Sending…" : "Send me a test card"}
          </button>
        </div>
        {send.isError ? <ErrorCallout error={send.error} title="The test wasn't sent" /> : null}
        <TestOutcome testId={testId} />
      </div>
    </section>
  );
}

export function EmailPage() {
  usePageTitle("Settings: email fallback");
  const email = useEmailSettings();
  const send = useTestEmail();
  const [testId, setTestId] = useState<string | null>(null);
  if (email.isPending) return <Loading />;
  if (email.isError) return <ErrorCallout error={email.error} />;
  const e = email.data;
  return (
    <section className="stack" aria-labelledby="email-h">
      <h2 id="email-h">Email fallback</h2>
      <p className="lede">
        When Teams can&apos;t reach someone, OmniFile emails them from one sender mailbox, through
        Microsoft Graph.
      </p>
      <dl className="kv card card-pad">
        <Fact label="Configured">{e.configured ? "Yes" : "Not yet"}</Fact>
        <Fact label="Sender mailbox">{e.sender ?? "not set"}</Fact>
        <Fact label="Your address">{e.my_mail ?? "not known yet"}</Fact>
        <Fact label="Sent (24 h)">{e.health.sent_24h}</Fact>
        <Fact label="Failed (24 h)">{e.health.failed_24h}</Fact>
        <Fact label="Last sent">{formatDateTime(e.health.last_sent_at)}</Fact>
        <Fact label="Last error">{e.health.last_error ?? "none"}</Fact>
      </dl>
      {e.instructions.length ? (
        <ol className="card card-pad">
          {e.instructions.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ol>
      ) : null}
      <div className="card card-pad stack">
        <h3>Test</h3>
        <div>
          <button
            type="button"
            className="btn btn-primary"
            disabled={send.isPending}
            onClick={() => send.mutate(undefined, { onSuccess: (r) => setTestId(r.test_id) })}
          >
            {send.isPending ? "Sending…" : "Send me a test email"}
          </button>
        </div>
        {send.isError ? <ErrorCallout error={send.error} title="The test wasn't sent" /> : null}
        <TestOutcome testId={testId} />
      </div>
    </section>
  );
}
