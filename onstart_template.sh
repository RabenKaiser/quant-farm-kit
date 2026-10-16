#!/bin/bash
# onstart_template.sh -- LE TEMPLATE VAST (champ "onstart" d'une instance/template).
#
# PRINCIPE (apres la mesure du 16/10) : Vast interdit les moteurs de conteneur dans
# les instances (pas de Docker-in-Docker, guide /etc/vast-agents-guide.md). Le
# template n'est donc PAS une image cuite : c'est UNE image de base quelconque +
# CE script, qui rend l'instance operationnelle de facon IDEMPOTENTE (relance sans
# dommage) en ~5 minutes :
#   1. Wine 11 si absent (recette prouvee, repli trusted=yes documente)
#   2. le kit MT5 (programme prouve + scripts) telecharge depuis une release GitHub
#   3. prefixe Wine, ini de contact, superviseur lance
# Tout ce qui compte vit dans ${WORKSPACE} (defaut /workspace).
#
# A definir dans le template Vast : KIT_URL = URL du dossier de release
# (ex. https://github.com/<compte>/<repo>/releases/download/v1 )

set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
export WINEPREFIX="${WORKSPACE:-/workspace}/wpx"
export WINEDEBUG=-all
# PIEGES PAYES le 16/10 sur l'instance reelle :
#  - sans ces overrides, wineboot -i ouvre une boite GUI (mono/gecko) que personne
#    ne peut cliquer sous Xvfb : le prefixe ne se finit jamais et terminal64 meurt
#    en silence ;
#  - ne JAMAIS mettre un demon (wineserver) au bout d'un pipe (`| tail`) : il ne
#    ferme jamais le tuyau et le script attend indéfiniment. Toujours un FICHIER.
export WINEDLLOVERRIDES="mscoree=d;mshtml=d"
export TZ=UTC
MT5="${WORKSPACE:-/workspace}/MT5"
RES="${WORKSPACE:-/workspace}/resultats"
LOG="$RES/onstart_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$MT5" "$RES" "${WORKSPACE:-/workspace}/jobs" "${WINEPREFIX}"
exec > >(tee -a "$LOG") 2>&1
echo "== onstart $(date -Is) =="

# --- 0. prerequis systeme (idempotent : rien a faire si deja la)
command -v xvfb-run >/dev/null 2>&1 || { apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq xvfb gnupg >/dev/null 2>&1; echo "xvfb/gnupg installes"; }

# --- 1. Wine 11 si absent
if ! command -v /opt/wine-stable/bin/wine >/dev/null 2>&1; then
    echo "== installation de Wine 11 =="
    dpkg --add-architecture i386
    # PIEGE (16/10) : l'image de base Vast livre un winehq.sources PERIME qui entre
    # en conflit. On nettoie avant tout.
    rm -f /etc/apt/sources.list.d/winehq*.sources /etc/apt/sources.list.d/winehq*.list
    cat > /etc/apt/sources.list.d/winehq.sources <<'EOF'
Types: deb
URIs: https://dl.winehq.org/wine-builds/ubuntu
Suites: noble
Components: main
Trusted: yes
EOF
    apt-get update -qq >/dev/null 2>&1
    apt-get install -y -qq --install-recommends winehq-stable >/dev/null 2>&1
    /opt/wine-stable/bin/wine --version || { echo "!! ECHEC wine"; exit 1; }
else
    echo "Wine deja present : $(/opt/wine-stable/bin/wine --version)"
fi
export PATH="/opt/wine-stable/bin:${PATH}"

