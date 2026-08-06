import * as vscode from 'vscode';

/**
 * Minimal VS Code extension that registers a Copilot Chat participant
 * for Lumena memory. Requires VS Code 1.90+ with chat participant API.
 */
export function activate(context: vscode.ExtensionContext) {
  const participant = vscode.chat.createChatParticipant(
    'copilot.lumena',
    async (request, _context, response, _token) => {
      const query = request.prompt;

      // Assemble context from Lumena
      try {
        const res = await fetch('http://localhost:8848/assemble', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query, top_k: 5 }),
        });
        if (!res.ok) {
          response.markdown(`Lumena server error: ${res.statusText}. Is \\"lumena serve\\" running?`);
          return;
        }
        const data = await res.json();
        if (data.assembled_context) {
          response.markdown(`**Lumena context retrieved:**\\n\\n${data.assembled_context}`);
        } else {
          response.markdown('No relevant memories found in the Lumena palace.');
        }
      } catch (err) {
        response.markdown(
          `Failed to reach Lumena server at localhost:8848. ` +
          `Please start it with: \`lumena serve\``
        );
      }
    }
  );

  participant.iconPath = new vscode.ThemeIcon('database');
  participant.description = 'Lumena Memory Palace';

  context.subscriptions.push(participant);
}
