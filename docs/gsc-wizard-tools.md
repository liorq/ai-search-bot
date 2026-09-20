# GSC Wizard — מפת כלי ה-MCP

**מצב המסמך:** השרת חושף 109 כלים. 37 מפורטים כאן כי הם נוגעים לסקיל קיים או מתוכנן;
72 הנותרים באינדקס קצר בסוף. הסכמות נקראו מהשרת עצמו ב-2026-09-19. שישה כלים גם הורצו:
נכס `sc-domain:sass-srq.com`, חלון 2026-08-20..2026-09-16. כלי או יכולת שלא ראינו אינם
"לא קיימים" — הם `טרם אומת`.

## סרגל ראיות

| סימון | משמעות |
|---|---|
| `נבדק בהרצה` | קראנו לכלי וראינו פלט (2026-09-19, הנכס והחלון שלמעלה) |
| `מאומת בתיעוד` | שם ופרמטרים נקראו מסכמת הכלי שהשרת מפרסם; לא הורץ. תיאור הפלט בשורות כאלה מקורו בתיאור הכלי בלבד |
| `טרם אומת` | טענה שלא נבדקה. לא מוכיחה קיום, לא מוכיחה חסר |

## 1. תעבורה ואימות — `נבדק בהרצה`

- נקודת קצה: `https://mcp.gscwizard.com/mcp`. כותרת `Authorization: Bearer <key>`. JSON-RPC: קודם `initialize`, אחר כך `tools/call`.
- השרת מחזיר כותרת `Mcp-Session-Id`; חובה להחזיר אותה בכל קריאה הבאה.
- התשובה היא `text/event-stream` **בלי charset**. מפענחים את הבייטים כ-UTF-8 ומפצלים על `"\n"` בלבד: `resp.text` + `splitlines()` בפייתון נשבר על U+2028 שהופיע בתוך מחרוזת שאילתה.
- מפתח שבוטל: HTTP 401 עם `{"error":"unauthorized","message":"MCP API key has been revoked"}`.
- מכסה לחשבון: 60 קריאות בדקה, 1,000 בשעה; חריגה = HTTP 429 + `Retry-After`. המקור: עמוד ה-API keys באפליקציה; את ה-429 עצמו לא עוררנו.
- למפתח יש scope: read או read&write. הפייתון שלנו עובד עם מפתח read. מה חוזר כשמפתח read קורא לכלי כתיבה — `טרם אומת`.
- מפתח או טוקן לא נכתבים בשום קובץ בריפו, כולל המסמך הזה.

## 2. כללים שחלים על כל הכלים

- **בסיס** (קיצור בטבלאות) = `siteUrl` חובה, בדיוק כפי ש-`list_sites` מחזיר + `startDate`/`endDate` + `searchType` (ברירת מחדל `web`). בלי תאריכים: 28 הימים הסגורים האחרונים; הנתונים מפגרים 2–3 ימים. אסור להעביר `null`. `מאומת בתיעוד`
- **השוואה** = `comparisonStartDate` + `comparisonEndDate`, שניהם או אף אחד. ב-`find_decaying_content`, `get_ranking_changes`, `get_ga4_overview` השמטה = התקופה הקודמת באותו אורך. בשאר הכלים השמטה = בלי השוואה, ושדות `prev*` חוזרים 0 עם `hasComparison:false` — 0 כזה אינו ירידה. `מאומת בתיעוד`
- השדה `ctr` חוזר **באחוזים**: 22 = 22%; סך הנכס 0.59 = 0.59%. `docs/data-contract.md` דורש שבר, ולכן המתאם מחלק ב-100. `נבדק בהרצה`
- אזור הזמן: `dataMaturity.dateBasis` הוא `America/Los_Angeles`, לא UTC. `נבדק בהרצה`
- השדה `dataSource` היה `"api"` בכל קריאה. מחסן ה-ClickHouse ("warehouse"; לפי הסכמות היסטוריה ארוכה יותר ובלי דגימה) לא שימש אף פעם; בחשבון `subscription: null`. האם מנוי מפעיל אותו — `טרם אומת`. `נבדק בהרצה`
- **תשובה ריקה אינה ממצא שלילי.** הקריאה הראשונה ל-`analyze_cannibalization` עם `minImpressions=30` החזירה 0 פריטים; שלוש קריאות זהות אחריה החזירו 59. לפני שכותבים "אין ממצאים" — מריצים שוב. `נבדק בהרצה`
- **שנה-מול-שנה חסום לנכס הזה:** אותם 28 ימים שנה קודם (2025-08-21..2025-09-17) החזירו 0 שורות. הסיבה `טרם אומת`. `נבדק בהרצה`

