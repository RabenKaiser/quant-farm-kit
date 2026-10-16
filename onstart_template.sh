#!/usr/bin/env bash
# onstart_template.sh -- LE TEMPLATE VAST (champ "onstart" ou PROVISIONING_SCRIPT).
#
# PRINCIPE : Vast interdit les moteurs de conteneur dans une instance (pas de
# Docker-in-Docker). Le template n'est donc PAS une image cuite : c'est une image de
# base + CE script, qui rend l'instance operationnelle de facon IDEMPOTENTE en ~5 min :
#   1. Wine 11 si absent (recette prouvee)
#   2. le kit MT5 telecharge depuis le depot GitHub -- avec VERIFICATION D'INTEGRITE
#   3. prefixe Wine, ini de contact (identifiants par environnement), travailleurs
#   4. auto-test de premier demarrage -> /workspace/resultats/etat_pret.txt
#
# REGLE DE CONCEPTION : ce script ne doit JAMAIS "avoir l'air d'avoir reussi". Toute
# etape critique qui echoue sort en code non nul avec un message lisible. Revue adverse
# du 16/10 : cinq defauts bloquants corriges (extraction non verifiee, apt muet, code
# de retour de wineboot faux, copie de prefixe imbriquee, surveillant Windows).

# PROVISIONING_SCRIPT peut etre lance par sh/dash : on se re-execute en bash.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
export WINEPREFIX="${WORKSPACE:-/workspace}/wpx"
export WINEDEBUG=-all
# PIEGES PAYES le 16/10 sur l'instance reelle :
#  - sans ces overrides, wineboot -i ouvre une boite GUI (mono/gecko) que personne ne
#    peut cliquer sous Xvfb : le prefixe ne se finit jamais et terminal64 meurt en silence ;
#  - ne JAMAIS mettre un demon (wineserver) au bout d'un pipe (`| tail`) : il ne ferme
#    jamais le tuyau et le script attend indefiniment. Toujours un FICHIER.
export WINEDLLOVERRIDES="mscoree=d;mshtml=d"
export TZ=UTC

MT5="${WORKSPACE:-/workspace}/MT5"
SCR="${WORKSPACE:-/workspace}/scripts"
RES="${WORKSPACE:-/workspace}/resultats"
LOG="$RES/onstart_$(date +%Y%m%d_%H%M%S).log"
# tailles attendues du kit publie (verifiees octet pour octet le 16/10)
TAILLE_PART=65959979
TAILLE_TOTALE=131919958

mkdir -p "$RES" "${WORKSPACE:-/workspace}/jobs"
exec > >(tee -a "$LOG") 2>&1
echo "== onstart $(date -Is) =="

# --- 0. preflight : le montage et le disque
for i in $(seq 1 30); do [ -d "${WORKSPACE:-/workspace}" ] && break; sleep 2; done
[ -d "${WORKSPACE:-/workspace}" ] || { echo "!! WORKSPACE inaccessible"; exit 1; }
LIBRE=$(df -Pk "${WORKSPACE:-/workspace}" | awk 'NR==2 {print $4}')
[ "${LIBRE:-0}" -lt 8000000 ] && echo "!! ATTENTION : moins de 8 Go libres (${LIBRE} Ko) : le testeur en consomme 7 a 10"
echo "  espace libre : $(( ${LIBRE:-0} / 1024 )) Mo"
[ -n "${KIT_URL:-}" ] || { echo "!! KIT_URL n'est pas defini dans le template"; exit 1; }

# --- 1. prerequis systeme (avec VERIFICATION, pas de silence)
if ! command -v xvfb-run >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    apt-get update -qq > "$RES/apt_update.log" 2>&1 || { echo "!! apt-get update a echoue (voir $RES/apt_update.log)"; exit 1; }
    apt-get install -y -qq xvfb curl ca-certificates gnupg > "$RES/apt_prereq.log" 2>&1 \
        || { echo "!! installation des prerequis echouee (voir $RES/apt_prereq.log)"; exit 1; }
fi
for o in xvfb-run curl tar; do
    command -v "$o" >/dev/null 2>&1 || { echo "!! $o manquant apres installation"; exit 1; }
