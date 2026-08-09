import { useEffect, useState } from "react";

type Bootstrap = {
  case_id: string;
  delegation_valid_from: string;
  delegation_valid_until: string;
  delegation_command_scope: string[];
  technical_source_lines: [number, string][];
};

export function App() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/bootstrap")
      .then((response) => {
        if (!response.ok) throw new Error("bootstrap unavailable");
        return response.json() as Promise<Bootstrap>;
      })
      .then(setBootstrap)
      .catch(() => setFailure("Workshop setup failed. Check the local server configuration."));
  }, []);

  return (
    <main className="workshop-shell">
      <section aria-labelledby="voice-title">
        <p className="eyebrow">Live specification room</p>
        <h1 id="voice-title">CSV Export Workshop</h1>
        <p>Voice and transcript controls arrive in the next build slice.</p>
        <p role="status">{failure ?? (bootstrap ? "Foundation ready" : "Validating evidence…")}</p>
      </section>
      <section aria-labelledby="source-title">
        <p className="eyebrow">Delegated technical evidence</p>
        <h2 id="source-title">Dev Lead pre-read</h2>
        {bootstrap && (
          <p>
            Scope valid {new Date(bootstrap.delegation_valid_from).toLocaleDateString()}–
            {new Date(bootstrap.delegation_valid_until).toLocaleDateString()} inclusive
          </p>
        )}
        <div className="source-lines" aria-label="Technical specification">
          {bootstrap?.technical_source_lines.slice(0, 30).map(([line, text]) => (
            <p id={`line-${line}`} key={line} tabIndex={-1}>
              <span aria-hidden="true">{line}</span>{text}
            </p>
          ))}
        </div>
      </section>
      <section aria-labelledby="package-title">
        <p className="eyebrow">Governed package</p>
        <h2 id="package-title">Spec Package</h2>
        <p>Confirmed items, evidence, readiness, and review obligations will appear here.</p>
      </section>
    </main>
  );
}
