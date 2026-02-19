# Telegram Notifications Setup

Shop Manager, Monitor, and Security send alerts to your Telegram via a **Zapier webhook**.

## 1. Create the Zapier Webhook Zap

1. Go to [Zapier](https://zapier.com) → **Create Zap**
2. **Trigger:** Search for **Webhooks by Zapier** → **Catch Hook**
3. Zapier will show a webhook URL (e.g. `https://hooks.zapier.com/hooks/catch/xxxxx/yyyyy/`)
4. **Action:** Search for **Telegram** → **Send Message**
5. Connect your Telegram bot (the one you use with BotFather)
6. Map fields:
   - **Chat ID:** your numeric chat ID (e.g. `6488555026`)
   - **Message Text:** from the webhook → choose `message` (or the field containing the alert text)
7. **Test** the action, then **Publish**

## 2. Store the Webhook URL

Save the Zapier webhook URL to a local config file:

```bash
echo "https://hooks.zapier.com/hooks/catch/YOUR_ID/YOUR_KEY/" > ~/.config/cursor-zapier-telegram-webhook
chmod 600 ~/.config/cursor-zapier-telegram-webhook
```

Replace with your actual URL from step 3.

## 3. Test

```bash
cd ~/howell-forge-agent
python3 -c "from notifications import send_telegram_alert; send_telegram_alert('Howell Forge test — notifications working!')"
```

Check Telegram for the message.

## 4. What Triggers Alerts

| Agent   | When                           | Severity  |
|---------|--------------------------------|-----------|
| Monitor | Site down, 500s, connection errors, Stripe/SSL failures | EMERGENCY or HIGH |
| Security| HTTPS redirect or security headers fail | HIGH      |
| Marketing | SEO check fails (title, meta description) | HIGH    |
| Shop Manager | `notify_user()` called from code | (any)     |

---

**Note:** If the webhook config file is missing, agents still run and write to the log file; they just skip Telegram (no errors).