done
echo "  prerequis systeme : ok"

# --- 1b. Wine 11 si absent
if [ ! -x /opt/wine-stable/bin/wine ]; then
    echo "== installation de Wine 11 =="
    dpkg --add-architecture i386
    # PIEGE : l'image de base Vast livre un winehq.sources PERIME qui entre en conflit.
    rm -f /etc/apt/sources.list.d/winehq*.sources /etc/apt/sources.list.d/winehq*.list
    cat > /etc/apt/sources.list.d/winehq.sources <<'EOF'
Types: deb
URIs: https://dl.winehq.org/wine-builds/ubuntu
Suites: noble
Components: main
Trusted: yes
EOF
    apt-get update -qq > "$RES/apt_wine_update.log" 2>&1 || { echo "!! apt-get update (winehq) a echoue"; exit 1; }
    # NB : --install-recommends n'existe PAS pour apt-get install ; on passe par -o.
    apt-get install -y -qq -o APT::Install-Recommends=true winehq-stable > "$RES/apt_wine.log" 2>&1 \
        || { echo "!! installation de winehq-stable echouee (voir $RES/apt_wine.log)"; exit 1; }
else
    echo "  Wine deja present"
fi
if /opt/wine-stable/bin/wine --version > /dev/null 2>&1; then
    echo "  Wine : $(/opt/wine-stable/bin/wine --version)"
else
    echo "!! Wine ne repond pas (voir $RES/apt_wine.log)"; exit 1
fi
export PATH="/opt/wine-stable/bin:${PATH}"

# --- 2. kit MT5 : telechargement VERIFIE, extraction VERIFIEE
kit_casse() { [ ! -f "$MT5/terminal64.exe" ] || [ ! -f "$MT5/MetaEditor64.exe" ]; }
if kit_casse && [ -f "$MT5/terminal64.exe" ]; then
    echo "  kit INCOMPLET detecte (MetaEditor64.exe manquant) : on le refait"
    rm -rf "$MT5"; mkdir -p "$MT5"
fi
if kit_casse; then
    echo "== telecharge le kit : $KIT_URL =="
    for p in part-aa part-ab; do
        curl -fsSL --connect-timeout 20 --max-time 900 --retry 3 \
             -o "/tmp/mt5prog.tgz.$p" "$KIT_URL/mt5prog.tgz.$p" \
            || { echo "!! telechargement de $p echoue"; exit 1; }
        T=$(stat -c %s "/tmp/mt5prog.tgz.$p" 2>/dev/null || echo 0)
        [ "$T" -eq "$TAILLE_PART" ] || { echo "!! $p fait $T o au lieu de $TAILLE_PART : kit tronque"; exit 1; }
    done
    cat /tmp/mt5prog.tgz.part-aa /tmp/mt5prog.tgz.part-ab > /tmp/mt5prog.tgz
    T=$(stat -c %s /tmp/mt5prog.tgz)
    [ "$T" -eq "$TAILLE_TOTALE" ] || { echo "!! kit recolle = $T o au lieu de $TAILLE_TOTALE"; exit 1; }
    tar -tzf /tmp/mt5prog.tgz > /dev/null 2>&1 || { echo "!! archive mt5prog illisible"; exit 1; }
    mkdir -p "$MT5"
    tar -xzf /tmp/mt5prog.tgz -C "$MT5" || { echo "!! extraction mt5prog echouee"; exit 1; }
    kit_casse && { echo "!! kit extrait mais incomplete (terminal64.exe/MetaEditor64.exe)"; exit 1; }
else
    echo "  kit deja en place : $(du -sh "$MT5" | cut -f1)"
fi

