/** React escapes all text. No HTML parser ever receives captured prompts. */
export function LeakHighlightedPrompt({ text }: { text: string | null }) {
  if (text === null) return <p className="text-sm text-gray-600">Raw prompt capture is disabled. Its SHA-256 audit hash is retained.</p>;
  const parts = text.split(/(smtp_|fs_|db_lookup|jira_ticket|mock:\/\/|MockProvider|preferred_fast_secure)/gi);
  return (
    <pre className="text-xs text-gray-800 whitespace-pre-wrap font-mono p-4 bg-gray-50 rounded border overflow-auto">
      {parts.map((part, index) => index % 2
        ? <mark key={index} className="bg-red-500 text-white font-bold px-1 rounded">{part}</mark>
        : part)}
    </pre>
  );
}
