import { useId, useState } from "react";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { AwsConnectSection } from "./AwsConnect";
import { CopyButton, Fact, TestOutcome } from "./common";
import { useAwsSettings, useAwsTest, useTrustPolicy } from "./queries";

const ROLE_ARN = /^arn:aws:iam::[0-9]{12}:role\/[A-Za-z0-9+=,.@_/-]{1,512}$/;

export function AwsPage() {
  usePageTitle("Settings: AWS");
  const aws = useAwsSettings();
  const [typed, setTyped] = useState("");
  const [arn, setArn] = useState<string | null>(null);
  const [testId, setTestId] = useState<string | null>(null);
  const trust = useTrustPolicy(arn);
  const test = useAwsTest();
  const arnId = useId();
  const hintId = useId();
  const valid = ROLE_ARN.test(typed.trim());

  if (aws.isPending) return <Loading />;
  if (aws.isError) return <ErrorCallout error={aws.error} />;
  const a = aws.data;
  const policy = trust.data ? JSON.stringify(trust.data.trust_policy, null, 2) : "";

  return (
    <section className="stack" aria-labelledby="aws-h">
      <h2 id="aws-h">AWS</h2>
      <AwsConnectSection />
      <h3 id="aws-hand-h">By hand</h3>
      <p className="muted">
        For a role you made yourself: see the trust policy it needs, and test that the service can
        assume it.
      </p>
      <Callout title="No AWS keys are stored anywhere">
        The service federates its Entra identity to the role (decision 43): AWS trusts tokens from
        your tenant for the service&apos;s identities, and gives back short-lived credentials.
      </Callout>
      <dl className="kv card card-pad">
        <Fact label="Configured">{a.configured ? "Yes" : "Not yet"}</Fact>
        <Fact label="Private networking">{a.private_network ? "On (PrivateLink)" : "Off"}</Fact>
        <Fact label="Token issuer">
          <span className="mono">{a.issuer}</span>
        </Fact>
        <Fact label="Audience">
          <span className="mono">{a.audience ?? "not set"}</span>
        </Fact>
        <Fact label="Service identities">
          {a.subjects.length ? <span className="mono">{a.subjects.join(", ")}</span> : "not set"}
        </Fact>
      </dl>
      {a.accounts.length ? (
        <div className="table-wrap card">
          <table className="table" aria-label="AWS accounts">
            <thead>
              <tr>
                <th scope="col">Account</th>
                <th scope="col">Region</th>
                <th scope="col">Role</th>
                <th scope="col">STS endpoint</th>
              </tr>
            </thead>
            <tbody>
              {a.accounts.map((acct) => (
                <tr key={acct.account_id}>
                  <th scope="row" className="mono">
                    {acct.account_id}
                  </th>
                  <td>{acct.region}</td>
                  <td className="mono">{acct.role_arn ?? "—"}</td>
                  <td className="mono">{acct.sts_endpoint ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <form
        className="card card-pad stack"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) {
            setArn(typed.trim());
            setTestId(null);
            test.reset();
          }
        }}
      >
        <div className="field">
          <label className="field-label" htmlFor={arnId}>
            Role ARN
          </label>
          <input
            id={arnId}
            type="text"
            className="mono"
            autoComplete="off"
            spellCheck={false}
            placeholder="arn:aws:iam::123456789012:role/omnifile-service"
            value={typed}
            aria-describedby={hintId}
            aria-invalid={typed.length > 0 && !valid}
            onChange={(e) => setTyped(e.target.value)}
          />
          <p id={hintId} className="field-hint">
            Paste the ARN of the role the service should assume. Nothing is saved: the role goes in
            the deployment&apos;s AWS settings.
          </p>
        </div>
        <div>
          <button type="submit" className="btn btn-primary" disabled={!valid}>
            Show trust policy
          </button>
        </div>
      </form>

      {trust.isError ? <ErrorCallout error={trust.error} /> : null}
      {trust.data ? (
        <div className="card card-pad stack">
          <h3>Trust policy for {trust.data.role_arn}</h3>
          <p>
            Give this to AWS as the role&apos;s trust policy. It lets only the service&apos;s
            identities, with tokens for its audience, assume the role.
          </p>
          <pre className="mono" aria-label="Trust policy JSON">
            {policy}
          </pre>
          <div>
            <CopyButton text={policy} label="Copy trust policy" />
          </div>
          <h4>Steps</h4>
          <ol>
            {trust.data.steps.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ol>
          {trust.data.configured_account ? null : (
            <Callout tone="warn">
              Account {trust.data.account_id} isn&apos;t in the service&apos;s AWS settings yet, so
              the test can&apos;t run until it is.
            </Callout>
          )}
          <div>
            <button
              type="button"
              className="btn"
              disabled={test.isPending}
              onClick={() =>
                test.mutate(trust.data.role_arn, { onSuccess: (t) => setTestId(t.test_id) })
              }
            >
              {test.isPending ? "Starting…" : "Test"}
            </button>
          </div>
          {test.isError ? <ErrorCallout error={test.error} title="The test didn't start" /> : null}
          <TestOutcome testId={testId} />
        </div>
      ) : trust.isFetching ? (
        <Loading />
      ) : null}
    </section>
  );
}