# --- 2b. les scripts : verifies INDEPENDAMMENT du programme. Sinon un /workspace/scripts
# efface (ou un premier boot interrompu) ne serait jamais repare, et l'instance
# demarrerait sans file de travail tout en ayant l'air prete.
if [ ! -f "$SCR/burst/travailleur_vast.py" ]; then
    echo "== telecharge les scripts =="
    curl -fsSL --connect-timeout 20 --max-time 300 --retry 3 \
         -o /tmp/scripts_pack.tgz "$KIT_URL/scripts_pack.tgz" \
        || { echo "!! telechargement de scripts_pack echoue"; exit 1; }
    tar -tzf /tmp/scripts_pack.tgz > /dev/null 2>&1 || { echo "!! archive scripts_pack illisible"; exit 1; }
    # PIEGE (repetition generale du 16/10) : sur une instance NEUVE le dossier n'existe
    # pas encore -- sans ce mkdir, tar echoue et l'instance demarre sans file de travail
    # tout en ayant l'air d'avoir reussi.
    mkdir -p "$SCR"
    tar -xzf /tmp/scripts_pack.tgz -C "$SCR" || { echo "!! extraction scripts_pack echouee"; exit 1; }
    [ -f "$SCR/burst/travailleur_vast.py" ] || { echo "!! travailleur_vast.py absent du kit"; exit 1; }
    echo "  scripts en place"
else
    echo "  scripts deja en place"
fi

# --- 3. prefixe Wine (copie du gabarit si present, sinon creation VERIFIEE)
if [ ! -d "$WINEPREFIX/drive_c" ]; then
    echo "== prefixe Wine =="
    mkdir -p "$WINEPREFIX"
    if [ -d /opt/wpx/drive_c ]; then
        cp -a /opt/wpx/. "$WINEPREFIX/"        # le /. evite d'imbriquer /opt/wpx dans la cible
    else
        timeout 180 xvfb-run -a wineboot -i > "$RES/wineboot.log" 2>&1
        WB=$?                                   # on capture le VRAI code de wineboot
        echo "  code wineboot : $WB"
        [ "$WB" -ne 0 ] && echo "  (wineboot non nul : on verifie quand meme le resultat)"
    fi
    if [ -d "$WINEPREFIX/drive_c" ]; then
        echo "  prefixe pret : $WINEPREFIX"
    else
        echo "!! prefixe Wine INCOMPLET : $WINEPREFIX/drive_c absent (voir $RES/wineboot.log)"
        exit 1
    fi
else
    echo "  prefixe Wine deja pret"
fi

# --- 4. ini de contact. Les identifiants viennent de l'ENVIRONNEMENT du template
# (DEMO_LOGIN / DEMO_PASSWORD) : le depot public n'en contient AUCUN. Sans variables,
# on retombe sur le compte maitre sans mot de passe valide : le terminal demarre,
# l'autorisation echoue, mais la base de symboles se construit quand meme.
# On retire tout retour a la ligne des valeurs : une valeur multiligne pourrait
# injecter une section dans le fichier ini.
NETTOYER() { printf '%s' "${1:-}" | tr -d '\r\n'; }
LOGIN=$(NETTOYER "${DEMO_LOGIN:-798154993}")
MDP=$(NETTOYER "${DEMO_PASSWORD:-x}")
SRV=$(NETTOYER "${DEMO_SERVER:-Dukascopy-demo-mt5-1}")
mkdir -p "$MT5/config"
cat > "$MT5/config/contact.ini" <<EOF
[Common]
Login=$LOGIN
Password=$MDP
Server=$SRV
ProxyEnable=0
NewsEnable=0
MaxBars=5000000
[StartUp]
EOF
[ -n "${DEMO_PASSWORD:-}" ] && echo "  ini ecrit (login $LOGIN, mot de passe fourni)" \
                            || echo "  ini ecrit (login $LOGIN, AUCUN mot de passe : autorisation attendue en echec)"

# --- 5. quarantaine OFF par defaut : l'instance a besoin du COURTIER pour tirer ses
# donnees (c'est tout l'interet de son IP fraiche). Le stub no_broker.so fait echouer
# connect()/getaddrinfo() : il ne doit etre actif que pour le travail HORS LIGNE.
if [ "${NOBROKER:-0}" = "1" ]; then
    if [ -f /usr/local/lib/no_broker.so ]; then
        export LD_PRELOAD=/usr/local/lib/no_broker.so
        echo "  NOBROKER=1 : courtier COUPE (travail hors ligne)"
    else
        echo "  !! NOBROKER=1 mais no_broker.so absent (il n'existe que dans l'image cuite) : courtier NON coupe"
    fi