# --- 2. kit MT5 depuis le depot GitHub (idempotent)
# KIT_URL = URL brute du dossier kit du depot, ex.
#   https://raw.githubusercontent.com/RabenKaiser/quant-farm-kit/main/kit
# GitHub refuse les fichiers de plus de 100 Mo dans un depot : mt5prog.tgz est
# donc DECOUPE en deux parties (part-aa, part-ab) que ce script recolle.
if [ ! -f "$MT5/terminal64.exe" ]; then
    echo "== telecharge le kit : ${KIT_URL:-NON_DEFINI} =="
    [ -n "${KIT_URL:-}" ] || { echo "!! KIT_URL non defini dans le template"; exit 1; }
    curl -fsSL --retry 3 -o /tmp/mt5prog.tgz.part-aa "$KIT_URL/mt5prog.tgz.part-aa"
    curl -fsSL --retry 3 -o /tmp/mt5prog.tgz.part-ab "$KIT_URL/mt5prog.tgz.part-ab"
    cat /tmp/mt5prog.tgz.part-aa /tmp/mt5prog.tgz.part-ab > /tmp/mt5prog.tgz
    curl -fsSL --retry 3 -o /tmp/scripts_pack.tgz "$KIT_URL/scripts_pack.tgz"
    tar -xzf /tmp/mt5prog.tgz      -C "$MT5"
    # PIEGE (repetition generale du 16/10) : sur une instance NEUVE le dossier
    # n'existe pas encore -- sans ce mkdir, tar echoue et l'instance demarre sans
    # scripts (donc sans file de travail) tout en ayant l'air d'avoir reussi.
    mkdir -p "${WORKSPACE:-/workspace}/scripts"
    tar -xzf /tmp/scripts_pack.tgz -C "${WORKSPACE:-/workspace}/scripts"
    echo "  kit en place : $(du -sh "$MT5" | cut -f1)"
else
    echo "MT5 deja en place : $(du -sh "$MT5" | cut -f1)"
fi

# --- 3. prefixe Wine (copie du template si present, sinon creation)
if [ ! -d "$WINEPREFIX/drive_c" ]; then
    echo "== prefixe Wine =="
    if [ -d /opt/wpx/drive_c ]; then cp -a /opt/wpx "$WINEPREFIX"; else timeout 180 xvfb-run -a wineboot -i > "$RES/wineboot.log" 2>&1; fi
    echo "  prefixe pret : $WINEPREFIX (code $?, journal wineboot.log)"
fi

# --- 4. ini de contact. Les identifiants viennent de l'ENVIRONNEMENT du template
# (DEMO_LOGIN / DEMO_PASSWORD) : le depot public n'en contient AUCUN. Sans variables,
# on retombe sur le compte maitre sans mot de passe valide : le terminal demarre,
# l'autorisation echoue, mais la base de symboles se construit quand meme.
mkdir -p "$MT5/config"
cat > "$MT5/config/contact.ini" <<EOF
[Common]
Login=${DEMO_LOGIN:-798154993}
Password=${DEMO_PASSWORD:-x}
Server=${DEMO_SERVER:-Dukascopy-demo-mt5-1}
ProxyEnable=0
NewsEnable=0
MaxBars=5000000
[StartUp]
EOF
echo "  ini ecrit (login ${DEMO_LOGIN:-798154993}, identifiants ${DEMO_PASSWORD:+fournis}${DEMO_PASSWORD:-absents})"

# --- 5. quarantaine respectee par construction
export LD_PRELOAD=/usr/local/lib/no_broker.so
[ -f /usr/local/lib/no_broker.so ] || { echo "stub no_broker absent : on continue sans (l'IP est fraiche)" ; unset LD_PRELOAD; }

