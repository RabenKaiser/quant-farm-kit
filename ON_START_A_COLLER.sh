#!/bin/bash
# ON_START_A_COLLER.sh -- a coller TEL QUEL dans le champ "On-start script" d'un
# template Vast. Autonome : aucun champ d'environnement a remplir, rien a taper.
#
# POURQUOI CE FICHIER EXISTE
# Le 16/10, une instance a 255 coeurs a ete louee avec le template PAR DEFAUT : son
# /root/onstart.sh etait le talon vide de Vast ("This file is run on instance start"),
# donc RIEN ne s'est installe -- et la location a tourne a vide. Ce script evite les
# trois pieges de la version "copiee d'un tutoriel" :
#   1. pas de `set -e` : une etape qui echoue est SIGNALEE, elle ne tue pas tout le
#      reste en silence ;
#   2. pas de `apt-get install wine64 wine32` (paquets de la distribution : version
#      ancienne, et `wine32` n'est resolvable que si `universe` est active) -- on
#      installe WineHQ 11.0, LA version avec laquelle MT5 6230 a ete prouve ;
#   3. pas de `curl | bash` : le script est telecharge, sa taille est verifiee, puis
#      execute -- une page d'erreur HTML n'est jamais executee comme du shell.
#
# ATTENTION : ce fichier contient les identifiants de demonstration. Si tu preferes
# les garder hors du template, mets-les plutot dans les variables d'environnement
# DEMO_LOGIN / DEMO_PASSWORD du template et supprime les deux lignes export ci-dessous.

export KIT_URL="https://raw.githubusercontent.com/RabenKaiser/quant-farm-kit/main/kit"
export BASE_URL="https://raw.githubusercontent.com/RabenKaiser/quant-farm-kit/main"
export DEMO_LOGIN="${DEMO_LOGIN:-603100618}"
export DEMO_PASSWORD="${DEMO_PASSWORD:-;AA6p5H*}"
export DEMO_SERVER="${DEMO_SERVER:-Dukascopy-demo-mt5-1}"
export WORKSPACE=/workspace
export WORKERS="${WORKERS:-0}"

echo "=== PREPARATION QUANT FARM : debut $(date -Is) ==="

mkdir -p /workspace
cd /workspace || { echo "!! /workspace inaccessible"; exit 1; }

echo "=== telechargement du script de preparation du depot ==="
curl -fsSL --connect-timeout 20 --max-time 180 -o /tmp/onstart.sh "$BASE_URL/onstart_template.sh" \
    || { echo "!! depot injoignable : verifier KIT_URL/BASE_URL"; exit 1; }
TAILLE=$(stat -c %s /tmp/onstart.sh 2>/dev/null || echo 0)
if [ "$TAILLE" -lt 3000 ]; then
    echo "!! script suspect : $TAILLE octets (page d'erreur au lieu du script ?)"
    exit 1
fi
echo "  script recupere : $TAILLE octets"

echo "=== execution (compter 5 a 6 minutes : WineHQ est la partie longue) ==="
bash /tmp/onstart.sh
RC=$?

echo "=== PREPARATION QUANT FARM : fin $(date -Is) code=$RC ==="
echo "=== VERDICT a lire : cat /workspace/resultats/etat_pret.txt ==="
[ -f /workspace/resultats/etat_pret.txt ] && tail -12 /workspace/resultats/etat_pret.txt
exit $RC