## 3. כלים מפורטים

עמודת "חוזר": **גולמי** = שורות נתונים · **ממצא** = הכלי כבר סיווג או דירג · **המלצה** = הכלי אומר מה לעשות.
בסכמות של 37 הכלים לא ראינו תיאור של פלט מסוג המלצה, ובשלושת כלי הניתוח שהורצו לא חזרה המלצה. ק = קריאה, כ = כתיבה.

### 3.1 חשבון ונתונים גולמיים

| כלי | מטרה | פרמטרים (חובה **מודגש**) | חוזר | מגבלות | ק/כ | ראיה | נוגע ל |
|---|---|---|---|---|---|---|---|
| `get_account_info` | בדיקת חיבור: מי המשתמש ומה מחובר | אין | גולמי: `{user, googleAccounts[{email, scopes}], subscription, apiKey{scope}}` | אצלנו `subscription: null`; scope של גוגל = webmasters בלבד | ק | `נבדק בהרצה` | קריאה ראשונה בכל סקיל |
| `list_sites` | רשימת הנכסים המחוברים לחשבון | אין | גולמי. חזרו 6 נכסים. שדות לפי הסכמה: `siteUrl`, תגיות, מקור נתונים, `permissionLevel`, `readable`, `sharedBy`, `accessLevel` | לפי הסכמה `readable:false` = נכס לא מאומת בגוגל, וכל קריאה עליו תיכשל | ק | `נבדק בהרצה` | כל הסקילים (מקור ה-`siteUrl`) |
| `list_ga4_properties` | אילו נכסי GA4 קריאים ולאיזה אתר כל אחד מקושר | אין | גולמי: נכסים, `linkedSiteUrls`, `connected`, `account` | חזר `connected:false` + רמז שהסכמת Google Analytics לא ניתנה באפליקציה → כל כלי GA4 חסום | ק | `נבדק בהרצה` | conversion-tracking-audit, clarity-gsc |
| `query_search_analytics` | שאילתת searchAnalytics חופשית על נכס | **`siteUrl`**; בסיס (`searchType` כולל גם `discover`, `googleNews`); `dimensions` עד 4 מתוך date/query/page/country/device/searchAppearance; `filters` על query/page/country/device, 6 אופרטורים כולל regex; `rowLimit` 100 (מקס' 25,000); `startRow` | גולמי: `rows[{keys, clicks, impressions, ctr, position}]`, `rowCount`, `pagination`, `dataSource`, `settledThrough`, `dataMaturity`, `startDate`, `endDate` | ראו 4.1. לפי הסכמה: `searchAppearance`, `googleNews`, 3+ ממדים או `startRow>0` עוברים ל-API החי; `searchAppearance` אינו ממד לסינון | ק | `נבדק בהרצה` | כל סקיל שצורך GSC; clarity-gsc |
| `query_top_queries` | השאילתות המובילות לפי קליקים (עטיפה נוחה) | **`siteUrl`**; בסיס; `limit` 100 (מקס' 1,000); `offset` | גולמי | כאן הסכמה מגדירה דפדוף ב-`offset`, לא ב-`startRow`. בלי ממד page | ק | `מאומת בתיעוד` | onpage-optimizer, ctr-titles, internal-anchors |
| `query_top_pages` | דפי הנחיתה המובילים לפי קליקים | כמו `query_top_queries`; `searchType` כולל `discover` | גולמי | סכום דפים אינו סך נכס (4.1) | ק | `מאומת בתיעוד` | click-depth, seo-audit-report, clarity-gsc |
| `get_query_performance` | סדרה יומית לשאילתה אחת, אפשר מצומצמת לדף אחד | **`siteUrl`**, **`query`**; `pageUrl`; תאריכים. אין `searchType` | גולמי יומי | שאילתה אחת לקריאה; מול 60 קריאות בדקה זה לא כלי לסריקה רחבה | ק | `מאומת בתיעוד` | ctr-titles, onpage-optimizer (אחרי שינוי), algorithm-update-watch |
| `get_page_performance` | סדרה יומית לדף אחד: clicks, impressions, CTR, position | **`siteUrl`**, **`pageUrl`**; תאריכים. אין `searchType` | גולמי יומי | דף אחד לקריאה | ק | `מאומת בתיעוד` | content-decay, algorithm-update-watch, clarity-gsc |

### 3.2 הזדמנויות, CTR, קניבליזציה ואשכולות

| כלי | מטרה | פרמטרים (חובה **מודגש**) | חוזר | מגבלות | ק/כ | ראיה | נוגע ל |
|---|---|---|---|---|---|---|---|
| `analyze_cannibalization` | שאילתות שכמה דפים מתחרים עליהן, מדורגות באנטרופיית שאנון על ההופעות | **`siteUrl`**; בסיס; `minImpressions` 10 (על סך השאילתה); `limit` 100 (מקס' 1,000) | ממצא: `cannibalized[{query, pageCount, totalClicks, totalImpressions, entropy, pages[{url, clicks, impressions, ctr, position}]}]` | בפלט אין דף מנצח, אין אומדן קליקים, אין המלצה. ראו 4.2 | ק | `נבדק בהרצה` | onpage-optimizer, internal-anchors, seo-audit-report |
| `score_opportunities` | דירוג שאילתות במיקום 3–30 עם CTR נמוך, עם אומדן קליקים נוספים | **`siteUrl`**; בסיס; `minImpressions` 10; `limit` 100 (מקס' 1,000) | ממצא + אומדן: `opportunities[{query, clicks, impressions, ctr, position, score, potentialClicks}]` | רמת שאילתה בלבד, בלי שדה דף. האומדן מנופח — ראו 4.3 | ק | `נבדק בהרצה` | onpage-optimizer, ctr-titles, seo-audit-report |
| `find_page_poaching_opportunities` | שאילתות בין `minPosition` ל-`maxPosition`, עם אומדן רווח אם יגיעו ל-`targetPosition` | **`siteUrl`**; בסיס; `minPosition` 4; `maxPosition` 20; `targetPosition` 3; `fallbackBenchmark` awr/firstPageSage/sistrix/backlinko; `minImpressions` 10; `limit` 100 | ממצא + אומדן, עם `targetCtr` ו-`ctrSource` ("own" או שם הבנצ'מרק) | האומדן נגזר מעקומת ה-CTR של האתר עצמו; בנצ'מרק רק כשאין נתון במיקום היעד. האם חוזר שדה דף — `טרם אומת` | ק | `מאומת בתיעוד` | onpage-optimizer, internal-anchors |
| `analyze_ctr_curve` | עקומת CTR בפועל לפי מיקום 1–20 מול בנצ'מרקים | **`siteUrl`**; בסיס; `benchmarkSource` awr 2026 / firstPageSage 2026 / sistrix 2024 / backlinko 2019 / `all`; `minImpressions` 10 | ממצא: לכל דלי `benchmarks` ו-`deltas` (נקודות אחוז; שלילי = מתחת לצפי) | רמת נכס, לא רמת דף. חציון או ממוצע בדלי — `טרם אומת` | ק | `מאומת בתיעוד` | ctr-titles, seo-audit-report |
| `get_branded_performance` | פיצול ממותג / לא-ממותג | **`siteUrl`**; בסיס; השוואה; `limit` 25 (מקס' 500) | ממצא: סיכומים, נתח ממותג, מגמה יומית, שאילתות מובילות לכל צד | דורש מילות מותג שמורות על הנכס (נכתבות ב-`update_site`, כלי כתיבה); בלעדיהן `notConfigured`. שני הצדדים מסתכמים לסך הנכס | ק | `מאומת בתיעוד` | seo-audit-report, ctr-titles (סינון מותג) |
| `get_longtail_clusters` | חלוקת **דפים** ל-Head / Chunky Middle / Long Tail לפי מרפק בעקומת הקליקים המצטברת | **`siteUrl`**; תאריכים; `preset` default/content/ecommerce/small_site/enterprise; `sensitivity`; `pagesPerCluster` 10 (0–200); `includeZeroClicks` | ממצא: לכל שכבה מספר דפים, נתחי קליקים והופעות, ממוצעים, נקודות ברך, דפים מובילים | אשכול לפי תנועה, לא לפי נושא | ק | `מאומת בתיעוד` | content-decay (גיזום), seo-audit-report |
| `list_topic_clusters` | הגדרות האשכולות השמורות על הנכס | **`siteUrl`** | גולמי: שם + מילות מפתח | לפי הסכמות אשכול נוצר ידנית ב-`create_topic_cluster`; גילוי נושאים אוטומטי — `טרם אומת` | ק | `מאומת בתיעוד` | topic-cluster |
| `get_topic_cluster_performance` | מדדים לכל אשכול + דלי "unclustered" | **`siteUrl`**; בסיס; השוואה; `clusterId` (או `"unclustered"`) לקידוח; `scanLimit` 50,000 (1,000–200,000); `limit` 100 | ממצא מצטבר; `overlappingQueries`, `truncated` | התאמה = תת-מחרוזת או `/regex/` על טקסט השאילתה, לא לפי ה-URL שגוגל בחר. אשכולות חופפים → הסכומים לא מסתכמים לסך הנכס | ק | `מאומת בתיעוד` | topic-cluster |

### 3.3 דעיכה, שינויי דירוג ועדכוני אלגוריתם

| כלי | מטרה | פרמטרים (חובה **מודגש**) | חוזר | מגבלות | ק/כ | ראיה | נוגע ל |
|---|---|---|---|---|---|---|---|
| `find_decaying_content` | דפים או שאילתות שאיבדו קליקים בין תקופת בסיס לתקופה אחרונה | **`siteUrl`**; בסיס (= התקופה האחרונה); השוואה (= הבסיס); `dimension` page/query (ברירת מחדל page); `minImpressions` 100; `limit` 100 | ממצא: דליי חומרה severe >50%, moderate 20–50%, mild <20% | רק מפתחות עם בסיס משמעותי (clicks>0, impressions ≥ `minImpressions`). הסכמה מתארת סימון ירידה; סיווג *סיבה* לא מתואר בה — `טרם אומת` | ק | `מאומת בתיעוד` | content-decay |
| `get_decay_overview` | מטריצה: שורה לשאילתה או דף, עמודה לכל חודש או שבוע | **`siteUrl`**; `months` 16 (מקס' 24); `granularity` month/week; `metric` clicks/impressions/position/ctr; `dimension` (ברירת מחדל query); `search`; `clusterKeywords` (עד 200); `limit` 50 (מקס' 1,000); תאריכים רק כזוג | גולמי מצטבר: `periods`, `values` לכל שורה, סיכום לכל תקופה | ברירת מחדל: חודשים מלאים עד סוף החודש שעבר. לנכס שלנו שנה אחורה חזרה ריקה (סעיף 2), אז צפויות עמודות ריקות | ק | `מאומת בתיעוד` | content-decay, topic-cluster |
| `get_ranking_changes` | סיווג שאילתות או דפים ל-new / lost / improved / declined בין שתי תקופות | **`siteUrl`**; בסיס; השוואה; `dimension` (ברירת מחדל query); `limit` 50 (מקס' 1,000) | ממצא | "שיפור" = מיקום ממוצע נמוך יותר, לא קליקים. בנתיב המחסן כל תקופה קוראת עד max(limit×10, 1,000) שורות | ק | `מאומת בתיעוד` | content-decay, algorithm-update-watch |
| `detect_anomalies` | ימים חריגים בסדרה היומית של הנכס | **`siteUrl`**; `days` 90 (21–480); `metric` clicks/impressions/ctr/position; `sensitivity` 3.5 (2–6) | ממצא: spike/drop עם חומרה critical→info | Modified Z-score עם MAD ובסיס לפי יום בשבוע. רמת נכס; חלון נגרר, בלי תאריכים | ק | `מאומת בתיעוד` | algorithm-update-watch |
| `detect_change_points` | תאריכים שבהם הרמה השתנתה לאורך זמן (step change) | **`siteUrl`**; `days` 180 (14–480); `metric` clicks/impressions; `sensitivity` 2 (1–5) | ממצא: לכל שבר ממוצע לפני ואחרי ואחוז שינוי | רמת נכס; בלי position ו-ctr | ק | `מאומת בתיעוד` | algorithm-update-watch; אימות לפני/אחרי |
| `list_algo_updates` | עדכוני דירוג מאושרים מ-Google Search Status Dashboard | `startDate`, `endDate` — אופציונליים. אין `siteUrl` | גולמי | רק `service_name="Ranking"`; תקלות אינדוקס וסריקה מסוננות החוצה | ק | `מאומת בתיעוד` | algorithm-update-watch |
| `get_position_distribution` | פילוח לפי רצועות מיקום 1–3 / 4–10 / 11–20 / 21+ | **`siteUrl`**; בסיס; `granularity` period/daily; `dimension` query/page; `scanLimit` 50,000 (1,000–200,000) | ממצא מצטבר; ב-daily: מספר שאילתות ייחודיות בכל רצועה ליום | בנתיב ה-API נסרקות רק `scanLimit` השורות המובילות — לבדוק `truncated`. daily תמיד לפי query | ק | `מאומת בתיעוד` | algorithm-update-watch, seo-audit-report |

### 3.4 אינדוקס, sitemap, מהירות ומדידת לפני/אחרי

| כלי | מטרה | פרמטרים (חובה **מודגש**) | חוזר | מגבלות | ק/כ | ראיה | נוגע ל |
|---|---|---|---|---|---|---|---|
| `inspect_url` | URL Inspection של גוגל לכתובת אחת | **`siteUrl`**, **`inspectionUrl`** | גולמי: verdict, coverage, robots.txt, סריקה אחרונה, crawled-as | מכסה 2,000 ליום לנכס, משותפת לממשק ולכל קריאת MCP. התוצאה נשמרת בהיסטוריית הבדיקות של GSC Wizard. האם מפתח read מספיק — `טרם אומת` | ק* | `מאומת בתיעוד` | click-depth, seo-audit-report |
| `bulk_inspect_urls` | אותה בדיקה לרשימה, בטור | **`siteUrl`**, **`urls`** (1–2,000) | גולמי + רשימת הכתובות שלא הספיק | נעצר כשהמכסה נגמרת. קודם `get_inspection_quota` | ק* | `מאומת בתיעוד` | click-depth, seo-audit-report |
| `list_sitemaps` | ה-sitemaps שהוגשו לגוגל | **`siteUrl`** | גולמי: זמני הגשה, אזהרות ושגיאות, submitted/indexed לפי סוג תוכן | — | ק | `מאומת בתיעוד` | click-depth, seo-audit-report |
| `get_sitemap_performance` | מושך sitemap ומצמיד לכל URL את ביצועי ה-GSC שלו | **`siteUrl`**, **`sitemapUrl`**; בסיס; `maxUrls` 500 (מקס' 2,000) | גולמי מוצמד; `indexedInGscCount`, `crawlNote` | `indexedInGscCount` = כתובות שקיבלו הופעות, לא סטטוס אינדוקס. מרחיב רמה אחת של index, עד 5 קבצים. UA: `GSCWizard-Bot/1.0 (On-Page SEO Checker)` — חסימה בשרת מחזירה ריק | ק | `מאומת בתיעוד` | click-depth (יתומים), seo-audit-report |
| `get_core_web_vitals` | נתוני שדה CrUX: היסטוריה שבועית של LCP/INP/CLS + FCP/TTFB ב-p75 | **`siteUrl`**; `url` (דף בודד); `formFactor` ALL/PHONE/DESKTOP/TABLET | גולמי + דירוג good/needs-improvement/poor + נסיגות שבוע-מול-שבוע | דורש מפתח CrUX API מוגדר באפליקציה, אחרת `notConfigured`; `noData` כשאין מספיק תנועה. המצב אצלנו — `טרם אומת` | ק | `מאומת בתיעוד` | wordpress-speed-optimizer |
| `list_annotations` | הערות על הגרפים: פלטפורמה (עדכוני גוגל), חשבון, נכס | `siteUrl`, `startDate`, `endDate` — כולם אופציונליים | גולמי | — | ק | `מאומת בתיעוד` | algorithm-update-watch; יומן שינויים |
| `create_annotation` | רישום אירוע על ציר הזמן, למשל "הוחלפו טייטלים" | **`eventDate`**, **`label`** (≤200); `siteUrl` (בלעדיו = כל החשבון); `description` ≤1,000; `category` ≤50; `color` hex | אישור יצירה | **כתיבה** — לפי עמוד המפתחות דורש scope של read&write; המפתח שלנו read | כ | `מאומת בתיעוד` | ctr-titles, onpage-optimizer, internal-anchors (סימון מועד השינוי) |
| `list_experiments` | ניסויי SEO שמוגדרים לנכס: קבוצות URL של control/variant, סטטוס, תאריך התחלה | **`siteUrl`**; `includeArchived` | גולמי | ברשימת 109 הכלים לא מצאנו כלי ליצירת ניסוי; איך מגדירים ניסוי — `טרם אומת` | ק | `מאומת בתיעוד` | ctr-titles, onpage-optimizer |
| `get_experiment_results` | ציון לניסוי: צמיחת קליקים של variant מול control, תקופה מול תקופה | **`siteUrl`**, **`experimentId`**; תאריכים; `confidenceLevel` 0.95 (0.8–0.999) | ממצא: clicks/impressions/CTR לקבוצה עם CI, מבחן two-proportion, uplift, winner, אזהרת low-power | חלון ההשוואה = התקופה הקודמת באותו אורך. האם 99 קליקים ב-28 יום מספיקים למובהקות — `טרם אומת` | ק | `מאומת בתיעוד` | ctr-titles, onpage-optimizer |

`ק*` = קורא מגוגל, אבל רושם תוצאה בצד GSC Wizard וצורך מכסה יומית.

### 3.5 GA4 והמרות

כל החמישה: `מאומת בתיעוד` · **חסום: GA4 לא מחובר** (ראו `list_ga4_properties`). לפי הסכמות הם מחזירים `notConfigured` עם סיבה, לא שגיאה.

| כלי | מטרה | פרמטרים (חובה **מודגש**) | חוזר | מגבלות | ק/כ | נוגע ל |
|---|---|---|---|---|---|---|
| `get_ga4_overview` | סיכום GA4: sessions, users, engagement, bounce, key events | **`siteUrl`**; תאריכים; השוואה (ברירת מחדל: תקופה קודמת → `prevSummary`); `filters`; `includeTimeseries` | גולמי | — | ק | conversion-tracking-audit, seo-audit-report |
| `query_ga4_report` | פילוח GA4 אחד לקריאה | **`siteUrl`**, **`dimension`** channel/sourceMedium/page/landingPage/country/device/event; תאריכים; השוואה; `filters`; `limit` 50 (מקס' 1,000) | גולמי; סט המדדים תלוי בממד (`metrics`) | ממד אחד לקריאה | ק | conversion-tracking-audit, clarity-gsc |
| `get_ga4_key_events` | פילוח על key event אחד + רשימת ה-key events שקיימים בנכס | **`siteUrl`**; `keyEvent` (בלעדיו נבחר הפעיל ביותר); תאריכים; השוואה; `filters`; `limit` 25 | גולמי: סיכומים, שיעורי המרה, פילוח לפי ערוץ, מקור, דף, מדינה, מכשיר | — | ק | conversion-tracking-audit |
| `get_blended_landing_pages` | חיבור GSC + GA4 לכל דף נחיתה לפי נתיב קנוני | **`siteUrl`**; תאריכים; השוואה; `organicOnly` true; `dateGranularity` none/day/week/month; `limit` 50 (מקס' 500) | גולמי מוצמד; מסמן דפי GSC-only ו-GA4-only | — | ק | clarity-gsc, seo-audit-report, conversion-tracking-audit |
| `get_query_value_attribution` | אומדן sessions, key events והכנסה לכל שאילתה לפי נתח הקליקים שלה בדף | **`siteUrl`**; תאריכים; `organicOnly`; `dateGranularity`; `limit` 1,000 (מקס' 5,000) | אומדן, לא מדידה; `matchedClickShare` | הסכמה עצמה מצהירה על נקודות עיוורות: מכשיר, מדינה, תאריך, וכל קליק נחשב שווה-סיכוי להמיר | ק | onpage-optimizer (תעדוף), seo-audit-report |

## 4. ממצאי הרצה — `נבדק בהרצה`, 2026-09-19

**4.1 `query_search_analytics`**
- הדפדוף עובד עם `startRow`. ה-`pagination.note` של השרת אומר "offset", אבל מארגומנט `offset` השרת מתעלם בשקט ועמוד 1 חוזר שוב. את `pagination.nextOffset` מעבירים כ-`startRow`.
- עם `dimensions: []` או בלי `dimensions` חוזרת שורת סיכום אחת לנכס. עם [query, page] חזרו 1,418 שורות ל-28 יום, `hasMore:false`.
- סכום ההופעות על שורות [page] הוא 20,129, מול 16,649 בסך הנכס: צבירה לפי URL סופרת כפול. אסור לסכם דפים כדי לקבל סך נכס.
- בשדה `dataMaturity` יש `firstIncompleteDate`, `settledThrough`, `dateBasis`.

**4.2 `analyze_cannibalization`**
- הפלט ממוין לפי `entropy`. דף מנצח, אומדן קליקים והמלצה לא חוזרים — אלה נשארים אצלנו ב-`find_cannibalisation()`.
- ספירה לפי `minImpressions`: 1→143, 10→96, 20→71, 30→59, 50→47.
- שאילתת המותג "sass srq" (24 כתובות) ראשונה באנטרופיה, ולכן מסננים מותג לפני שמציגים.
- אמינות: ראו "תשובה ריקה" בסעיף 2.

**4.3 `score_opportunities`**
- חזרו 29 שורות ב-`minImpressions=50`. רמת שאילתה בלבד; אין שדה דף או URL בפלט.
- סכום `potentialClicks` = 1,082, כשכל הנכס קיבל 99 קליקים בחלון. השורה הראשונה טוענת ל-+414 קליקים לשאילתה במיקום 7.3. לא מציגים את המספר ללקוח כצפי.

## 5. מה עלה מהסכמות ושווה בדיקה — `מאומת בתיעוד`

- **היסטוריה:** הסכמה של `get_query_shape_trend` מציינת ש-Search Console שומר כ-16 חודשים. `get_decay_overview` מקבל עד 24 חודשים, `detect_*` עד 480 יום, `forecast_traffic` עד 1,000 יום; מאיפה מגיע העודף מעבר ל-16 חודשים — `טרם אומת`.
- **SERP features:** הממד `searchAppearance` קיים ב-`query_search_analytics` (נתיב API חי, לא ניתן לסינון). שדה ייעודי ל-AI Overview לא ראינו ב-77 הסכמות שקראנו; ב-32 הנותרות — `טרם אומת`.
- **ai-visibility (מתוכנן):** `analyze_query_shapes` ו-`get_query_shape_trend` מסווגים שאילתות לפי *צורה* (שיחתית, follow-up ועוד; היוריסטיקה ב-27 שפות) — ראיה נסיבתית, לא מדידת AI Overview. `get_ga4_llm_traffic` סופר הפניות מ-ChatGPT, Perplexity ואחרים, ומצהיר שתנועת AI Overviews / AI Mode **לא** נכללת כי אין לה referrer נפרד; חסום עד חיבור GA4.
- **לפני/אחרי:** `create_annotation` (כתיבה) + `get_page_performance` / `get_query_performance` + `detect_change_points` + `get_experiment_results`. שימו לב ש-`compare_migration` משווה A מול B **באותו חלון**, לא שתי תקופות.
- **חפיפה לסקילים מחוץ לרשימה המפורטת:** `audit_onpage_seo` (סריקה חיה, עד 25 כתובות לקריאה) נוגע ל-onpage-optimizer; `generate_seo_report` (HTML או JSON בקריאה אחת, חלון 7–180 יום) נוגע ל-seo-audit-report. שניהם לא הורצו.

## 6. אינדקס קצר — 72 הכלים הנותרים

`*` = הגלוס נגזר משם הכלי בלבד (`טרם אומת`). בלי כוכבית = הסכמה נקראה (`מאומת בתיעוד`). אף אחד מהם לא הורץ.
לפי הסכמות שנקראו, כלי Bing מחזירים `notConfigured` כשאין מפתח Bing Webmaster באפליקציה; כלי GA4 חסומים כמו ב-3.5.

| קבוצה | כלי | מה הוא עושה |
|---|---|---|
| ניתוח GSC נוסף | `analyze_query_shapes` | סיווג שאילתות לפי צורה (AI) |
| ניתוח GSC נוסף | `get_query_shape_trend` | מגמת שאילתות בצורת AI לאורך זמן |
| ניתוח GSC נוסף | `analyze_sampling_impact` | כמה נתונים GSC מסתיר באנונימיזציה |
| ניתוח GSC נוסף | `audit_onpage_seo` | סריקת on-page חיה, עד 25 דפים |
| ניתוח GSC נוסף | `breakdown_by_path` | צבירת ביצועים לפי תיקייה ותת-דומיין |
| ניתוח GSC נוסף | `forecast_traffic` | תחזית קליקים עם עונתיות ומגמה |
| ניתוח GSC נוסף | `get_site_summary` | סיכום נכס מול תקופה קודמת |
| ניתוח GSC נוסף | `get_cross_site_summary` | סיכום לכל נכסי החשבון |
| ניתוח GSC נוסף | `query_countries` | ביצועים לפי מדינה (ISO alpha-3) |
| ניתוח GSC נוסף | `query_devices` | ביצועים לפי סוג מכשיר |
| Bing | `list_bing_sites` * | רשימת האתרים ב-Bing Webmaster |
| Bing | `get_bing_traffic_stats` * | סטטיסטיקת תנועה כללית מ-Bing |
| Bing | `get_bing_query_stats` * | ביצועי שאילתות ב-Bing |
| Bing | `get_bing_page_stats` * | ביצועי דפים ב-Bing |
| Bing | `get_bing_page_queries` * | השאילתות של דף ב-Bing |
| Bing | `get_bing_query_page_stats` * | צירופי שאילתה-דף ב-Bing |
| Bing | `get_bing_query_page_trend` | מגמה יומית לצירוף שאילתה-דף (כ-6 חודשים) |
| Bing | `get_bing_keyword_stats` | נפח הופעות שבועי למילה ב-Bing |
| Bing | `get_bing_link_counts` | ספירת קישורים נכנסים לכל URL |
| Bing | `get_bing_crawl_stats` * | סטטיסטיקת סריקה של Bing |
| Bing | `get_bing_crawl_issues` * | בעיות סריקה ש-Bing מדווח |
| Bing | `get_bing_feeds` | sitemaps ופידים שהוגשו ל-Bing |
| Bing | `get_bing_url_submission_quota` * | מכסת הגשת כתובות ל-Bing |
| GA4 שאר | `get_ga4_ecommerce` * | נתוני מסחר מ-GA4 |
| GA4 שאר | `get_ga4_error_pages` * | דפי שגיאה לפי GA4 |
| GA4 שאר | `get_ga4_llm_traffic` | תנועה מעוזרי AI לפי referrer |
| אינדוקס ו-IndexNow | `get_inspection_quota` | יתרת מכסת הבדיקות היומית |
| אינדוקס ו-IndexNow | `list_url_inspections` | היסטוריית בדיקות URL שמורה |
| אינדוקס ו-IndexNow | `get_indexing_tracker` | הגדרות וסיכום מעקב האינדוקס |
| אינדוקס ו-IndexNow | `get_indexing_tracker_report` | ציון בריאות אינדוקס 0–100 |
| אינדוקס ו-IndexNow | `list_tracked_urls` | הכתובות במעקב והסטטוס שלהן |
| אינדוקס ו-IndexNow | `add_tracked_urls` | הוספת כתובות למעקב, תקרה 1,800 (כתיבה) |
| אינדוקס ו-IndexNow | `remove_tracked_urls` * | הסרת כתובות מהמעקב (כתיבה) |
| אינדוקס ו-IndexNow | `check_tracked_url_now` | בדיקה מיידית, עד 10 כתובות (כתיבה) |
| אינדוקס ו-IndexNow | `submit_sitemap` | הגשת sitemap קיים לגוגל (כתיבה) |
| אינדוקס ו-IndexNow | `get_indexnow_settings` * | הגדרות IndexNow של הנכס |
| אינדוקס ו-IndexNow | `update_indexnow_settings` * | עדכון הגדרות IndexNow (כתיבה) |
| אינדוקס ו-IndexNow | `list_indexnow_submissions` * | היסטוריית הגשות IndexNow |
| אינדוקס ו-IndexNow | `submit_indexnow_urls` | פינג IndexNow, עד 100 כתובות (כתיבה) |
| מיגרציה | `compare_migration` | השוואת A מול B באותו חלון |
| מיגרציה | `list_migration_redirects` * | מיפויי ההפניות השמורים |
| מיגרציה | `add_migration_redirects` | שמירת מיפויי ישן→חדש, מצטבר (כתיבה) |
| מיגרציה | `delete_migration_redirects` * | מחיקת סט מיפויים (כתיבה) |
| דוחות ושיתוף | `generate_seo_report` | דוח מלא בקריאה אחת, HTML/JSON |
| דוחות ושיתוף | `create_shared_report` | שמירת טבלה כדוח לשיתוף (כתיבה) |
| דוחות ושיתוף | `list_shared_reports` * | רשימת הדוחות השמורים |
| דוחות ושיתוף | `delete_shared_report` * | מחיקת דוח שמור (כתיבה) |
| דוחות ושיתוף | `create_report_client` | הוספת איש קשר לדוחות (כתיבה) |
| דוחות ושיתוף | `list_report_clients` * | רשימת אנשי הקשר |
| דוחות ושיתוף | `manage_report_access` | מתן או שלילת גישה לדוח (כתיבה) |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `list_tags` * | רשימת התגיות בחשבון |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `manage_tags` | יצירה, שיוך, שינוי שם, מחיקה (כתיבה) |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `get_tag_group_view` | דוח מאוחד לנכסים עם תגית |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `list_saved_filters` * | המסננים השמורים לנכס |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `create_saved_filter` | שמירת מסנן לנכס (כתיבה) |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `update_saved_filter` * | עדכון מסנן שמור (כתיבה) |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `delete_saved_filter` * | מחיקת מסנן שמור (כתיבה) |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `list_content_groups` * | הגדרות קבוצות התוכן |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `get_content_group_performance` | מדדים לכל קבוצת תוכן |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `create_content_group` | קבוצת תוכן לפי חוקי URL (כתיבה) |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `update_content_group` * | עדכון קבוצת תוכן (כתיבה) |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `delete_content_group` * | מחיקת קבוצת תוכן (כתיבה) |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `create_topic_cluster` | אשכול = שם + עד 1,000 מילים (כתיבה) |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `update_topic_cluster` * | עדכון מילות האשכול (כתיבה) |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `delete_topic_cluster` * | מחיקת אשכול (כתיבה) |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `update_annotation` * | עדכון הערה קיימת (כתיבה) |
| תגיות / מסננים / קבוצות / אשכולות / הערות | `delete_annotation` * | מחיקת הערה (כתיבה) |
| ניהול אתרים | `add_site` | רישום נכס GSC מאומת (כתיבה) |
| ניהול אתרים | `update_site` | תגיות, מילות מותג, sitemaps (כתיבה) |
| ניהול אתרים | `delete_site` * | הסרת נכס מ-GSC Wizard (כתיבה) |
| ניהול אתרים | `create_gsc_property` | יצירת נכסי URL-prefix בגוגל (כתיבה) |
| ניהול אתרים | `set_dashboard_visibility` | הצגה או הסתרה; צורך slot בתוכנית (כתיבה) |
