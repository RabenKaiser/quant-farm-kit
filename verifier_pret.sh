#!/bin/bash
# verifier_pret.sh -- ecrit /workspace/resultats/etat_pret.txt : un VERDICT lisible.
# Lance le terminal maitre (contact), attend, lit le journal, tranche.
# Ne tue PAS le terminal a la fin : c'est lui qui construit la base de symboles
# au fil du temps, et c'est sur ce datadir que le superviseur s'appuiera.
# Appele par onstart_template.sh en tache de fond (nohup) pour ne pas retarder le boot.
set -uo pipefail
export WINEPREFIX="${WORKSPACE:-/workspace}/wpx"
export PATH="/opt/wine-stable/bin:${PATH}"
export WINEDLLOVERRIDES="mscoree=d;mshtml=d"
export WINEDEBUG=-all
export TZ=UTC
MT5="${WORKSPACE:-/workspace}/MT5"
RES="${WORKSPACE:-/workspace}/resultats"
ETAT="$RES/etat_pret.txt"
mkdir -p "$RES"
: > "$ETAT"

ligne() { echo "$1" >> "$ETAT"; }
ok()    { ligne "  [OK]    $1"; }
ko()    { ligne "  [KO]    $1"; PROBLEME=1; }
PROBLEME=0

ligne "================================================================"
ligne " ETAT DE L'INSTANCE — $(date -Is)"
ligne "================================================================"

# --- 1. le kit est-il descendu en entier ?
if [ -f "$MT5/terminal64.exe" ]; then
    ok "programme MT5 present ($(du -sh "$MT5" | cut -f1))"
else
    ko "terminal64.exe ABSENT : le kit n'est pas descendu"
fi
for f in "${WORKSPACE:-/workspace}/scripts/superviseur_boucle.py" \
         "${WORKSPACE:-/workspace}/scripts/burst/travailleur_vast.py"; do
    [ -f "$f" ] && ok "script present : $(basename "$f")" || ko "script ABSENT : $f"
done
if [ -f "${WORKSPACE:-/workspace}/scripts/burst/paquet/MQL5/Experts/eepeek/TRIGGER_LAB.ex5" ]; then
    ok "EA de la file present (avec jalons)"
else
    ko "EA TRIGGER_LAB.ex5 absent"
fi

# --- 2. Wine et le prefixe
if command -v wine >/dev/null 2>&1; then ok "Wine : $(wine --version 2>/dev/null)"; else ko "Wine introuvable"; fi
[ -d "$WINEPREFIX/drive_c" ] && ok "prefixe Wine pret" || ko "prefixe Wine absent"

# --- 3. l'ini
if [ -f "$MT5/config/contact.ini" ]; then
    L=$(grep -c '^Login=' "$MT5/config/contact.ini" || true)
    [ "$L" -ge 1 ] && ok "ini de contact ecrit (identifiants depuis l'environnement)" \
                   || ko "ini present mais sans Login"
else
    ko "ini de contact absent"
fi

# --- 4. premier contact reel : lancer le terminal et LIRE le journal
ligne ""
ligne "--- premier contact (le vrai test) ---"
if pgrep -f terminal64 >/dev/null 2>&1; then
    ligne "  un terminal tourne deja : on lit son journal"
else
    cd "$MT5"
    setsid xvfb-run -a wine terminal64.exe /portable /config:config\\contact.ini \
        > "$RES/premier_contact.log" 2>&1 < /dev/null &
    ligne "  terminal lance, attente de 150 s (base de symboles)"
    sleep 150
fi

JOUR=$(ls -t "$MT5"/logs/*.log 2>/dev/null | head -1)
if [ -n "${JOUR:-}" ]; then
    TEXTE=$(iconv -f UTF-16LE -t UTF-8 "$JOUR" 2>/dev/null || cat "$JOUR")
    ligne ""
    ligne "  journal : $(basename "$JOUR")"
    AUTH=$(echo "$TEXTE" | grep -oE "'[0-9]+': terminal synchronized[^[:cntrl:]]*" | tail -1)
    FAIL=$(echo "$TEXTE" | grep -oE "authorization on [^[:cntrl:]]*failed[^[:cntrl:]]*" | tail -1)
    if [ -n "$AUTH" ]; then
        ok "AUTORISE — $AUTH"
        ligne "  ==> l'instance est connectee : elle peut tirer ses donnees"
    elif [ -n "$FAIL" ]; then
        ko "NON AUTORISE — $FAIL"
        ligne "  ==> verifier DEMO_LOGIN / DEMO_PASSWORD dans le template Vast"
    else
        ko "aucune ligne d'autorisation : le terminal n'a pas abouti en 150 s"
    fi
    ligne ""
    ligne "  --- 6 dernieres lignes du journal ---"
    echo "$TEXTE" | tail -6 | sed 's/^/    /' >> "$ETAT"
else
    ko "aucun journal de terminal produit"
fi

# --- 5. verdict
ligne ""
ligne "================================================================"
if [ "$PROBLEME" -eq 0 ]; then
    ligne " VERDICT : PRET"
    ligne " Demarrer les travailleurs : python3 /workspace/scripts/burst/travailleur_vast.py"
    ligne "   --travailleur 0 --datadir /workspace/W00 --sortie /workspace/resultats"
else
    ligne " VERDICT : A CORRIGER (voir les lignes [KO] ci-dessus)"
fi
ligne "================================================================"
echo "etat ecrit : $ETAT"