else
    echo "  courtier autorise (NOBROKER desactive : l'instance doit tirer ses donnees)"
fi

# --- 6. les travailleurs (et PAS le surveillant de la boucle locale)
# Deux mesures du 16/10 en LISANT le code de travailleur_vast.py :
#  a) superviseur_boucle.py est le chien de garde de la boucle LOCALE (version Windows :
#     chemins C:\ et powershell) : le lancer sur une instance n'a aucun sens ;
#  b) le travailleur impose DISPLAY=:99 et --prefixe-wine, et il lance terminal64.exe
#     avec cwd=<datadir> : il faut donc UN X VIRTUEL PERSISTANT et une COPIE du
#     programme dans chaque datadir.
WORKERS="${WORKERS:-0}"
FILE="${QUEUE_DB:-${WORKSPACE:-/workspace}/queue/usine_indus.db}"
# la charge de travail (charge_travail.tgz) se detare dans ${WORKSPACE}/file : on la
# trouve la aussi, sans obliger a definir QUEUE_DB a la main.
[ -f "$FILE" ] || FILE="${WORKSPACE:-/workspace}/file/usine_indus.db"
TRAV="$SCR/burst/travailleur_vast.py"
if [ "$WORKERS" -gt 0 ] 2>/dev/null && [ -f "$TRAV" ] && [ -f "$FILE" ]; then
    if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
        setsid nohup Xvfb :99 -screen 0 1280x1024x24 > "$RES/xvfb99.log" 2>&1 < /dev/null &
        sleep 3
        echo "  Xvfb :99 lance (le travailleur l'exige)"
    fi
    # Les setfiles ne sont PAS dans le kit public (ils portent nos declencheurs) :
    # on les depose a la main dans ${WORKSPACE}/setfiles (ou SETFILES_DIR).
    SETF="${SETFILES_DIR:-${WORKSPACE:-/workspace}/setfiles}"
    CFD="$WINEPREFIX/drive_c/users/root/AppData/Roaming/MetaQuotes/Terminal/Common/Files"
    mkdir -p "$SETF" "$CFD"
    NSET=$(ls -1 "$SETF" 2>/dev/null | wc -l)
    if [ "$NSET" -gt 0 ]; then
        echo "  setfiles : $NSET fichier(s) dans $SETF"
        cp -n "$SETF"/*.txt "$CFD/" 2>/dev/null
    else
        echo "  !! AUCUN setfile dans $SETF : les lots seront refuses (deposer charge_travail.tgz)"
    fi
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
    echo "  deposer la base de la file puis relancer avec WORKERS=<nombre>."
else
    echo "  WORKERS=0 : travailleurs non lances."
    echo "  python3 $TRAV --travailleur 0 --datadir ${WORKSPACE:-/workspace}/W00 --db $FILE --sortie $RES"
fi

echo "== onstart termine $(date -Is) =="

# --- 7. auto-test de premier demarrage : ecrit $RES/etat_pret.txt
# En tache de fond pour ne pas retarder le boot : il lance le terminal maitre, attend
# 150 s, lit le journal et rend un VERDICT lisible (PRET / A CORRIGER).
VERIF="${WORKSPACE:-/workspace}/verifier_pret.sh"
[ -f "$VERIF" ] || VERIF="$SCR/verifier_pret.sh"
if [ ! -f "$VERIF" ]; then
    curl -fsSL --connect-timeout 20 --max-time 120 \
         -o "${WORKSPACE:-/workspace}/verifier_pret.sh" "${KIT_URL%/*}/verifier_pret.sh" 2>/dev/null
    VERIF="${WORKSPACE:-/workspace}/verifier_pret.sh"
fi
if [ -f "$VERIF" ]; then
    setsid nohup bash "$VERIF" >> "$RES/etat_pret.log" 2>&1 < /dev/null &
    echo "auto-test lance : verdict dans $RES/etat_pret.txt (2-3 min)"
else
    echo "!! verifier_pret.sh introuvable : pas d'auto-test"
fi
