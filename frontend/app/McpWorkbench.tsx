import { useState } from "react";
import { gateway, itemPath, type Role } from "../lib/gateway";
import { useQuery } from "../lib/useGateway";
import { Empty, timestamp } from "./ConsoleViews";
import { EvidenceDisclosure, Issue, Outcome, Panel, useAction } from "./WorkbenchShared";

type Server = { id: string; endpoint: string };
type Tool = { name: string; description: string; input_schema: unknown; schema_sha256: string; compatible: boolean };
type Catalog = { tools: Tool[]; argument_mapping: Record<string, unknown> };
type Mapping = {
  adapter_id: string;
  server_id: string;
  tool: string;
  schema_sha256: string;
  revision: string | number;
  priority: number;
  enabled: boolean;
  actor?: string;
  recorded_at?: number;
};
type Registry = { servers: Server[]; mappings: Mapping[] };

function FixedArguments({ argumentsMap }: { argumentsMap: Record<string, unknown> }) {
  return <dl className="detail-meta workbench-mapping">
    {Object.entries(argumentsMap ?? {}).map(([source, target]) => <div key={source}>
      <dt><code>{source}</code></dt>
      <dd><code>{typeof target === "string" ? target : JSON.stringify(target)}</code></dd>
    </div>)}
  </dl>;
}