# --- 6. les travailleurs (et PAS le surveillant de la boucle locale)
# Deux mesures du 16/10 en LISANT le code de travailleur_vast.py :
#  a) superviseur_boucle.py est le chien de garde de la boucle LOCALE (12h00->03h00,
#     relance boucle_nuit_locale.py) : le lancer sur une instance n'a aucun sens ;
#  b) le travailleur impose DISPLAY=:99 et WINEPREFIX=--prefixe-wine, et il lance
#     terminal64.exe avec cwd=<datadir> : il faut donc UN X VIRTUEL PERSISTANT et
#     une COPIE du programme dans chaque datadir.
# WORKERS = nombre de travailleurs a lancer (0 = aucun : on affiche la commande).
WORKERS="${WORKERS:-0}"
FILE="${QUEUE_DB:-${WORKSPACE:-/workspace}/queue/usine_indus.db}"
TRAV="${WORKSPACE:-/workspace}/scripts/burst/travailleur_vast.py"
if [ "$WORKERS" -gt 0 ] 2>/dev/null && [ -f "$TRAV" ] && [ -f "$FILE" ]; then
    # 1. X virtuel persistant sur :99 (le travailleur y envoie ses terminaux)
    if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
        setsid nohup Xvfb :99 -screen 0 1280x1024x24 > "$RES/xvfb99.log" 2>&1 < /dev/null &
        sleep 3
        echo "  Xvfb :99 lance (le travailleur l'exige)"
    fi
    # 2. les setfiles : le travailleur verifie que chaque ligne du bilan appartient
    #    bien a son fichier de reglages. Sans eux, il ne peut rien valider.
    SETF="${SETFILES_DIR:-$WINEPREFIX/drive_c/users/root/AppData/Roaming/MetaQuotes/Terminal/Common/Files}"
    mkdir -p "$SETF"
    NSET=$(ls -1 "$SETF" 2>/dev/null | wc -l)
    [ "$NSET" -gt 0 ] && echo "  setfiles : $NSET fichier(s) dans $SETF" \
                      || echo "  !! AUCUN setfile dans $SETF : les lots seront refuses"
    # 3. un travailleur par datadir, chacun avec SA copie du programme
    i=0
    while [ "$i" -lt "$WORKERS" ]; do
        DD="${WORKSPACE:-/workspace}/W$(printf %02d "$i")"
        if [ ! -f "$DD/terminal64.exe" ]; then
            mkdir -p "$DD"
            cp -a "$MT5/." "$DD/" 2>/dev/null
            echo "  datadir W$(printf %02d "$i") prepare (copie du programme)"
        fi
        if pgrep -f "travailleur_vast.py --travailleur $i " >/dev/null 2>&1; then
            echo "  travailleur $i deja actif"
        else
            setsid nohup python3 "$TRAV" --travailleur "$i" --datadir "$DD" \
                --prefixe-wine "$WINEPREFIX" --db "$FILE" --sortie "$RES" \
                --setfiles "$SETF" --delai-max "${DELAI_MAX:-420}" \
                >> "$RES/travailleur_$i.log" 2>&1 < /dev/null &
            echo "  travailleur $i lance (datadir $DD, journal $RES/travailleur_$i.log)"
        fi
        i=$((i + 1))
    done
elif [ ! -f "$TRAV" ]; then
    echo "  !! travailleur_vast.py absent : kit incomplet"
elif [ ! -f "$FILE" ]; then
    echo "  aucune file de lots dans $FILE : travailleurs NON lances."
    echo "  deposer la base de la file (usine_indus.db) dans $(dirname "$FILE"),"
    echo "  puis relancer ce script avec WORKERS=<nombre> pour demarrer."
else
    echo "  WORKERS=0 : travailleurs non lances (commande disponible ci-dessus)."
    echo "  python3 $TRAV --travailleur 0 --datadir ${WORKSPACE:-/workspace}/W00 --db $FILE --sortie $RES"
fi

echo "== onstart termine $(date -Is) =="

# --- 7. auto-test de premier demarrage : ecrit /workspace/resultats/etat_pret.txt
# En tache de fond pour ne pas retarder le boot : il lance le terminal maitre, attend
# 150 s, lit le journal et rend un VERDICT lisible (PRET / A CORRIGER) au lieu de
# laisser l'operateur fouiller les journaux.
VERIF="${WORKSPACE:-/workspace}/verifier_pret.sh"
[ -f "$VERIF" ] || VERIF="${WORKSPACE:-/workspace}/scripts/verifier_pret.sh"
# sinon on le prend a cote de ce script, dans le depot (il est versionne la-bas)
if [ ! -f "$VERIF" ] && [ -n "${KIT_URL:-}" ]; then
    curl -fsSL -o "${WORKSPACE:-/workspace}/verifier_pret.sh" "${KIT_URL%/*}/verifier_pret.sh" 2>/dev/null
    VERIF="${WORKSPACE:-/workspace}/verifier_pret.sh"
fi
if [ -f "$VERIF" ]; then
    setsid nohup bash "$VERIF" >> "$RES/etat_pret.log" 2>&1 < /dev/null &
    echo "auto-test de premier demarrage lance : etat dans $RES/etat_pret.txt (2-3 min)"
else
    echo "!! verifier_pret.sh introuvable : pas d'auto-test (kit incomplet ?)"
fi
