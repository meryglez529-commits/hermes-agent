# Upstream Sync With Model Integration Preservation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move this fork onto the latest `upstream/main` while preserving the current model integration surface, including custom provider resolution, runtime provider routing, model switching, fallback routing, provider-specific request headers, and SoulStore gateway support.

**Architecture:** Treat the fork's model integration as a bounded patch stack, not as a repo-wide merge. Start every sync from a fresh branch at the latest `upstream/main`, then replay the local model layer in dependency order: config and provider identity, runtime credential resolution, `/model` provider switching, client construction, main-agent wiring, and optional gateway-specific headers. Future updates must reuse the same patch boundaries and the same verification suite so each sync stays deterministic and reviewable.

**Tech Stack:** Git (`fetch`, `worktree`, `format-patch`, `cherry-pick`/manual replay), Python, pytest, Hermes CLI/runtime provider stack, `models.dev`, OpenAI/Anthropic/Codex adapters.

---

### Task 1: Freeze The Current Source Of Truth

**Files:**
- Reference: `run_agent.py`
- Reference: `agent/auxiliary_client.py`
- Reference: `agent/soulstore_gateway.py`
- Reference: `hermes_cli/providers.py`
- Reference: `hermes_cli/runtime_provider.py`
- Reference: `hermes_cli/model_switch.py`
- Reference: `hermes_cli/models.py`
- Reference: `hermes_cli/config.py`
- Reference: `agent/models_dev.py`
- Reference: `agent/model_metadata.py`
- Reference: `cli.py`
- Test: `tests/hermes_cli/test_user_providers_model_switch.py`
- Test: `tests/run_agent/test_fallback_model.py`
- Test: `tests/run_agent/test_provider_parity.py`
- Test: `tests/run_agent/test_soulstore_headers.py`
- Test: `tests/hermes_cli/test_config.py`

- [ ] **Step 1: Create a preservation branch from the current dirty state**

```bash
git switch -c wip/model-integration-preservation
git status --short
```

Expected: the branch is `wip/model-integration-preservation` and all current model-related modified or untracked files are visible.

- [ ] **Step 2: Commit the currently-uncommitted model-routing work before any upstream sync**

```bash
git add \
  agent/soulstore_gateway.py \
  agent/auxiliary_client.py \
  run_agent.py \
  cli.py \
  hermes_cli/config.py \
  tests/hermes_cli/test_user_providers_model_switch.py \
  tests/run_agent/test_soulstore_headers.py \
  tests/hermes_cli/test_config.py
git commit -m "feat(model-routing): preserve local provider and gateway integrations"
```

Expected: SoulStore support and explicit user-provider `/model --provider` behavior are no longer only in the working tree.

- [ ] **Step 3: Export a patch backup for the preserved model surface**

```bash
mkdir -p tmp/upstream-sync-preservation
git format-patch --stdout upstream/main -- \
  run_agent.py \
  agent/auxiliary_client.py \
  agent/soulstore_gateway.py \
  hermes_cli/providers.py \
  hermes_cli/runtime_provider.py \
  hermes_cli/model_switch.py \
  hermes_cli/models.py \
  hermes_cli/config.py \
  agent/models_dev.py \
  agent/model_metadata.py \
  cli.py \
  tests/hermes_cli/test_user_providers_model_switch.py \
  tests/run_agent/test_fallback_model.py \
  tests/run_agent/test_provider_parity.py \
  tests/run_agent/test_soulstore_headers.py \
  tests/hermes_cli/test_config.py \
  > tmp/upstream-sync-preservation/model-integration.patch
```

Expected: `tmp/upstream-sync-preservation/model-integration.patch` exists as an emergency replay artifact.

### Task 2: Start Every Sync From A Fresh Upstream Worktree

**Files:**
- Reference: `.git`
- Reference: `docs/plans/2026-05-08-upstream-sync-model-integration-plan.md`

- [ ] **Step 1: Fetch the latest upstream refs and tags**

