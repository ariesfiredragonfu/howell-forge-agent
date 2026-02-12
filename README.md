# Howell Forge Agent

Layer 1 testing sandbox for the Howell Forge agent crew.

**Nothing in this repo touches Deploy Now or the live site** until we're ready.

## Architecture

See `~/project_docs/howell-forge-website-log.md` for the full plan.

### Build order

1. **Monitor** (foundation) — site health, order pipeline, Stripe
2. **Customer Data** — customer/order knowledge
3. **Customer Service** — FAQs, inquiries, triage
4. **Marketing** — SEO, traffic, outreach
5. **Security** — protection, vuln checks

### Layers

- **Layer 1:** You + AI + GitHub (testing only)
- **Layer 2:** Deploy Now + website
- **Layer 3:** Healers (Monitor, Security, AI)
- **Layer 4+:** Rest of crew

### Self-healing loop

Monitor and Security report to the log file. Cursor reads, fixes, pushes to GitHub, redeploys.

---

## Monitor Agent

**Usage:**
```bash
python3 monitor.py
```

- Checks `https://howell-forge.com` (/, /about, /contact)
- **Uptime:** alerts on HTTP errors, connection failures
- **Latency:** alerts if any page takes > 5s to respond
- **Stripe API:** calls balance endpoint; requires `~/.config/cursor-stripe-secret-key` (skips if missing)
- On failure: appends to `~/project_docs/howell-forge-website-log.md` with EMERGENCY or HIGH
- Exit 0 = OK, exit 1 = alert written