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
| Kimi | `kimi-code/k3` | thinking enabled | Local configured alias points to K3; print output may omit backend model metadata. |

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

## Sources and CLI versions

- [Codex models](https://developers.openai.com/codex/models/)
- [Codex non-interactive execution](https://developers.openai.com/codex/noninteractive)
- [Codex configuration](https://developers.openai.com/codex/config-reference)
- [Claude models, effort and modelUsage](https://code.claude.com/docs/en/model-config)
- [Claude headless execution](https://code.claude.com/docs/en/headless)
- [Kimi CLI options](https://moonshotai.github.io/kimi-cli/en/reference/kimi-command.html)
- [Kimi custom agents](https://moonshotai.github.io/kimi-cli/en/customization/agents.html)
- [Kimi print mode](https://moonshotai.github.io/kimi-cli/en/customization/print-mode.html)
- [Kimi K3 configuration](https://moonshotai.github.io/kimi-code/en/configuration/config-files.html)

Local versions checked: Claude Code 2.1.305, Codex 0.163.0, Kimi CLI 1.49.0.
These commands intentionally target the installed Python Kimi CLI; newer Kimi
implementations may use different effort/configuration flags.
