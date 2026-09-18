# GSC Wizard מול הסקילים — מפת חפיפות

**מצב המסמך:** העמודה שלנו מלאה ומאומתת מהקוד. העמודה של GSC Wizard **ממתינה
ל-`tools/list` מהשרת** — כל מה שכתוב בה עכשיו מסומן `טרם אומת` ומקורו אתר
המוצר בלבד. שום כלי לא מסומן "לא קיים" רק כי לא הגענו אליו.

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

**6 חסומים · 2 מנוונים · 3 לא תלויים.** ושום רכיב בריפו או במחשב לא מייצר את
הקובץ שהששה דורשים: אין קריאה ל-GSC Wizard בקוד, `GSC_WIZARD_API_KEY` מוכרז
ולא נצרך, ו-`gsc-crawl-checker` המותקן מחזיר סטטוס אינדוקס בלבד דרך Playwright.

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
| striking distance | `seo_core/sources/queries.py:find_near_misses()` | מיקום 3.5–20, תקרה 3 מקומות, לא מבטיח מיקום 1 | "striking distance" | **דורש אימות** — האם יש אומדן קליקים עם בסיס? |
| קניבליזציה | `seo_core/sources/queries.py:find_cannibalisation()` | שני דפים על שאילתה; המנצח לפי קליקים; 70% השבה | "cannibalization" | **דורש אימות** |
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

## 4. טבלת החלטות — ממולאת אחרי שלב C

| רכיב | הכרעה | סיבה | השפעה על תחזוקה |
|---|---|---|---|
| *(ממולא אחרי `docs/gsc-wizard-test/RESULTS.md`)* | | | |

---

## 5. מה חסם, ומה נדרש

| | |
|---|---|
| `gscwizard.com`, `mcp.gscwizard.com`, `glama.ai`, `mcpservers.org` | ה-proxy של הסביבה מחזיר **403 ב-CONNECT** |
| רג'יסטרי הקונקטורים | GSC Wizard לא מופיע |
| קונקטור בסשן | לא מותקן (יש Figma, Google Drive, Semrush) |
| **מה פותח:** custom connector ב-claude.ai עם `https://mcp.gscwizard.com/` | עובר דרך `mcp-proxy.anthropic.com`, שברשימת המותרים |

תקצירי חיפוש **לא** שימשו למילוי העמודה: הם ערבבו GSC Wizard עם Suganthan's
GSC MCP ועם FlorianBruniaux/google-search-console-mcp — שלושה שרתים שונים.
