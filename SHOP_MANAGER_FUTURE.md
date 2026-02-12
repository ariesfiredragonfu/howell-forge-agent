# Shop/Factory Manager — Future Structure

Research and structure for when the Shop Manager agent is built out.

---

## Order Flow (Target)

```
Stripe payment (checkout.session.completed)
        ↓
Customer Data stores order
        ↓
Shop Manager receives → adds to shop queue
        ↓
Shop: Released → In Progress (fabrication) → Complete
        ↓
Ship → Deliver
        ↓
Monitor verifies each stage
```

---

## Production Stages (Job Shop)

| Stage | Description |
|-------|-------------|
| **Quote/Order** | Customer order received, entered |
| **Released** | Job released to shop floor |
| **In Progress** | Active fabrication (queue, setup, run, transport) |
| **Complete** | All assemblies done |
| **Shipped** | Sent to customer |
| **Delivered** | Customer received |

---

## Messaging (Future)

**To you (owner):**
- Notify when new order needs shop handoff
- Alert when order is stuck or delayed
- Daily/weekly summaries
- Channel: Telegram (Zapier webhook), email, or both

**To factory / shop floor:**
- Eventually: factory manager person, robots, machines
- New job arrives → message shop
- Stage updates → notify relevant party
- Format/channel TBD (dashboard, SMS, in-shop display, M2M API)

---

## Integration Options (Future)

### Stripe → Shop

- **Webhook:** `checkout.session.completed` (primary) — reliable, runs server-side
- **Redirect:** Customer redirect after payment — immediate, but can miss if browser closed
- **Best practice:** Use webhooks; idempotent fulfill function

### Shop System (TBD)

Options for small metal fab:

| Tool | Best for | Notes |
|------|----------|-------|
| **Spreadsheet** | Simplest start | Manual, error-prone |
| **Airtable** | Lightweight, flexible | Good for custom workflows |
| **Flowlens** | SME manufacturers | Order→stock, job cards, Xero/QuickBooks |
| **Craftybase** | Inventory + workflow | Material tracking, COGS |
| **ProcessMate** | Custom manufacturing | Order templates, RFI, milestones |
| **Full ERP** | Larger operations | Job travelers, routing, WIP |

---

## What Shop Manager Will Do (Future)

1. **Receive:** Get new orders from Stripe (webhook or poll)
2. **Queue:** Add to shop system (format TBD)
3. **Track:** Know stage of each order (received, in progress, complete, shipped)
4. **Report:** Feed status to Monitor for pipeline health
5. **Alert:** Flag orders stuck or delayed
6. **Message:** Notify you; eventually notify factory manager, robots, machines

---

## Dependencies

- Stripe key (already used by Customer Data)
- Shop system/API (to be chosen)
- Messaging: Telegram/Zapier (for you), TBD for factory
- Possibly: shipping API (tracking), email (notifications)

---

## Links

- [Stripe: Fulfill orders](https://docs.stripe.com/checkout/fulfillment)
- [Stripe: After the payment](https://docs.stripe.com/payments/checkout/after-the-payment)
- Metal fab workflow: quote → release → in progress → complete → ship
- Small manufacturer tools: Flowlens, Craftybase, ProcessMate, Wipfusion
