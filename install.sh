#!/usr/bin/env bash
# התקנת ערכת הסקילים ל-SEO.
# מעתיק את seo_core ל-~/.claude/seo_core ואת הסקילים ל-~/.claude/skills.
# בטוח להרצה חוזרת — מעדכן במקום.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"
CORE_DEST="${CLAUDE_DIR}/seo_core"
SKILLS_DEST="${CLAUDE_DIR}/skills"
SEO_DIR="${CLAUDE_DIR}/seo"

log() { printf '[%s] %s %s\n' "$(date +%H:%M:%S)" "$1" "$2"; }
ok()   { log "✅" "$1"; }
info() { log "ℹ️ " "$1"; }
warn() { log "⚠️ " "$1"; }

echo
echo "═══════════════════════════════════════════════════════"
echo "  התקנת ערכת סקילים ל-SEO"
echo "═══════════════════════════════════════════════════════"
echo

# ── seo_core ───────────────────────────────────────────
info "מעתיק את seo_core ל-${CORE_DEST}"
mkdir -p "${CORE_DEST}"
rm -rf "${CORE_DEST:?}/seo_core"
cp -R "${REPO_DIR}/seo_core" "${CORE_DEST}/seo_core"
ok "seo_core הותקן"

# ── skills ─────────────────────────────────────────────
mkdir -p "${SKILLS_DEST}"
installed=0
if compgen -G "${REPO_DIR}/skills/*/SKILL.md" > /dev/null; then
  for skill_path in "${REPO_DIR}"/skills/*/; do
    name="$(basename "${skill_path}")"
    rm -rf "${SKILLS_DEST:?}/${name}"
    cp -R "${skill_path}" "${SKILLS_DEST}/${name}"
    ok "סקיל הותקן: ${name}"
    installed=$((installed + 1))
  done
else
  info "אין עדיין סקילים בתיקיית skills/ — מותקנת התשתית בלבד"
fi

# ── config ─────────────────────────────────────────────
mkdir -p "${SEO_DIR}/data"

if [[ ! -f "${SEO_DIR}/.env" ]]; then
  cp "${REPO_DIR}/.env.example" "${SEO_DIR}/.env"
  chmod 600 "${SEO_DIR}/.env"
  warn "נוצר ${SEO_DIR}/.env — צריך למלא אותו לפני הרצה"
else
  ok ".env קיים — לא נדרס"
fi

if [[ ! -f "${SEO_DIR}/clients.json" ]]; then
  cp "${REPO_DIR}/clients.example.json" "${SEO_DIR}/clients.json"
  warn "נוצר ${SEO_DIR}/clients.json — צריך להשלים פרטי לקוחות"
else
  ok "clients.json קיים — לא נדרס"
fi

# ── בדיקת שפיות ────────────────────────────────────────
info "מריץ בדיקת תקינות"
if PYTHONPATH="${CORE_DEST}" python3 -c "from seo_core.schema import Finding; from seo_core.clients import load_all" 2>/dev/null; then
  ok "seo_core נטען בהצלחה"
else
  warn "seo_core לא נטען — בדוק גרסת פייתון (נדרש 3.10+)"
fi

echo
echo "═══════════════════════════════════════════════════════"
echo "  📋 סיכום"
echo "═══════════════════════════════════════════════════════"
echo "  סקילים שהותקנו:     ${installed}"
echo "  seo_core:           ${CORE_DEST}/seo_core"
echo "  הגדרות:             ${SEO_DIR}"
echo
echo "  הצעד הבא:"
echo "    1. מלא את ${SEO_DIR}/.env"
echo "    2. השלם את ${SEO_DIR}/clients.json"
echo "    3. הרץ סקיל עם --self-check"
echo
