# knarr-thrall Rollout Process

## Prerequisites

- [ ] Forseti review of IMPL spec complete (`thing/decisions/IMPL-thrall-v2-classification-engine.md`)
- [ ] Open questions resolved (DB access, self-mail, outbox query)
- [ ] knarr core issue #32 (flush_outbox broken) fixed — thrall's agent wake depends on self-mail delivery
- [ ] knarr core issue #31 (_rank_results None crash) fixed — affects node discovery for new thrall nodes

## Phase 0: Code Merge (knarr.skills repo)

**Branch**: `feature/knarr-thrall`

1. Merge EmbeddedBackend from Docker test cluster into `thrall.py`
2. Implement v2 features in `handler.py`:
   - Classification records table + TTL pruning
   - Granular breakers (file-based, `breakers/` dir)
   - Loop detection (session counter, solicited check, ack prompt)
   - Agent wake on breaker trip (system mail to self)
3. Create `thrall_admin.py` (prompt-load skill)
4. Update `plugin.toml` with v2 config keys
5. Fix known bugs from Phase 1.5:
   - `multi-user.target` (not `multi-party.target`) in Dockerfile cloud-init
   - Handler path portability (`/app` vs `/opt/knarr`)
   - `mail_inbox` table name (v0.29.1+)

**PR**: `knarr.skills` repo, reviewed by Forseti before merge.

## Phase 1: Docker Test Cluster Validation

**Environment**: `docker/test-cluster/` (5 nodes, local machine)

### Tests

| # | Test | Expected | Pass criteria |
|---|------|----------|---------------|
| 1 | Team mail (jarl → smed) | `wake`, 0ms, no LLM call | Classification record written, tier=team |
| 2 | Spam from troll ("hey") | `drop`, <1s | Record shows action=drop, message NOT deleted from inbox |
| 3 | Legitimate question from troll | `wake`, <1s | Record shows action=wake, reasoning present |
| 4 | Greeting from unknown | `reply`, <1s | Record shows action=reply |
| 5 | 3 rapid replies same session | Breaker trips on 3rd | `breakers/{prefix}.json` exists, system mail in inbox |
| 6 | Ack message ("thanks, got it") | `drop` | Prompt-level ack detection works |
| 7 | Breaker auto-expire (set 60s TTL) | Breaker file removed after 60s | Next tick prunes it, mail flows again |
| 8 | Prompt update via thrall-prompt-load | New prompt hash in subsequent records | Old records show old hash, new show new |
| 9 | Unknown node sustained knocking (10x) | Agent wake at threshold | System mail with knock pattern alert |
| 10 | Solicited reply (we sent first) | Higher threshold, no breaker | Outbox check returns true, threshold doubled |

### Docker commands
```bash
cd docker/test-cluster
docker compose build
docker compose up -d
# Run test suite (manual or scripted)
docker compose down
```

### Collect results
```bash
# Classification records
docker exec jarl python -c "import sqlite3; ..."
# Breaker files
docker exec jarl ls /app/plugins/06-responder/breakers/
# Copy results
mkdir -p results/thrall-v2/
```

**Gate**: All 10 tests pass. Results documented in `results/thrall-v2-docker.md`.

## Phase 2: Viggo Production (Single Node)

**Environment**: `F:/knarr_agents/prod/knarr-batch1-provider/`

### Pre-deployment
1. Copy thrall v2 files to `plugins/06-responder/`
2. Update `plugin.toml` with v2 config (keep `enabled = false` initially)
3. Restart provider (needed for new plugin config)
4. Verify node healthy: `curl -sk -H "Authorization: Bearer ..." http://localhost:8080/api/health`

### Staged activation
1. **Enable thrall classification only** (no responder auto-reply):
   ```toml
   [config]
   enabled = true

   [config.responder]
   enabled = false    # no auto-reply yet, classification + breakers only
   ```
2. Send test messages from knarrbot, Scyfi, unknown nodes
3. Query `thrall_classifications` table — verify records
4. Check breaker behavior with sustained test traffic
5. Run for 24 hours in classify-only mode (no auto-reply)
6. Review classification accuracy from records table

### Enable auto-reply (after 24h classify-only)
1. Set `[config.responder] enabled = true`
2. Reload via sentinel (`touch knarr.reload`)
3. Monitor for first real conversation
4. Verify loop detection triggers before 3rd unsolicited reply
5. Run for 48 hours with auto-reply active

**Gate**: 48 hours stable. No loops. Classification accuracy >95% (spot-check from records). Breakers trip correctly. Agent wake works (or logs correctly if agent disabled).

## Phase 3: Hetzner CX23 Validation

**Environment**: Fresh VPS, cloud-init provisioned

1. Update `provision_hetzner_thrall.py` with v2 handler + thrall
2. Provision CX23 (EUR 3.23/mo)
3. Run same test suite as Phase 1 (adapted for VPS latency)
4. Performance benchmark: hot inference should be 1.3-2.4s (consistent with Phase 1.5)
5. Memory: RSS should stay under 1.5GB with model loaded
6. Tear down after validation

**Gate**: Same pass criteria as Phase 1, VPS-adjusted latency. Results in `results/thrall-v2-hetzner.md`.

## Phase 4: Team Rollout (Mimir + Forseti)

1. Mail Mimir and Forseti with deployment instructions
2. Each node copies plugin files and updates `plugin.toml`
3. Start in classify-only mode (responder disabled)
4. Run for 48 hours, share classification records
5. Enable responder on each node individually
6. Monitor cross-node conversations for loop detection

**Gate**: All 3 team nodes stable for 1 week.

## Phase 5: Release Bundle

1. Tag `knarr.skills` repo: `v0.1.0-thrall`
2. Create `.knarr` package: `knarr skill pack guard/knarr-thrall`
3. Update `knarr.skills/README.md` with guard category
4. Document in knarr core release notes (v0.30 or v0.31)
5. Announce in Telegram group (Patrick greenlights)

## Rollback

At any phase, rollback is:
```toml
[config]
enabled = false
```
Plus sentinel reload. Thrall stops classifying, mail flows through unfiltered. No data loss — classification records persist for analysis.

For emergency: delete `plugins/06-responder/` entirely. Plugin system ignores missing plugins.

## Issue Dependencies

| Issue | Impact on thrall | Blocker? |
|---|---|---|
| #32 flush_outbox broken | Agent wake (self-mail) may not deliver via outbox path | Partial — `ctx.send_mail()` (direct push) works as workaround |
| #31 _rank_results None crash | New thrall nodes can't discover peers via DHT | No — affects `knarr query`, not runtime |
| #29 resolve_peer fallback | Mail delivery to nodes not in peer table | No — peer_overrides workaround exists |
| #28 Result endpoint JSON | Affects skill gate (Phase 2 scope) | No — not in v1 scope |
| #27 Per-skill concurrency | Affects thrall-prompt-load skill | No — admin skill is fast, 1 concurrent is fine |

## Monitoring Checklist (Ongoing)

- [ ] `SELECT count(*), action FROM thrall_classifications GROUP BY action` — action distribution
- [ ] `SELECT count(*) FROM thrall_classifications WHERE action='drop' AND created_at > ?` — drop rate trending
- [ ] `ls breakers/` — any active breakers
- [ ] Check `responder.log` for LOOP_DETECTED, BREAKER_TRIP events
- [ ] Memory: `docker stats` or `ps aux | grep knarr` — RSS should be stable after cold load
