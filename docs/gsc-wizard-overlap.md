# GSC Wizard מול הסקילים — מפת חפיפות

**מצב המסמך (2026-09-19):** העמודה שלנו מלאה ומאומתת מהקוד. בעמודה של GSC Wizard,
השורות של קניבליזציה, קרובות-לעמוד-ראשון, עקומת CTR, דעיכה ועדכוני אלגוריתם
מולאו מהשרת עצמו ומהרצה אמיתית על `sass-srq.com` — הפירוט ב-
`docs/gsc-wizard-test/RESULTS.md` וב-`docs/gsc-wizard-tools.md`. מה שלא נבדק נשאר
`טרם אומת`. שום כלי לא מסומן "לא קיים" רק כי לא הגענו אליו.

## סרגל ראיות

| סימון | משמעות |
|---|---|
| `מאומת בקוד` | `file.py:func()` שקיים בריפו — `tests/test_docs_map.py` מוודא כל אחד |
| `נבדק בהרצה` | קראנו לכלי של GSC Wizard וראינו פלט |
| `מאומת בתיעוד` | שם + פרמטרים מ-`tools/list` או מהתיעוד הרשמי, בלי הרצה |
| `טרם אומת` | טענה מאתר המוצר. לא מוכיחה קיום, לא מוכיחה חסר |

---

## 1. מה תלוי ב-GSC היום — מאומת בהרצה, לא מזיכרון

כל סקריפט הורץ עם `--client` בלבד; נרשם הדבר הראשון שהוא דורש.

| סקיל | הדרישה הראשונה | מה זה אומר |
|---|---|---|
| `content_decay` | `--gsc-data` | **חסום** בלי ייצוא שני-חלונות |
| `onpage_optimizer` | `--queries` | **חסום** |
| `ctr_titles` | `--queries` | **חסום** |
| `topic_cluster` | `--queries` (או `--show`) | **חסום** לבנייה |
| `algorithm_update_watch` | `--before` + `--after` | **חסום** |
| `internal_anchors` | `--queries` | **חסום** |
| `click_depth` | מזהיר, ממשיך לסרוק | מנוון — בלי עומק/חסימות |
| `seo_audit_report` | רץ, `rc=0` | מנוון — תקרת CTR מהתעשייה |
| `conversion_tracking_audit` | האתר עצמו | לא GSC |
| `speed_optimizer` | `--url` | לא GSC |
| `wp_migration` | `--target` | לא GSC |

**6 חסומים · 2 מנוונים · 3 לא תלויים** — זה היה המצב עד 2026-09-19.

**מה השתנה:** `seo_core/sources/gsc_wizard.py:export_queries()` מושך את השורות מ-GSC
Wizard וכותב אותן בפורמט ש-`seo_core/sources/queries.py:load_export()` קורא.
`onpage_optimizer` רץ עכשיו עם `--client` בלבד `נבדק בהרצה`. חמשת האחרים עדיין
דורשים קובץ, עד שהמושך יורחב אליהם (מחזור ב).

---

## 2. השוואה רכיב-רכיב

לכל רכיב חמש שאלות: מה GSC Wizard כבר עושה · מה הפייתון מוסיף · האלגוריתם
שונה או מימוש מחדש · גולמי / ממצא / המלצה · מה אי אפשר לשחזר מהפלט שלו.

הכרעה מארבע: **לשמר · לצמצם · להחליף · דורש אימות.** כל הכרעה כאן זמנית עד
תוצאות ההרצה (`docs/gsc-wizard-test/RESULTS.md`).

### 2.1 זיהוי — מה שצפוי לחפוף

