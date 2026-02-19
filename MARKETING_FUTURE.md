# Marketing Agent — Future Structure

Research and structure for when the Marketing agent is built out. **This agent will grow significantly soon.**

---

## Scope (from plan)

| Area | Description |
|------|-------------|
| **SEO** | Title, meta, structure, indexability |
| **Traffic** | Analytics, sources, trends |
| **Outreach** | Content, social, campaigns |

---

## SEO (Layer 1+)

**Current (Layer 1):**
- Title tag presence and length
- Meta description presence and length

**Future:**
- Open Graph tags (og:title, og:description, og:image)
- Twitter Card meta
- Canonical URL
- H1 structure
- Sitemap check
- robots.txt
- Core Web Vitals (LCP, FID, CLS) — via PageSpeed or similar

---

## Traffic (Future)

- **Google Analytics** — page views, sources, bounce rate (requires GA4 property + API or export)
- **Search Console** — impressions, clicks, queries (requires GSC API)
- **Simple checks** — referrer headers, UTM tracking

---

## Outreach (Future)

- **Content** — blog posts, product updates (could integrate with website CMS)
- **Social** — posting to Twitter/X, LinkedIn, etc. (requires APIs)
- **Email** — newsletters (Mailchimp, etc.)
- **Ads** — Google Ads, Meta (requires ad APIs)

---

## Integration with Other Agents

- **Monitor:** Marketing does not replace Monitor; Monitor checks health, Marketing checks growth/visibility
- **Log file:** Marketing writes SEO/traffic alerts (HIGH) when issues found
- **Telegram:** Same Zapier webhook as Monitor/Security for alerts
