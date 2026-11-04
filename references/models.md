# Frontier model selection

Verified on 2026-10-20 against official documentation and the installed CLIs.
With `auto_update_models: true` (default), initialization and new launches refresh
the strongest model profile from official metadata and local model lists. Active
reviewer calls refresh the profile when its cache is due. Metadata is cached for
30 minutes. Model releases and entitlements change; login alone does not prove
access to a selected model.

| Participant | Current default | Reasoning | Verification |
| --- | --- | --- | --- |
| Claude Code | `claude-fable-5-1` | `max` | `modelUsage` keys in the CLI JSON result identify actual model usage. |
| Codex | `gpt-6-astra` | `max` | Explicit model flag; current CLI JSON events do not always expose the returned model. |
| Kimi Code | `kimi-code/k3` | maximum supported thinking | Official Code CLI; selected alias is separate from served-model observation, which may remain unknown. |

`max` keeps Codex focused on the consultation; `ultra` enables automatic task
delegation. The strongest Claude model can have different entitlement or credit
requirements from the account default. An inaccessible selected model uses Kimi;
the helper does not silently replace it with a smaller Claude/Codex model.

The `models` object supplies the initial profile; `active_models` records audited
updates without changing the original goal, criteria or budgets. Set
`auto_update_models: false` to intentionally pin an explicitly chosen run profile.
On metadata/network failure the helper retains the last verified profile and
records a warning rather than inventing a newer model. Model access failures still
use the documented Kimi fallback. The running driver takes an updated model on its
next launch/handoff; skill text does not hot-swap an active session.

Keep the requested model, observed model (when available), actual provider
and fallback reason in the audit record. A model saying its own name is not model
attestation. The helper cannot independently prove a remote provider's routing.

The same policy applies to the **driver**. Start it with:

```bash
python3 /path/to/ccloop/scripts/launch.py --driver codex \
  --project /work/project --goal 'The original goal'
python3 /path/to/ccloop/scripts/launch.py --driver claude \
  --project /work/project --goal 'The original goal'
```

Explicit launcher model overrides intentionally pin that launch profile
(`auto_update_models: false`). Omit them for normal automatic frontier updates.
An already running session keeps its model until its client changes it or a new
driver is launched.

```bash
python3 /path/to/ccloop/scripts/ccloop.py --root /work/private-supervisor models --refresh
```

Model metadata refresh does not upgrade CLI binaries, change global account
settings or authorize paid-usage changes.

## Kimi Code client

Normal Kimi calls use the official **Kimi Code CLI**, with configuration under
`~/.kimi-code/` (or `KIMI_CODE_HOME`). They do not route through OMK or implicitly
substitute the archived Python CLI. `doctor` reports the selected client kind
and config source. Run `kimi-code --version` for the installed Code alias.

The reviewer uses an isolated Code home, a Markdown agent with no tools or
subagents, and `--prompt`/`--output-format stream-json`. The full request is in
the private agent context because Code prompt mode does not consume stdin.
Managed work uses ACP and normal per-operation permissions, not prompt mode's
automatic tool policy. Use `ccloop --driver kimi --goal 'The original goal'`;
the Code driver requires managed mode (the default). Open the Code TUI directly
for a separate interactive session.

Original Code credentials are referenced locally; Python OAuth tokens are not
copied or migrated by ccloop. If Code is not logged in, use `kimi-code login` on
that computer. Model aliases and cached aliases are checked against the selected
client's config. An old Python-only cache cannot select a missing Code alias.
Python 3.11+ uses stdlib TOML parsing; Python 3.10 can use optional `tomli` without
changing the Kimi Code client itself.

Explicit legacy prefixes remain compatible for existing installations; they are
never the normal automatic fallback. To select one intentionally, set the exact
`CCLOOP_KIMI_BIN` and, if necessary, `CCLOOP_KIMI_FLAVOR=python`.

## Sources and CLI versions

- [Codex models](https://developers.openai.com/codex/models/)
- [Codex non-interactive execution](https://developers.openai.com/codex/noninteractive)
- [Codex configuration](https://developers.openai.com/codex/config-reference)
- [Claude models, effort and modelUsage](https://code.claude.com/docs/en/model-config)
- [Claude headless execution](https://code.claude.com/docs/en/headless)
- [Kimi Code command](https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html)
- [Kimi Code custom agents](https://moonshotai.github.io/kimi-code/en/customization/agents.html)
- [Kimi Code ACP](https://moonshotai.github.io/kimi-code/en/reference/kimi-acp.html)
- [Kimi K3 configuration](https://moonshotai.github.io/kimi-code/en/configuration/config-files.html)

Kimi Code CLI 2.4.1 is the verified modern interface. Legacy Python Kimi CLI
1.49 uses different flags and Wire transport, retained only for explicit
compatibility. Other versions may require capability verification.