| רכיב | אצלנו `מאומת בקוד` | מה הרכיב עושה | GSC Wizard `טרם אומת` | הכרעה זמנית |
|---|---|---|---|---|
| סיווג דעיכה | `seo_core/sources/gsc_source.py:classify()` | שש סיבות; `seasonality` = השפעה 0; `serp_takeover` דורש שם מתחרה | "content decay" | **דורש אימות** — האם הוא מסווג *סיבה*, או רק מסמן ירידה? |
| striking distance | `seo_core/sources/queries.py:find_near_misses()` | מיקום 3.5–20, תקרה 3 מקומות, לא מבטיח מיקום 1 | `score_opportunities` `נבדק בהרצה` — רמת שאילתה בלבד, בלי דף; `potentialClicks` הסתכם ב-1,082 באתר עם 99 קליקים | **לשמר** — מוצע; האומדן שלו לא ראוי להצגה ואין בו דף |
| קניבליזציה | `seo_core/sources/queries.py:find_cannibalisation()` | שני דפים על שאילתה; המנצח לפי קליקים; 70% השבה | `analyze_cannibalization` `נבדק בהרצה` — כל הדפים לשאילתה + ציון אנטרופיה; בלי מנצח, בלי אומדן, בלי פעולה | **לשמר ולתקן** — מוצע; המסנן שלנו יושב על הציר הלא נכון: 19 מ-22 ממצאים לא אמיתיים, ושניים מציעים 301 מדף הבית |
| פער כיסוי | `seo_core/sources/queries.py:find_coverage_gaps()` | מילות השאילתה חסרות **בטקסט הדף** — דורש סריקה | לא צפוי; דורש תוכן הדף | **לשמר** בינתיים — נבדק אם יש לו גישה לתוכן |
| עקומת CTR | `seo_core/sources/queries.py:build_curve()` | חציון לכל מיקום, ≥8 שאילתות לדלי, מונוטונית; נופלת לממוצע תעשייה ומורידה ביטחון | "CTR curve / benchmarking" | **דורש אימות** — חציון או ממוצע? מהאתר או מהתעשייה? |
| דף מתחת לצפי | `seo_core/text/snippets.py:audit_page()` | רק עמוד ראשון; סיבה מבנית קודם; 60% / 25% | "CTR מול מיקום" | **דורש אימות** — האם הוא אומר *למה*? |
| קיבוץ נושאים | `seo_core/sources/clusters.py:build()` | לפי ה-URL שגוגל בחר, לא לפי מילים | "topic clusters / content groups" | **דורש אימות** — האם הקיבוץ שלו לפי URL, מילים, או ידני? |
| מפה שמתמזגת | `seo_core/sources/clusters.py:merge()` | זהות לפי חפיפת שאילתות; `first_seen`; נושא שנעלם נשמר | ? | **דורש אימות** — האם יש לו זיכרון בין הרצות? |
| תנודה רגילה של האתר | `seo_core/sources/updates.py:spread_of()` | טווח בין-רבעוני מפיזור הדפים | "traffic drops / anomalies" | **דורש אימות** — סף קבוע או מהאתר? |
| אתר מול לוח שנה | `seo_core/sources/updates.py:diagnose()` | site_wide בלבד מוצע לעדכון; דף בודד = בעיית דף | ? | **דורש אימות** |
| יתומים | `seo_core/sources/linkgraph.py:orphans()` | סריקה מול רשימת כתובות מ-GSC/sitemap | "sitemaps: audit coverage against GSC" | **דורש אימות** — אולי חופף חלקית |

### 2.2 מה שצורך דבר שאינו GSC — לא צפוי לחפוף

| רכיב | אצלנו `מאומת בקוד` | מה הוא צורך | הכרעה זמנית |
|---|---|---|---|
| רוחב פיקסלים של טייטל | `seo_core/text/pixels.py:measure()` | מטריקות Arial, לא נתונים | **לשמר** |
| תבנית טייטל נלמדת | `seo_core/text/pixels.py:infer_affixes()` | ה-HTML החי מול ה-meta | **לשמר** |
| שער דירוג לטייטל | `seo_core/text/snippets.py:validate()` | מילים שמביאות קליקים + הטייטל הנוכחי | **לשמר** |
| ייחוס מנוטרל-מיקום | `seo_core/text/snippets.py:attribute()` | לפני/אחרי של *השינוי שלנו* | **לשמר** — נבדק אם יש לו "לפני/אחרי לדף" |
| הצלבה מול יומן השינויים | `seo_core/change_guard/ledger.py:applied_between()` | הנתון אצלנו בלבד | **לשמר** |
| קיבוץ תביעות בין סקילים | `seo_core/report.py:pool()` | כל הממצאים, מכל מקור | **לשמר** — נעשה נחוץ *יותר* אם GSC Wizard פולט ממצאים משלו |
| איחוד ממצאים כפולים | `seo_core/schema.py:dedupe()` | — | **לשמר** |

### 2.3 השאלות שהשרת חייב לענות עליהן (שלב A)