export function McpWorkbench({ token, role, version }: { token: string; role: Role; version: number }) {
  const [selectedServer, setSelectedServer] = useState<string | null>(null);
  const [selectedTool, setSelectedTool] = useState<string | null>(null);
  const [priority, setPriority] = useState("5");
  const [revision, setRevision] = useState(0);
  const [discoveryVersion, setDiscoveryVersion] = useState(0);
  const [notice, setNotice] = useState("");
  const { busy, error, clearError, run } = useAction();
  const reviewer = role === "reviewer";
  const registry = useQuery<Registry>(reviewer ? "/mcp" : null, token, version + revision);
  const servers = registry.data?.servers ?? [];
  const server = servers.find(item => item.id === selectedServer) ?? servers[0];
  const catalog = useQuery<Catalog>(server && reviewer ? `${itemPath("/mcp/servers", server.id)}/tools` : null,
    token, version + discoveryVersion);
  const tools = catalog.data?.tools ?? [];
  const tool = tools.find(item => item.name === selectedTool) ?? tools.find(item => item.compatible) ?? tools[0];
  const priorityNumber = Number(priority);
  const priorityValid = priority.length > 0 && Number.isInteger(priorityNumber) &&
    priorityNumber >= 0 && priorityNumber <= 1000;
  const alreadyEnabled = registry.data?.mappings.some(mapping => mapping.enabled && mapping.server_id === server?.id &&
    mapping.tool === tool?.name && mapping.schema_sha256 === tool.schema_sha256 && mapping.priority === priorityNumber);

  function selectServer(id: string) {
    setSelectedServer(id);
    setSelectedTool(null);
    setNotice("");
    clearError();
  }

  async function save(values: { server_id: string; tool: string; schema_sha256: string; priority: number; enabled: boolean }) {
    await run("mapping", async () => {
      setNotice("");
      const stored = await gateway<Mapping>("/mcp/mappings", token, { method: "POST", body: JSON.stringify(values) });
      setNotice(values.enabled ? `Mapping ${stored.adapter_id} enabled.` : `Mapping ${stored.adapter_id} disabled.`);
      setRevision(value => value + 1);
    });
  }

  if (!reviewer) return <Panel title="MCP registry">
    <Empty icon="approvals" title="Reviewer access required">Reviewers can inspect server capabilities and change approved mappings.</Empty>
  </Panel>;

  return <div className="view-stack">
    <Issue error={registry.error || error} />
    {notice && <p className="workbench-notice" role="status">{notice}</p>}
    <div className="workbench-layout registry-layout">
      <div className="view-stack">
        <Panel title="Allowlisted servers">
          {registry.data ? servers.length ? <ul className="approval-list registry-list">
            {servers.map(item => <li key={item.id}>
              <button type="button" disabled={!!busy} aria-pressed={server?.id === item.id} onClick={() => selectServer(item.id)}>
                <span><strong>{item.id}</strong><small><code>{item.endpoint}</code></small></span>
              </button>
            </li>)}
          </ul> : <Empty icon="mcp" title="No MCP servers configured">See the project README for local MCP provisioning.</Empty> :
            !registry.error && <p className="list-status" role="status">Loading server registry…</p>}
        </Panel>
        <Panel title="Published mappings">
          {registry.data ? registry.data.mappings.length ? <ul className="registry-mappings">
            {registry.data.mappings.map(mapping => <li key={mapping.adapter_id}>
              <div className="registry-entry-head">
                <strong>{mapping.adapter_id}</strong><Outcome value={mapping.enabled ? "enabled" : "disabled"} />
              </div>
              <code>{mapping.server_id} / {mapping.tool}</code>
              <dl className="detail-meta">
                <div><dt>Priority</dt><dd>{mapping.priority}</dd></div>
                <div><dt>Revision</dt><dd><code>{mapping.revision}</code></dd></div>
                {mapping.actor && <div><dt>Reviewed by</dt><dd>{mapping.actor}</dd></div>}
                {mapping.recorded_at && <div><dt>Recorded</dt><dd>{timestamp(mapping.recorded_at)}</dd></div>}
              </dl>
              {mapping.enabled && <button type="button" className="text-button" disabled={!!busy}
                onClick={() => void save({ server_id: mapping.server_id, tool: mapping.tool,
                  schema_sha256: mapping.schema_sha256, priority: mapping.priority, enabled: false })}>
                Disable mapping
              </button>}
            </li>)}
          </ul> : <Empty icon="mcp" title="No mappings approved">Approved mappings and their revisions appear here.</Empty> :
            !registry.error && <p className="list-status" role="status">Loading mappings…</p>}
        </Panel>
      </div>
      <Panel title="Capability inspection" accessory={server &&
        <button type="button" className="button secondary compact" disabled={!!busy}
          onClick={() => setDiscoveryVersion(value => value + 1)}>Rediscover</button>}>
        {server ? <div className="workbench-body">
          <Issue error={catalog.error} />
          {catalog.data ? !tool ? <p className="muted">This server advertised no tools.</p> : <>
            <label className="input-group"><span>Advertised tool</span>
              <select value={tool.name} disabled={!!busy} onChange={event => setSelectedTool(event.target.value)}>
                {tools.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}
              </select>
            </label>
            <div className="registry-entry-head">
              <strong>Discovery result</strong><Outcome value={tool.compatible ? "compatible" : "incompatible"} />
            </div>
            {tool.description && <p className="untrusted-description">{tool.description}</p>}
            <dl className="detail-meta">
              <div><dt>Schema hash</dt><dd><code>{tool.schema_sha256}</code></dd></div>
            </dl>
            <EvidenceDisclosure title="Advertised input schema" value={tool.input_schema} />
            {tool.compatible && <form className="view-stack" onSubmit={event => {
              event.preventDefault();
              if (!busy && priorityValid && !alreadyEnabled) void save({
                server_id: server.id, tool: tool.name, schema_sha256: tool.schema_sha256, priority: priorityNumber, enabled: true,
              });
            }}>
              <h3>Trusted argument mapping</h3>
              <FixedArguments argumentsMap={catalog.data.argument_mapping} />
              <label className="input-group registry-priority"><span>Routing priority</span>
                <input type="number" min="0" max="1000" step="1" value={priority} required
                  onChange={event => setPriority(event.target.value)} disabled={!!busy} />
              </label>
              {alreadyEnabled && <p className="muted" role="status">This tool is already enabled for the current schema and routing priority.</p>}
              <div className="workbench-action-row">
                <button className="button primary" disabled={!!busy || !priorityValid || alreadyEnabled}>
                  {busy ? "Saving…" : "Enable mapping"}
                </button>
              </div>
            </form>}
          </> : !catalog.error && <p className="muted" role="status">Discovering capabilities…</p>}
        </div> : <Empty icon="mcp" title="No server selected">Configure a local server to inspect its capabilities.</Empty>}
      </Panel>
    </div>
  </div>;
}
