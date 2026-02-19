# Marketing Agent — Growth Plan

Phased plan for expanding the Marketing agent. Pick a phase and we can implement it.

---

## Phase 1: SEO Depth (low effort, no new APIs)

**Goal:** Expand SEO checks without external APIs. All can be done by fetching the live site HTML.

| Check | Effort | Value |
|-------|--------|-------|
| Open Graph (og:title, og:description, og:image) | Low | High — better shares on social |
| Twitter Card meta | Low | Medium — Twitter/X link previews |
| H1 structure (at least one H1 present) | Low | Medium — accessibility + SEO |
| Canonical URL | Low | Low — avoid duplicate content |
| Check `/about` and `/contact` pages too (if they exist as separate URLs) | Medium | Medium |

**Deliverable:** `marketing.py` gains `check_og()`, `check_twitter_card()`, `check_h1()`, etc. CLI could support `python3 marketing.py seo` (default) or `python3 marketing.py seo --pages home,about,contact`.

---

## Phase 2: Sitemap & robots.txt

**Goal:** Verify the site is set up for search engines to crawl.

| Check | Effort | Value |
|-------|--------|-------|
| `robots.txt` exists and allows crawling | Low | High |
| `sitemap.xml` exists and is valid | Medium | High |
| sitemap URLs are reachable | Medium | Medium |

**Note:** The site is currently a single-page app (SPA). Sitemap might list `/`, `/about`, `/contact` if they're separate routes, or just `/` if it's all client-side routing. We'd need to confirm the site structure.

---

## Phase 3: Traffic & Analytics (needs setup)

**Goal:** Surface traffic data so you can see what's working.

| Source | Setup required | Effort |
|--------|----------------|--------|
| **Google Analytics 4** | GA4 property, Measurement ID, API or export | Medium–High |
| **Search Console** | GSC property, OAuth or service account | Medium |
| **Simple UTM check** | None — verify links on site use UTM params where appropriate | Low |

**Recommendation:** Start with Phase 1 and 2. Add analytics when you have GA4/GSC set up and want to automate reporting.

---

## Phase 4: Outreach (content, social, email)

**Goal:** Help create and distribute content.

| Area | Options | Notes |
|------|---------|-------|
| **Content** | Blog/updates, product announcements | Needs CMS or static pages |
| **Social** | Twitter/X, LinkedIn, etc. | Needs API keys, OAuth |
| **Email** | Mailchimp, etc. | Needs API key |
| **Ads** | Google Ads, Meta | Complex, usually later |

**Recommendation:** Defer until you have a clear content strategy or specific channels you want to automate.

---

## Suggested Next Step

**Phase 1 — SEO Depth** is the best next move:

1. No new APIs or credentials
2. Uses existing fetch logic
3. Improves social sharing and search visibility
4. Clear, testable checks

Concrete Phase 1 tasks:

1. Add Open Graph checks (og:title, og:description, og:image)
2. Add Twitter Card checks (twitter:card, twitter:title, twitter:description)
3. Add H1 structure check
4. Optionally extend to check multiple pages if the site has distinct URLs

---

## CLI Structure (proposed)

```bash
# Current
python3 marketing.py                    # runs SEO check (default)

# Phase 1+
python3 marketing.py seo               # full SEO check (title, meta, OG, Twitter, H1)
python3 marketing.py seo --quick       # title + meta only (current behavior)
python3 marketing.py sitemap           # Phase 2
python3 marketing.py traffic           # Phase 3 (when GA/GSC connected)
```

---

When you're ready, we can start with Phase 1 and add Open Graph + Twitter Card checks first.