| # | שאלה | למה זה קובע |
|---|---|---|
| 1 | יש כלי שמחזיר שורות גולמיות `query, url, clicks, impressions, position` לחלון? | בלי זה אין מתאם דק — נצרוך ממצאים גמורים, וזה שינוי יסודי |
| 2 | כמה היסטוריה? 16 חודשים כגוגל, או יותר? | תרחישים רב-שנתיים; `content-decay` לא צריך יותר מ-13 |
| 3 | יש כלי GA4 — סשנים והמרות לדף נחיתה? | `impact_conversions` מפסיק להיות `None`; `conversion-tracking-audit` מקבל צד שני |
| 4 | יש תכונות SERP לשאילתה (AI Overview וכו')? | `ctr-titles --serp`; בלעדיו AI Overview נראה כמו בעיית טייטל |
| 5 | אפשר לבקש "28 יום" ו"אותם 28 יום אשתקד" בקריאה אחת/שתיים? | `content-decay` כולו |

**התשובות (2026-09-19):**

| # | תשובה | ראיה |
|---|---|---|
| 1 | **כן.** `query_search_analytics` מחזיר שורות גולמיות, עד 25,000 בקריאה, דפדוף ב-`startRow`. 1,418 שורות ל-`sass-srq.com` ב-28 ימים, משיכה מלאה | `נבדק בהרצה` |
| 2 | לנכס הבדיקה: אותם 28 ימים אשתקד החזירו **0 שורות**; כל הקריאות רצו מול `api` ולא מול המחסן. עומק ההיסטוריה של המחסן, והאם הוא תלוי במנוי | `נבדק בהרצה` לנכס הזה; השאר `טרם אומת` |
| 3 | יש כלי GA4, כולל חיבור Search Console + GA4 לדף נחיתה. **חסום:** Google Analytics לא מחובר בחשבון | `מאומת בתיעוד` + `נבדק בהרצה` שהוא לא מחובר |
| 4 | בממד `searchAppearance` בלבד; AI Overview וחבילה מקומית התקבלו בפועל מ-DataForSEO `serp_organic_live_advanced` | `נבדק בהרצה` (DataForSEO); הצד של GSC Wizard `מאומת בתיעוד` |
| 5 | **כן.** `find_decaying_content` מקבל חלון וחלון השוואה בקריאה אחת | `מאומת בתיעוד` |

---

## 3. הערך הייחודי — עשר יכולות, כל אחת בנפרד

לא מניחים ייחודיות ולא מוחקים בגלל שם דומה. "—" בעמודה של GSC Wizard פירושו
*ייבדק ב-`tools/list`*, לא "אין".

| יכולת | אצלנו `מאומת בקוד` | GSC Wizard | הכרעה זמנית |
|---|---|---|---|
| כתיבה ועריכה ב-WordPress | `seo_core/wp/seo_meta.py:write_payload()` | scope read-write מכסה "add sites, IndexNow, clusters" `טרם אומת` — לא אתר | **לשמר** |
| Elementor / Gutenberg | `seo_core/wp/content.py:link_phrase()` | — | **לשמר** |
| שינוי טייטל / תוכן / meta / קישורים | `seo_core/wp/content.py:retarget_link()` | האם מציע *טקסט*, או רק מסמן? | **לשמר** |
| אישורים, גיבויים, שחזור | `seo_core/wp/rehearsal.py:run()` | — | **לשמר** |
| זיהוי התנגשות בין עריכות | `seo_core/change_guard/rollback.py:on_concurrent_edit()` | — | **לשמר** |
| איחוד ממצאים ומניעת סתירות | `seo_core/report.py:pool()` | האם יש לו דירוג/איחוד? (`score_opportunities` `טרם אומת`) | **דורש אימות** |
| בדיקות אחרי פרסום | `seo_core/change_guard/checks.py:run()` | — | **לשמר** |
| מדידה אחרי 28 יום | `seo_core/change_guard/ledger.py:due_checkpoints()` | האם יש "לפני/אחרי שינוי" לדף? | **דורש אימות** |
| מהירות | `seo_core/sources/pagespeed.py:measure()` | האם יש CrUX / PSI? | **דורש אימות** |
| מיגרציה | `seo_core/wp/migration.py:size_gate()` | — | **לשמר** |
| רענון ציון Yoast | `seo_core/wp/seo_refresh.py:pending()` | — | **לשמר** |

---

## 4. טבלת החלטות

**הוחלט (ליאור, 2026-09-20): חלופה א — הקוד שלנו + GSC Wizard כמקור נתונים.** GSC Wizard
משמש למשיכת נתונים וליכולות משלימות שבהן יוכח יתרון; לוגיקת הניתוח, ההמלצות והביצוע
נשארת אצלנו.

מבוסס על `docs/gsc-wizard-test/RESULTS.md`. שום קוד לא נמחק.
רכיבים שלא נבדקו בהרצה במחזור א נשארים **דורש אימות**.

| רכיב | הכרעה מוצעת | סיבה | השפעה על תחזוקה |
|---|---|---|---|
| משיכת נתוני Search Console | **GSC Wizard כמקור** דרך `seo_core/sources/gsc_wizard.py:export_queries()` | שורות גולמיות, משיכה מלאה, עדכניות מדווחת; רץ אוטומטית עם `--client` | כ-270 שורות + תלות במפתח, במכסה ובפורמט התשובה |
| `seo_core/sources/queries.py:find_cannibalisation()` | **לשמר — תוקן 2026-09-20** (הוחלט: חלופה א) | הכלי שלהם מחזיר נתון בלי שיפוט ובלי פעולה, ולא מוסיף על השורות הגולמיות. אצלנו תוקן: "במשחק" = מיקום 25 ומעלה ונתח 15%+; דף הבית ושאילתות מותג בחוץ; אין המלצת 301. מול נתונים יומיים: 7 סומנו, 7 אמיתיים, 0 שגויים, 2 הוחמצו (קודם: 22 / 2 / 20 / 7) | כ-40 שורות + 7 בדיקות |
| `seo_core/sources/queries.py:find_near_misses()` | **לשמר** | אצלם בלי דף, ואומדן פי 11 מכל התנועה של האתר | אפס |
| `seo_core/sources/queries.py:build_curve()` | **לשמר** | העקומה מהאתר עצמו כשיש נתונים; אצלם מול ממוצעי תעשייה | אפס |
| `seo_core/text/snippets.py:audit_page()` + `seo_core/text/pixels.py:measure()` | **לשמר** | מצא סיבה מבנית אמיתית (תיאור שנחתך) שאף כלי אחר לא נתן | אפס |
| `seo_core/sources/gsc_source.py:classify()` | **דורש אימות** | ההשוואה השנתית חסומה לנכס הבדיקה — אין היסטוריה | — |
| רשימת עדכוני האלגוריתם המובנית | **להחליף** ב-`list_algo_updates` | הרשימה המובנית מעודכנת רק עד 31.8.2025; אצלם מהמקור הרשמי | פחות תחזוקה ידנית |
| `seo_core/sources/clusters.py:build()`, `seo_core/sources/updates.py:spread_of()`, `seo_core/sources/linkgraph.py:orphans()` | **דורש אימות** | לא נבדקו בהרצה במחזור א | — |
| תכונות SERP ל-`ctr-titles` | **DataForSEO** | חבילה מקומית ו-AI Overview התקבלו בקריאה אחת, ושינו את האבחנה במקרה הבוחן | עלות לקריאה; נרשמת |

---

## 5. מה חסם, ומה נדרש

**נפתר ב-2026-09-19:** העבודה עברה למחשב המקומי, שם `mcp.gscwizard.com` נגיש. GSC
Wizard מחובר כקונקטור ב-claude.ai (OAuth), ופייתון ניגש אליו ישירות עם מפתח API
לקריאה בלבד שב-`.env`. המפתח שהיה שם קודם היה מבוטל. הטבלה מטה מתעדת את מה
שחסם בסביבת הענן, לצורך ההיסטוריה.

| | |
|---|---|
| `gscwizard.com`, `mcp.gscwizard.com`, `glama.ai`, `mcpservers.org` | ה-proxy של הסביבה מחזיר **403 ב-CONNECT** |
| רג'יסטרי הקונקטורים | GSC Wizard לא מופיע |
| קונקטור בסשן | לא מותקן (יש Figma, Google Drive, Semrush) |
| **מה פותח:** custom connector ב-claude.ai עם `https://mcp.gscwizard.com/` | עובר דרך `mcp-proxy.anthropic.com`, שברשימת המותרים |

תקצירי חיפוש **לא** שימשו למילוי העמודה: הם ערבבו GSC Wizard עם Suganthan's
GSC MCP ועם FlorianBruniaux/google-search-console-mcp — שלושה שרתים שונים.