```bash
git fetch upstream --prune --tags
git show -s --format='%h %ci %s' upstream/main
```

Expected: the displayed commit is the exact upstream base that this sync will target.

- [ ] **Step 2: Create a clean sync worktree from `upstream/main`**

```bash
git worktree add ../hermes-agent-sync-$(date +%Y%m%d) \
  -b sync/upstream-main-$(date +%Y%m%d) \
  upstream/main
cd ../hermes-agent-sync-$(date +%Y%m%d)
git status --short --branch
```

Expected: the new worktree starts clean on `sync/upstream-main-<date>` and contains no fork history.

- [ ] **Step 3: Never sync directly on the long-lived fork branch**

```bash
git branch --show-current
```

Expected: the active branch is a dated sync branch created from `upstream/main`, not `main` and not the old fork branch.

### Task 3: Replay The Config And Provider Identity Layer First

**Files:**
- Modify: `hermes_cli/config.py`
- Modify: `hermes_cli/providers.py`
- Modify: `hermes_cli/models.py`
- Modify: `agent/models_dev.py`
- Modify: `agent/model_metadata.py`
- Test: `tests/hermes_cli/test_config.py`

- [ ] **Step 1: Reapply config compatibility for custom model providers**

```bash
git diff upstream/main...wip/model-integration-preservation -- \
  hermes_cli/config.py \
  hermes_cli/providers.py \
  hermes_cli/models.py \
  agent/models_dev.py \
  agent/model_metadata.py
```

Expected: you can see the exact local behavior to port without replaying unrelated files.

- [ ] **Step 2: Keep upstream structure, but preserve these local behaviors**

```text
1. `providers:` keyed config remains the primary user-defined provider schema.
2. `custom_providers` remains a read-compatible legacy view via `get_compatible_custom_providers()`.
3. Provider entries preserve `api`/`url`/`base_url`, `key_env`, `api_key`,
   `default_model`, `models`, `transport`/`api_mode`, and `context_length`.
4. `models.dev` mappings and provider aliases continue to cover the current
   local provider set used by `/model` and runtime routing.
5. `agent/model_metadata.py` continues to infer provider context for custom
   endpoints instead of degrading everything to generic OpenAI behavior.
```

Expected: provider identity and metadata remain stable before any runtime code is replayed.

- [ ] **Step 3: Verify the config and provider catalog layer**

```bash
source venv/bin/activate
python -m pytest tests/hermes_cli/test_config.py -q
```

Expected: config migration and compatibility tests pass before moving on.

- [ ] **Step 4: Commit the provider/config layer separately**

```bash
git add hermes_cli/config.py hermes_cli/providers.py hermes_cli/models.py agent/models_dev.py agent/model_metadata.py tests/hermes_cli/test_config.py
git commit -m "feat(model-config): restore provider metadata and config compatibility"
```

Expected: the sync branch now contains one isolated commit for config and provider identity.

### Task 4: Replay Runtime Provider Resolution And `/model` Switching

**Files:**
- Modify: `hermes_cli/runtime_provider.py`
- Modify: `hermes_cli/model_switch.py`
- Modify: `cli.py`
- Test: `tests/hermes_cli/test_user_providers_model_switch.py`

- [ ] **Step 1: Reapply named custom-provider resolution from `providers:`**

```bash
git diff upstream/main...wip/model-integration-preservation -- \
  hermes_cli/runtime_provider.py \
  hermes_cli/model_switch.py \
  cli.py \
  tests/hermes_cli/test_user_providers_model_switch.py
```

Expected: the diff shows the `providers:` dictionary path, `custom_providers` compatibility path, and `/model --provider` CLI flow.

- [ ] **Step 2: Preserve these runtime behaviors while keeping upstream refactors**

