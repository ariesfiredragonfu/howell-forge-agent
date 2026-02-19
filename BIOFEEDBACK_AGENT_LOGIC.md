# Biofeedback & Scaling Logic — Agent Architecture

Design for integrating reward/constraint loops and autonomous scaling into Monitor, Security, Marketing, and optionally Customer Service.

---

## 1. The Reward System (Positive Feedback)

**Concept:** Mimic dopamine-style rewards. When a marketing action (ad copy, page layout, SEO change) hits a predefined KPI (conversion, CTR, engagement), the system "rewards" that path.

**Mechanics:**
- **KPI triggers:** Conversion rate ↑, CTR ↑, engagement ↑, bounce rate ↓, SEO score passes
- **Reward action:** Write a *reward entry* to a shared state file (e.g. `biofeedback.json` or a `REWARDS` section in the log)
- **Reinforcement:** Successful patterns are tagged (e.g. `[REWARD] ad_copy_v2 | CTR+12%`) so Cursor can see them and iterate on those styles/strategies for the next deployment
- **Repository signal:** A `biofeedback/rewards.md` or similar file that Cursor reads: "These patterns worked—double down"

**Implementation notes:**
- Marketing agent: when SEO check passes *and* (future) GA/analytics show positive trend → emit reward
- Needs a simple rewards log/storage the AI can read
- Cursor rule: "When helping with Marketing, check `biofeedback/rewards.md` for patterns to reinforce"

---

## 2. The Constraint System (Negative Feedback / Guardrails)

**Concept:** "Pain" response. When Monitor or Security detects issues, or Marketing detects off-brand/hallucinated content, that acts as a negative stimulus—force immediate pivot away from that "behavior."

**Mechanics:**
- **Monitor triggers:** Site down, 500s, SSL expiry, latency spike, Stripe API fail
- **Security triggers:** HTTPS redirect broken, missing headers, suspicious activity
- **Marketing triggers (future):** Off-brand copy, missing required meta, hallucinated/made-up claims
- **Constraint action:** Write to log (already done) + write to `biofeedback/constraints.md` or a `CONSTRAINTS` section
- **Correction:** AI (healer) reads constraints and applies fixes; constraints also feed into Scaling (see below)

**Implementation notes:**
- Monitor/Security already write to log on failure—we extend this to also append to a structured constraints list
- Marketing: add an off-brand / content-safety check (keyword blocklist, tone check, or simple heuristics)
- Cursor rule: "When healing, check `biofeedback/constraints.md`—avoid repeating these patterns"

---

## 3. Scaling Strategy (Autonomous Expansion)

**Concept:** Scale based on Reward-to-Constraint ratio. No manual "scale now" command—the agent clears itself to scale when the loop is positive.

**Mechanics:**
- **Ratio:** `score = rewards_count - (constraints_count * weight)` over a time window (e.g. last 7 days)
- **Thresholds:**
  - **High Reward / Low Constraint:** Agent cleared for *high scale* — increase deployment frequency, more Cursor-triggered repo updates, more aggressive iterations
  - **Neutral:** Maintain current cadence
  - **High Constraint / Low Reward:** *Throttle* — reduce deployment frequency, pause new experiments until constraints are resolved
- **State file:** `biofeedback/scale_state.json` — `{ "mode": "high" | "normal" | "throttle", "score": N, "last_updated": "..." }`
- **Who reads it:** Cron/scheduler, or a lightweight "orchestrator" script that decides how often to run agents and whether to prompt Cursor for updates

**Implementation notes:**
- Start simple: a `scale_state.json` that Marketing (or a small `scaler.py`) updates based on recent log + rewards
- Orchestrator: `python3 scaler.py` reads state, returns exit code or prints mode for cron to use
- Future: integrate with n8n or GitHub Actions to vary deployment frequency

---

## Integration Summary

| Component | Reward System | Constraint System | Scaling |
|-----------|---------------|-------------------|---------|
| **Monitor** | — | ✓ (already logs failures) | Feeds constraints |
| **Security** | — | ✓ (already logs failures) | Feeds constraints |
| **Marketing** | ✓ (when KPIs hit) | ✓ (off-brand / SEO fail) | Primary driver (rewards) |
| **Customer Service** | ✓ (satisfaction ↑) | ✓ (complaints, refunds) | Optional |

---

## File Structure (Implemented)

```
~/project_docs/biofeedback/
  rewards.md       # Human + AI readable: "These worked"
  constraints.md   # Human + AI readable: "Avoid these"
  scale_state.json # Machine readable: { mode, score, window }

howell-forge-agent/
  biofeedback.py   # append_reward(), append_constraint()
  scaler.py        # Computes ratio, writes scale_state.json
  run_agents.py    # Orchestrator: runs scaler + agents; throttle = Monitor+Security only
  run_agents.sh    # Wrapper for cron
```

---

## Implementation Status (2026-02-13)

- **Phase 1:** ✅ Done. `project_docs/biofeedback/` created on first use. Monitor/Security append constraints on failure. Marketing appends rewards on SEO pass, constraints on fail.
- **Phase 2:** ✅ Done. `scaler.py` computes ratio over 7-day window, writes `scale_state.json`. Thresholds: score≥3→high, score≤-2→throttle.
- **Phase 3:** ✅ Done. Marketing `check_off_brand()` with blocklist; extend via `~/project_docs/howell-forge-off-brand-blocklist.txt`.
- **Phase 4:** ✅ Done. `run_agents.py` runs scaler, then agents. In throttle mode, runs Monitor+Security only. Use `./run_agents.sh` or cron.

---

*Added 2026-02-13. User vision: biofeedback loops so agents learn and scale autonomously.*