```text
1. `resolve_runtime_provider()` must accept built-in providers, named custom
   providers, and legacy custom providers through one path.
2. `_get_named_custom_provider()` must match both provider keys and display names.
3. `resolve_runtime_provider()` must propagate `model`, `api_mode`, `base_url`,
   and `api_key` for named custom endpoints.
4. `/model --provider <user-key>` must pass `providers` into `switch_model()`
   even when the user skips the interactive picker.
5. `list_authenticated_providers()` must expose full `models` lists for user
   providers, not only `default_model`.
```

Expected: the sync branch keeps the current custom-provider UX instead of regressing to built-in providers only.

- [ ] **Step 3: Verify `/model` and custom-provider behavior**

```bash
source venv/bin/activate
python -m pytest tests/hermes_cli/test_user_providers_model_switch.py -q
```

Expected: explicit user-provider switching and provider-list rendering pass.

- [ ] **Step 4: Commit the runtime provider layer separately**

```bash
git add hermes_cli/runtime_provider.py hermes_cli/model_switch.py cli.py tests/hermes_cli/test_user_providers_model_switch.py
git commit -m "feat(model-runtime): restore custom provider resolution and /model switching"
```

Expected: runtime provider resolution is isolated from later client-construction changes.

### Task 5: Replay Client Construction, Fallback, And Main-Agent Wiring

**Files:**
- Modify: `agent/auxiliary_client.py`
- Modify: `run_agent.py`
- Test: `tests/run_agent/test_fallback_model.py`
- Test: `tests/run_agent/test_provider_parity.py`

- [ ] **Step 1: Reapply the unified client-router behavior**

```bash
git diff upstream/main...wip/model-integration-preservation -- \
  agent/auxiliary_client.py \
  run_agent.py \
  tests/run_agent/test_fallback_model.py \
  tests/run_agent/test_provider_parity.py
```

Expected: the diff shows how local provider routing extends the upstream router rather than bypassing it.

- [ ] **Step 2: Preserve these client-construction rules**

```text
1. `agent/auxiliary_client.py::resolve_provider_client()` remains the single
   factory for provider-aware clients and model normalization.
2. Main-agent initialization in `run_agent.py` must keep using the centralized
   router when explicit credentials are absent.
3. Fallback activation must continue to use the centralized router rather than
   rebuilding provider-specific auth logic in `run_agent.py`.
4. Runtime restore and client rebuild flows must preserve provider-specific
   headers and `api_mode`.
5. Direct OpenAI, Codex, Anthropic, Bedrock, Copilot, Kimi, Qwen, OpenRouter,
   and named custom endpoints must keep their provider-specific wire behavior.
```

Expected: sync work does not flatten provider differences or regress fallback behavior.

- [ ] **Step 3: Verify fallback and provider parity**

```bash
source venv/bin/activate
python -m pytest tests/run_agent/test_fallback_model.py -q
python -m pytest tests/run_agent/test_provider_parity.py -q
```

Expected: fallback activation and per-provider request-shape parity pass.

- [ ] **Step 4: Commit the client-routing layer separately**

```bash
git add agent/auxiliary_client.py run_agent.py tests/run_agent/test_fallback_model.py tests/run_agent/test_provider_parity.py
git commit -m "feat(model-client): restore provider-aware client routing and fallback"
```

Expected: the largest and riskiest layer is isolated in one replay commit.

### Task 6: Replay SoulStore As An Optional Additive Extension

**Files:**
- Create: `agent/soulstore_gateway.py`
- Modify: `agent/auxiliary_client.py`
- Modify: `run_agent.py`
- Modify: `hermes_cli/config.py`
- Test: `tests/run_agent/test_soulstore_headers.py`

- [ ] **Step 1: Port SoulStore support as a thin header-injection layer**

```bash
git diff upstream/main...wip/model-integration-preservation -- \
  agent/soulstore_gateway.py \
  agent/auxiliary_client.py \
  run_agent.py \
  hermes_cli/config.py \
  tests/run_agent/test_soulstore_headers.py
```

Expected: the diff is additive and clearly separable from generic provider routing.

- [ ] **Step 2: Preserve these SoulStore-specific rules**

```text
1. SoulStore behavior must remain fully env-gated.
2. Header injection must only apply when `SOULSTORE_INSTANCE_ID` is set and
   the request base URL matches `SOULSTORE_BASE_URL` or the `soulstore`
   heuristic.
3. SoulStore header logic must augment, not replace, existing provider headers.
4. Non-SoulStore requests must remain byte-for-byte unchanged.
```

Expected: SoulStore support survives syncs without contaminating other providers.

- [ ] **Step 3: Verify SoulStore isolation**

```bash
source venv/bin/activate
python -m pytest tests/run_agent/test_soulstore_headers.py -q
```

Expected: SoulStore headers are injected only when configured and never leak to unrelated endpoints.

- [ ] **Step 4: Commit SoulStore support separately**

```bash
git add agent/soulstore_gateway.py agent/auxiliary_client.py run_agent.py hermes_cli/config.py tests/run_agent/test_soulstore_headers.py
git commit -m "feat(model-gateway): restore SoulStore header injection"
```

Expected: SoulStore remains an optional top-layer patch that can be toggled or dropped independently.

### Task 7: Run The Verification Suite And Establish The Ongoing Sync Policy

**Files:**
- Modify: `docs/plans/2026-05-08-upstream-sync-model-integration-plan.md`
- Test: `tests/hermes_cli/test_config.py`
- Test: `tests/hermes_cli/test_user_providers_model_switch.py`
- Test: `tests/run_agent/test_fallback_model.py`
- Test: `tests/run_agent/test_provider_parity.py`
- Test: `tests/run_agent/test_soulstore_headers.py`

- [ ] **Step 1: Run the targeted model-integration suite**

```bash
source venv/bin/activate
python -m pytest tests/hermes_cli/test_config.py -q
python -m pytest tests/hermes_cli/test_user_providers_model_switch.py -q
python -m pytest tests/run_agent/test_fallback_model.py -q
python -m pytest tests/run_agent/test_provider_parity.py -q
python -m pytest tests/run_agent/test_soulstore_headers.py -q
```

Expected: all targeted model-integration tests pass on the sync branch.

- [ ] **Step 2: Run the full repo suite before merging the sync branch**

```bash
source venv/bin/activate
python -m pytest tests/ -q
```

Expected: the full suite passes, proving the replayed model layer did not silently break unrelated subsystems.

- [ ] **Step 3: Record the exact upstream SHA and preserved commit SHAs**

```bash
git show -s --format='upstream=%H %ci %s' upstream/main
git log --oneline --max-count=10
```

Expected: the sync branch history makes it obvious which upstream commit was used and which local preservation commits were replayed.

- [ ] **Step 4: Keep the future sync policy unchanged**

```text
1. Never sync on a dirty worktree.
2. Never merge the old fork branch wholesale into new upstream.
3. Always create a fresh worktree from the latest `upstream/main`.
4. Always replay the model layer in the same order:
   config/providers -> runtime_provider and /model -> client routing -> SoulStore.
5. Always keep those layers in separate commits with stable prefixes:
   `feat(model-config)`, `feat(model-runtime)`, `feat(model-client)`, `feat(model-gateway)`.
6. Always run the targeted model suite before the full suite.
7. Always update this plan with new hotspots if upstream changes the same files again.
```

Expected: future upgrades reuse the same workflow instead of rediscovering conflict policy each time.

## Conflict Policy

- If a file has large upstream churn and small local behavior changes, keep the upstream file shape and reapply only the local behavior.
- Never replay merge commits from the old fork branch; only replay behavior.
- For `run_agent.py` and `agent/auxiliary_client.py`, prefer manual re-implementation over whole-file checkout.
- If a local behavior is fully absorbed upstream, drop the local patch and update the verification tests instead of preserving dead code.
- If a new local model feature is added in the future, it must land in one of the four stable patch layers above so the next upstream sync stays repeatable.
