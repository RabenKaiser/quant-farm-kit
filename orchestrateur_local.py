#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""orchestrateur_local.py -- chef d'orchestre de N creneaux MT5 sur UNE machine puissante.

CIBLE : une seule grosse instance Vast louee qui fait tourner BEAUCOUP de terminaux MT5
en meme temps (l'objectif du proprietaire : "one powerful with 500 MT5"), tous sur le
meme hote, le meme systeme de fichiers, le meme localhost.

CE QUE CE SCRIPT FAIT
  1. il cree/mainttient N CRENEAUX : chaque creneau = un dossier de donnees
     /workspace/W00, W01, ... contenant l'arbre du programme MT5 obtenu par LIENS DURS
     (`cp -al`) : 302 Mo payes UNE fois pour toute la ferme au lieu de 302 Mo par
     creneau (mesure sur cette machine : voir ORCHESTRATEUR_LOCAL.md) ;
  2. il demarre les creneaux ETALES (--stagger, 8 s par defaut), verifie pour chacun
     qu'un processus vit ET qu'un journal s'ecrit, et REESSAIE ceux qui meurent ;
  3. il n'annonce jamais un creneau mort avant --demarrage-max (180 s par defaut) :
     un test coute 26 s a chaud et 45 s a froid, le premier test d'un symbole
     telecharge son historique ;
  4. il affiche un TABLEAU DE SANTE : pid, lot en cours, secondes sur le lot,
     derniere ligne de journal, etat (TRAVAILLE / LENT / BLOQUE / MORT / VIDE),
     plus les totaux de la file (FAIT / ECHEC / PRET) ;
  5. --rampe N monte les creneaux par paquets de N en verifiant l'erreur de port
     entre les paquets, et ARRETE la montee si plus de 20 % d'un paquet meurt :
     on decouvre le vrai plafond de la machine au lieu de tout casser.

LES QUATRE PIEGES MESURES SUR CETTE MACHINE (16/10), TOUS TRAITES ICI
  a. X11 : les travailleurs etaient lances par `xvfb-run -a wine ...`. Deux effets
     pervers mesures : chaque travailleur cree son propre ecran avec son propre cookie
     d'authentification (9 Xvfb orphelins retrouves), et wine herite d'un XAUTHORITY
     qui ne correspond pas -> `Authorization required, but no authorization protocol
     specified` puis le terminal sort en une seconde SANS faire aucun test.
     ICI : UN SEUL Xvfb possede par l'orchestrateur, `-ac` (sans controle d'acces),
     DISPLAY et XAUTHORITY FIXES et exportes pour tous les creneaux. C'est ce
     changement qui a fait passer un test de "jamais" a "successfully finished".
  b. PORT : MT5 ouvre un serveur MCP sur un port LOCAL FIXE (127.0.0.1:22346,
     machine entiere). Le deuxieme terminal qui demarre en meme temps ecrit
     `MCP bind error on 127.0.0.1:22346 [(10048)]`. On ETALE donc les demarrages
     (--stagger) et on relance les creneaux qui meurent au demarrage.
  c. BILAN ECRIT A LA SORTIE : mesure du 16/10 sur cette machine -- le journal de
     l'agent (qui porte `TRD_TOTAL`) est ecrit 29 ms APRES la sortie du terminal.
     Un travailleur qui s'arrete quand le processus meurt perd le lot. Ici le controle
     de sante lit le journal de l'AGENT, pas la vie du processus.
  d. RAM : un test complet (terminal + metatester + agent) a culmine a 275 Mo.
     La machine en a 503 Go : la RAM n'est PAS le plafond. Le plafond, c'est le
     nombre de coeurs (64 vus par MT5) et le disque.

AUCUNE COMMANDE DISTANTE N'EST CONSTRUITE PAR CONCATENATION DE GUILLEMETS.
Un unique script /root/.orch/creneau.sh est depose par scp, puis appele avec des
ARGUMENTS (bash /root/.orch/creneau.sh start 7) : ni guillemets, ni parentheses, ni
heredoc a proteger. C'est ce qui cassait les commandes toute la journee du 16/10.

DEPENDANCE 2 (le levier de parallelisme) : `[Tester] Port`.
L'agent de test local de MT5 ecoute sur un port FIXE (3000 : les journaux disent
"Agent-127.0.0.1-3000"). L'aide MetaQuotes : `[Tester] Port` = "port de l'agent de
test local ; a specifier pour lancer les tests EN PARALLELE sur differents agents".
Sans cela, tous les creneaux se disputent l'agent 3000. Cet orchestrateur pose
`Port=3000+creneau` par emplacement (et `MCP.* Enable=0` dans assistant.ini), MAIS
`travailleur_vast.py` ecrit son propre .ini de campagne et n'inclut ni Port ni le
drapeau --agent-port : tant que ce n'est pas ajoute, le lecteur doit verifier que le
testeur utilise bien un agent par creneau. Voir ORCHESTRATEUR_LOCAL.md.

DEPENDANCE 1 : travailleur_vast.py doit accepter `--slot K --slots N` et ne prendre que
les lots ou `id % N == K`. Verifier : ssh ... python3 <trv> --help | grep -i slot

EXEMPLES
  python orchestrateur_local.py --hote ssh5.vast.ai --port 32012 --creneaux 8 --sante
  python orchestrateur_local.py --creneaux 64 --rampe 8 --surveiller --duree 3600
  python orchestrateur_local.py --creneaux 500 --dry-run
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import posixpath
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# valeurs par defaut
# ---------------------------------------------------------------------------
HOTE_DEFAUT = "ssh5.vast.ai"
PORT_DEFAUT = 32012
CLE_DEFAUT = "~/.ssh/vast_quant"
DOSSIER_DISTANT = "/root/.orch"

_VERROU = threading.Lock()
_T0 = time.time()


def hms() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


def info(msg: str) -> None:
    with _VERROU:
        sys.stdout.write("%s %s\n" % (hms(), msg))
        sys.stdout.flush()


def info2(tag: str, msg: str) -> None:
    with _VERROU:
        sys.stdout.write("%s [%s] %s\n" % (hms(), tag, msg))
        sys.stdout.flush()


def tronquer(texte, limite=150) -> str:
    t = "" if texte is None else str(texte)
    t = t.replace("\r", " ").replace("\n", " | ").strip()
    return t if len(t) <= limite else t[: limite - 3] + "..."


def chemin_ouvert(chemin: str) -> str:
    return os.path.expanduser(chemin) if chemin.startswith("~") else chemin


def montrer(chemin: str) -> str:
    """Chemin lisible : sur Windows, ssh accepte les barres obliques inverses mais
    elles brouillent la lecture d'une commande a copier-coller."""
    return str(chemin).replace("\\", "/")


def entier(v, defaut=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return defaut


def reel(v, defaut=0.0):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return defaut


# ---------------------------------------------------------------------------
# le script distant : UN fichier, depose par scp, appele avec des arguments
# ---------------------------------------------------------------------------
SCRIPT_CRENEAU = r'''#!/bin/bash
# creneau.sh -- gestion d'UN creneau MT5. Depose par orchestrateur_local.py.
# Usage : bash /root/.orch/creneau.sh <action> [args]
# AUCUNE commande de ce fichier ne tue un processus qui n'est pas le sien.
WS=/workspace
RES="$WS/resultats"
TREE_SRC="$WS/MT5"
TRAV="$WS/scripts/burst/travailleur_vast.py"
DB="${QUEUE_DB:-$WS/file/usine_indus.db}"
SETF="$WS/setfiles"
DELAI_MAX=420
PAUSE=20
export TZ=UTC
export WINEPREFIX_TEMPLATE="$WS/wpx"

dossier() { printf "%s/W%02d" "$WS" "$1"; }
config() { printf "%s/Tester/config_%d.ini" "$(dossier "$1")" "$1"; }
rapport() { printf "%s/Tester/raw_farm_%d.txt" "$(dossier "$1")" "$1"; }
journal() { printf "%s/resultats/travailleur_%d.log" "$WS" "$1"; }

# ---------------------------------------------------------------------------
etat_creneau() {
    N="$1"
    D=$(dossier "$N")
    J=$(journal "$N")
    echo "CRENEAU=$N"
    echo "DOSSIER=$D"
    echo "EXISTE=$([ -d "$D" ] && echo OK || echo NON)"
    echo "EXE=$([ -f "$D/terminal64.exe" ] && echo OK || echo MANQUANT)"
    # l'executable est-il PARTAGE (lien dur) ? c'est la mesure de l'economie
    if [ -f "$D/terminal64.exe" ]; then
        echo "NLINK=$(stat -c %h "$D/terminal64.exe" 2>/dev/null || echo 0)"
    fi
    P=""
    for c in "$TRAV --slot $N " "$TRAV --travailleur $N "; do
        P=$(pgrep -f "$c" | head -1)
        [ -n "$P" ] && break
    done
    echo "PID=${P:-0}"
    if [ -n "$P" ]; then
        echo "ETIME=$(ps -o etimes= -p "$P" 2>/dev/null | tr -d " " || echo 0)"
        echo "RSS_KO=$(awk "/VmRSS/ {print \$2}" /proc/$P/status 2>/dev/null || echo 0)"
    fi
    # le terminal de CE creneau (le dossier le distingue des autres)
    T=$(pgrep -f "terminal64.exe" | head -1)
    if [ -n "$T" ]; then
        C=$(tr "\000" " " < /proc/$T/cmdline 2>/dev/null)
        case "$C" in *"/W$N/"*|*"/W0$N/"*) ;; *) T="" ;; esac
    fi
    echo "PID_TERM=${T:-0}"
    # journal du TERMINAL de ce creneau (le plus recent, prive par emplacement)
    L=$(ls -t "$D/logs/"*.log 2>/dev/null | head -1)
    if [ -n "$L" ]; then
        echo "MT5LOG=$(basename "$L")"
        echo "MT5LOG_MTIME=$(stat -c %Y "$L" 2>/dev/null || echo 0)"
        echo "MT5LOG_TAILLE=$(stat -c %s "$L" 2>/dev/null || echo 0)"
        echo "MT5_BEGIN"
        tr -d "\000" < "$L" | tail -6
        echo "MT5_END"
        echo "BINDERR=$(tr -d "\000" < "$L" | grep -c "MCP.*bind error" 2>/dev/null || echo 0)"
        echo "MCPOK=$(tr -d "\000" < "$L" | grep -c "MCP.*started" 2>/dev/null || echo 0)"
        echo "TEST_FINI=$(tr -d "\000" < "$L" | grep -ac "last test passed" 2>/dev/null || echo 0)"
        echo "TEST_LANCE=$(tr -d "\000" < "$L" | grep -ac "automatic testing started" 2>/dev/null || echo 0)"
    fi
    # LE BILAN EST DANS LE JOURNAL DE L'AGENT, PAS DANS CELUI DU TERMINAL.
    # Mesure du 16/10 : compter TRD_TOTAL dans le journal du terminal donne 0 et fait
    # croire a un echec alors que le test a rendu son bilan.
    A=$(ls -t "$D"/Tester/Agent-*/logs/*.log 2>/dev/null | head -1)
    if [ -n "$A" ]; then
        echo "AGENT=$(echo "$A" | sed "s|$D/||")"
        echo "AGENT_MTIME=$(stat -c %Y "$A" 2>/dev/null || echo 0)"
        echo "AGENT_PORT_VU=$(tr -d "\000" < "$A" | grep -aoE "started on 127.0.0.1:[0-9]+" | tail -1 | cut -d: -f2)"
        echo "BILANS=$(tr -d "\000" < "$A" | grep -ac TRD_TOTAL 2>/dev/null || echo 0)"
        echo "TRD=$(tr -d "\000" < "$A" | grep -a "TRD_TOTAL" | tail -1 | cut -c1-140)"
        echo "DEALS=$(tr -d "\000" < "$A" | grep -ac "^..\s*0\s.*DEAL " 2>/dev/null || echo 0)"
    fi
    # reglages de parallelisme : port d'agent et etat du MCP
    if [ -f "$D/config/agent_port.txt" ]; then
        echo "PORT_AGENT=$(cat "$D/config/agent_port.txt" 2>/dev/null)"
    fi
    echo "ASSISTANT=$([ -f "$D/config/assistant.ini" ] && echo OK || echo ABSENT)"
    echo "COMMON_PORT=$(grep -h "^Port=" "$D/config/common.ini" 2>/dev/null | tail -1 | cut -d= -f2)"
    # l'agent de test est-il en ecoute sur SON port ?
    A_LIGNE=$(ls -t "$D"/Tester/Agent-*/logs/*.log 2>/dev/null | head -1)
    if [ -n "$A_LIGNE" ]; then
        echo "AGENT_PORT_VU=$(tr -d "\000" < "$A_LIGNE" | grep -aoE "started on 127.0.0.1:[0-9]+" | tail -1 | cut -d: -f2)"
    fi
    echo "AUTRES_CRENEAUX=$(pgrep -cf "travailleur_vast.py" || echo 0)"
    # battement de coeur du travailleur
    H="$RES/etat_W$N.json"
    if [ -f "$H" ]; then
        echo "HB_MTIME=$(stat -c %Y "$H" 2>/dev/null || echo 0)"
        echo "HB=$(head -c 3000 "$H" 2>/dev/null | tr "\n" " ")"
    fi
    # journal du travailleur
    if [ -f "$J" ]; then
        echo "J_MTIME=$(stat -c %Y "$J" 2>/dev/null || echo 0)"
        echo "J_BEGIN"
        tail -c 2500 "$J" 2>/dev/null | tail -18
        echo "J_END"
    fi
    # journal MT5 du creneau (le plus recent, PRIVE par emplacement)
    L=$(ls -t "$D/logs/"*.log 2>/dev/null | head -1)
    if [ -n "$L" ]; then
        echo "MT5LOG=$(basename "$L")"
        echo "MT5LOG_MTIME=$(stat -c %Y "$L" 2>/dev/null || echo 0)"
        echo "MT5LOG_TAILLE=$(stat -c %s "$L" 2>/dev/null || echo 0)"
        echo "MT5_BEGIN"
        tr -d "\000" < "$L" | tail -6
        echo "MT5_END"
        echo "BINDERR=$(tr -d "\000" < "$L" | grep -c "MCP.*bind error" 2>/dev/null || echo 0)"
        echo "MCPOK=$(tr -d "\000" < "$L" | grep -c "MCP.*started" 2>/dev/null || echo 0)"
        echo "TEST_LANCE=$(tr -d "\000" < "$L" | grep -ac "automatic testing started" 2>/dev/null || echo 0)"
        echo "TEST_FINI=$(tr -d "\000" < "$L" | grep -ac "last test passed" 2>/dev/null || echo 0)"
    fi
    # LE BILAN EST DANS LE JOURNAL DE L'AGENT, PAS DANS CELUI DU TERMINAL.
    # Mesure du 16/10 : compter TRD_TOTAL dans le journal du TERMINAL rend 0 et fait
    # croire a un echec alors que le test a bien rendu son bilan (erreur commise dans
    # mon propre harnais avant de la corriger ici).
    A=$(ls -t "$D"/Tester/Agent-*/logs/*.log 2>/dev/null | head -1)
    if [ -n "$A" ]; then
        echo "AGENT=$(echo "$A" | sed "s|$D/||")"
        echo "AGENT_MTIME=$(stat -c %Y "$A" 2>/dev/null || echo 0)"
        echo "AGENT_PORT_VU=$(tr -d "\000" < "$A" | grep -aoE "started on 127.0.0.1:[0-9]+" | tail -1 | cut -d: -f2)"
        echo "BILANS=$(tr -d "\000" < "$A" | grep -ac TRD_TOTAL 2>/dev/null || echo 0)"
        echo "TRD=$(tr -d "\000" < "$A" | grep -a "TRD_TOTAL" | tail -1 | cut -c1-140)"
        echo "DEALS=$(tr -d "\000" < "$A" | grep -ac "^..\s*0\s.*DEAL " 2>/dev/null || echo 0)"
    fi
    echo "FIN_ETAT=OK"
}

# ---------------------------------------------------------------------------
creer_creneau() {
    N="$1"
    D=$(dossier "$N")
    if [ -f "$D/terminal64.exe" ]; then
        echo "CREATION=DEJA"
    else
        mkdir -p "$D"
        # LIENS DURS : 302 Mo payes une fois pour toute la ferme (mesure : 309744 Ko
        # pour une vraie copie contre 92 Ko pour cp -al, soit 302 Mo d'economie).
        # Les binaires restent PARTAGES meme apres un test complet (verifie par
        # inode + md5) : MT5 n'ecrit jamais dans terminal64.exe.
        cp -al "$TREE_SRC/." "$D/" 2>/dev/null || cp -a "$TREE_SRC/." "$D/"
        echo "CREATION=OK"
    fi
    mkdir -p "$D/config" "$D/MQL5/Experiments" "$RES/jobs"
    # PIEGE MESURE : `cp -al` sur l'arbre ENTIER partage aussi logs/ et Tester/.
    # Deux creneaux ecrivent alors dans LE MEME journal et le diagnostic devient
    # illisible (constate le 16/10 : W0 et W1 affichaient mot pour mot le meme
    # journal, c'etait le fichier de W99). Ces dossiers doivent etre PRIVES.
    for sous in logs Tester; do
        S="$D/$sous"
        if [ -d "$S" ] && [ "$(find "$S" -type f -links +1 2>/dev/null | wc -l)" -gt 0 ]; then
            mkdir -p "$S.neuf"
            cp -a --remove-destination "$S"/. "$S.neuf"/ 2>/dev/null
            rm -rf "$S"
            mv "$S.neuf" "$S"
            find "$S" -type f -exec rm -f {} + 2>/dev/null
            echo "PRIVE=$sous"
        fi
    done
    mkdir -p "$D/logs" "$D/Tester"
    # MCP : MT5 (build 5955+) ouvre un serveur MCP sur un port UNIQUE PAR MACHINE
    # (127.0.0.1:22346). Mesure du 16/10 : 'MCP started' quand le port est libre,
    # 'MCP bind error [10048]' quand un autre terminal le tient -- et l'erreur n'est
    # PAS fatale (un terminal qui l'affiche a quand meme fait un test complet).
    # On le desactive proprement par emplacement, dans les DEUX dossiers possibles
    # (le programme et le prefixe Wine).
    for C in "$D/config" "$D/.wine/drive_c/users/root/AppData/Roaming/MetaQuotes/Terminal/Common/Files"; do
        mkdir -p "$C" 2>/dev/null
        printf "[MCP.MetaEditor]\r\nEnable=0\r\nEndpoint=http://127.0.0.1:22345/mcp\r\n[MCP.MetaTrader]\r\nEnable=0\r\nEndpoint=http://127.0.0.1:22346/mcp\r\n[MCP.Custom]\r\n" > "$C/assistant.ini" 2>/dev/null
    done
    echo "MCP=DESACTIVE"
    # L'AUTRE PORT, LE VRAI PLAFOND : l'agent de test local ecoute sur un port FIXE
    # (3000, vu dans les journaux : "Agent-127.0.0.1-3000"). L'aide MetaQuotes est
    # explicite : `[Tester] Port` = "port de l'agent de test local ; a specifier pour
    # lancer les tests EN PARALLELE sur differents agents". On pose donc un port
    # D'AGENT DISTINCT par emplacement : 3000 + numero de creneau.
    PORT_AGENT=$((3000 + N))
    echo "PORT_AGENT=$PORT_AGENT"
    printf "Port=%d\r\n" "$PORT_AGENT" >> "$D/config/common.ini" 2>/dev/null
    if [ ! -f "$D/config/agent_port.txt" ]; then
        printf "%d\n" "$PORT_AGENT" > "$D/config/agent_port.txt"
    fi
    # l'EA : sans lui chaque lot tourne sans expert et ne produit RIEN
    EA="$WS/scripts/burst/paquet/MQL5/Experts/eepeek"
    if [ -f "$EA/TRIGGER_LAB.ex5" ]; then
        mkdir -p "$D/MQL5/Experts/eepeek"
        cp -f "$EA/TRIGGER_LAB.ex5" "$EA/TRIGGER_LAB.mq5" "$D/MQL5/Experts/eepeek/" 2>/dev/null
        echo "EA=OK"
    else
        echo "EA=ABSENT"
    fi
    # les setfiles : copies REELLES (petits) -- MT5 les relit a chaque test, on ne
    # veut pas d'un fichier partage qui pourrait etre reecrit par un autre creneau
    if [ -d "$SETF" ]; then
        mkdir -p "$D/MQL5/Files"
        cp -f "$SETF"/*.txt "$D/MQL5/Files/" 2>/dev/null
        echo "SETFILES=$(ls -1 "$D/MQL5/Files" 2>/dev/null | wc -l)"
    fi
    # prefixe Wine PROPRE au creneau : le registre Wine n'est pas concu pour 500
    # ecrivains simultanes. On le fabrique par liens durs depuis le prefixe modele
    # (les milliers de petites DLL/fonts sont alors payees une fois).
    P="$D/.wine"
    if [ ! -d "$P/drive_c" ]; then
        MODEL="$WS/wpx"
        if [ -d "$MODEL/drive_c" ]; then
            cp -al "$MODEL" "$P" 2>/dev/null || cp -a "$MODEL" "$P" 2>/dev/null
        fi
        mkdir -p "$P/drive_c/users/root/AppData/Roaming/MetaQuotes/Terminal/Common/Files"
        cp -f "$SETF"/*.txt "$P/drive_c/users/root/AppData/Roaming/MetaQuotes/Terminal/Common/Files/" 2>/dev/null
        echo "PREFIXE=OK"
    else
        echo "PREFIXE=DEJA"
    fi
    echo "FIN_CREATION=OK"
}

# ---------------------------------------------------------------------------
demarrer() {
    N="$1"
    D=$(dossier "$N")
    J=$(journal "$N")
    mkdir -p "$RES"
    if [ ! -f "$D/terminal64.exe" ]; then echo "DEMARRAGE=PAS_DE_CRENEAU"; exit 0; fi
    # deja en route ? on ne double JAMAIS un travailleur
    if pgrep -f "$TRAV --slot $N " >/dev/null 2>&1 || pgrep -f "$TRAV --travailleur $N " >/dev/null 2>&1; then
        echo "DEMARRAGE=DEJA"
        echo "PID=$(pgrep -f "$TRAV --slot $N " | head -1)"
        exit 0
    fi
    if [ ! -f "$TRAV" ]; then echo "DEMARRAGE=TRV_ABSENT"; exit 0; fi
    if [ ! -f "$DB" ]; then echo "DEMARRAGE=DB_ABSENT"; exit 0; fi
    # UN SEUL terminal MT5 par machine : la variable est posee pour ce travailleur
    export WORKERS=1
    export WINEPREFIX="$D/.wine"
    export PATH="/opt/wine-stable/bin:$PATH"
    export WINEDLLOVERRIDES="mscoree,mshtml="
    export WINEDEBUG=-all
    export DISPLAY="${ORCH_DISPLAY:-:99}"
    export XAUTHORITY="${ORCH_XAUTHORITY:-/root/.Xauthority}"
    export DEMO_SERVER="${DEMO_SERVER:-Dukascopy-demo-mt5-1}"
    export TZ=UTC
    export SLOTS_TOTAL="${SLOTS_TOTAL:-1}"
    # LE COMPTE, SINON LE TESTEUR NE DEMARRE PAS.
    # Mesure du 16/10 sur W40 : "tester not started because the account is not
    # specified" puis "tester didn't start". Le travailleur lit DEMO_LOGIN /
    # DEMO_PASSWORD dans l'environnement (des valeurs vides donnent un .ini sans
    # Login). Le script de demarrage du template les exportait ; un orchestrateur qui
    # lance le travailleur lui-meme DOIT les fournir, sinon l'emplacement tourne, prend
    # des lots, et ne rend JAMAIS de bilan.
    #
    # ORDRE DE RECHERCHE STRICT -- et pas "le dernier trouve" : mesure du 16/10, un
    # /tmp/onstart.sh perime portait un AUTRE compte (798154993) que /root/onstart.sh
    # (603100618). Un `tail -1` choisissait le mauvais et l'autorisation echouait.
    #   1. l'environnement (ce que l'operateur a passe) ;
    #   2. le compte PROUVE BON : celui de etat_pret.txt, dont le terminal a ete
    #      reellement autorise ("terminal synchronized") ;
    #   3. /root/onstart.sh (le template qui tourne) ;
    #   4. le travailleur lui-meme ;
    #   5. /tmp/onstart.sh en DERNIER recours seulement.
    if [ -z "${DEMO_LOGIN:-}" ]; then
        V=""
        if [ -f "$RES/etat_pret.txt" ]; then
            V=$(grep -ahoE "'[0-9]{6,10}'" "$RES/etat_pret.txt" 2>/dev/null | head -1 | tr -d "'")
        fi
        [ -z "$V" ] && V=$(grep -ahoE "DEMO_LOGIN:-[0-9]+" /root/onstart.sh 2>/dev/null | head -1 | cut -d: -f2-)
        [ -z "$V" ] && V=$(grep -ahoE "DEMO_LOGIN:-[0-9]+" "$TRAV" 2>/dev/null | head -1 | cut -d: -f2-)
        [ -z "$V" ] && V=$(grep -ahoE "DEMO_LOGIN:-[0-9]+" /tmp/onstart.sh 2>/dev/null | head -1 | cut -d: -f2-)
        [ -n "$V" ] && export DEMO_LOGIN="$V"
    fi
    if [ -z "${DEMO_PASSWORD:-}" ]; then
        V=$(grep -ahoE 'DEMO_PASSWORD:-[^}"[:space:]]+' /root/onstart.sh 2>/dev/null | head -1 | cut -d: -f2-)
        [ -z "$V" ] && V=$(grep -ahoE 'DEMO_PASSWORD:-[^}"[:space:]]+' "$TRAV" 2>/dev/null | head -1 | cut -d: -f2-)
        [ -z "$V" ] && V=$(grep -ahoE 'DEMO_PASSWORD:-[^}"[:space:]]+' /tmp/onstart.sh 2>/dev/null | head -1 | cut -d: -f2-)
        [ -n "$V" ] && export DEMO_PASSWORD="$V"
    fi
    if [ -z "${DEMO_LOGIN:-}" ] || [ -z "${DEMO_PASSWORD:-}" ]; then
        echo "COMPTE=ABSENT"
        echo "  !! ni DEMO_LOGIN ni DEMO_PASSWORD : le testeur ne demarrera pas"
        echo "     (message MT5 : 'tester not started because the account is not specified')"
        echo "     passer --login et --motdepasse a l'orchestrateur"
    else
        echo "COMPTE=OK ($(printf "%s" "$DEMO_LOGIN" | cut -c1-3)***)"
    fi
    cd "$WS" || exit 1
    # LE CONTRAT SE VERIFIE : sans --slot/--slots, les passer fait mourir le
    # travailleur (argparse). On ne les ajoute que s'ils existent, et on DIT ce que
    # cela implique -- sinon le travailleur prendrait tous les lots de la ferme.
    AIDE=$(python3 "$TRAV" --help 2>&1)
    ARGS_SLOT=""
    if printf "%s" "$AIDE" | grep -q -- "--slot" && printf "%s" "$AIDE" | grep -q -- "--slots"; then
        ARGS_SLOT="--slot $N --slots $SLOTS_TOTAL"
        echo "CONTRAT_SLOT=OK"
    else
        echo "CONTRAT_SLOT=ABSENT"
        echo "  !! travailleur_vast.py ne connait pas --slot/--slots : il prendra les lots"
        echo "     de TOUTE la ferme. Partition ajoutee cote travailleur requise."
    fi
    setsid nohup python3 "$TRAV" \
        $ARGS_SLOT \
        --travailleur "$N" \
        --datadir "$D" \
        --prefixe-wine "$D/.wine" \
        --db "$DB" \
        --sortie "$RES" \
        --setfiles "$SETF" \
        --delai-max "$DELAI_MAX" \
        --pause "$PAUSE" \
        >> "$J" 2>&1 < /dev/null &
    P=$!
    echo "DEMARRAGE=OK"
    echo "PID=$P"
    echo "JOURNAL=$J"
}

# ---------------------------------------------------------------------------
arreter() {
    N="$1"
    D=$(dossier "$N")
    # on n'arrete QUE le travailleur de ce creneau : aucune autre tache n'est touchee
    P=$(pgrep -f "$TRAV --slot $N " | head -1)
    [ -z "$P" ] && P=$(pgrep -f "$TRAV --travailleur $N " | head -1)
    if [ -z "$P" ]; then echo "ARRET=DEJA_ARRETE"; exit 0; fi
    kill -TERM "$P" 2>/dev/null
    for i in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        kill -0 "$P" 2>/dev/null || break
    done
    kill -0 "$P" 2>/dev/null && kill -KILL "$P" 2>/dev/null
    echo "ARRET=OK"
    echo "PID=$P"
    # menage du terminal de CE creneau seulement (le processus porte le nom du dossier)
    for t in $(pgrep -f "terminal64.exe"); do
        C=$(tr "\000" " " < /proc/$t/cmdline 2>/dev/null)
        case "$C" in *"$D"*|*"W0$N/"*|*"W$N/"*) kill -TERM "$t" 2>/dev/null; echo "TERMINAL_ARRETE=$t";; esac
    done
}

# ---------------------------------------------------------------------------
demarrer_ecran() {
    # UN ECRAN X PAR EMPLACEMENT.
    #
    # Pourquoi pas UN SEUL ecran pour toute la ferme ? Parce que la mesure du 16/10 a
    # montre que c'est un point de defaillance collectif : un harnais concurrent a
    # lance `pkill -9 -f Xvfb` et TOUS les emplacements sont morts d'un coup (deux
    # emplacements ont rendu zero bilan pour cette seule raison). Avec un ecran par
    # emplacement, la mort d'un ecran ne coute qu'un emplacement, et l'orchestrateur
    # le voit et le relance.
    #
    # C'est aussi le correctif du piege initial : `xvfb-run -a` donnait a wine un
    # XAUTHORITY qui ne correspondait pas a l'ecran -> "Authorization required, but no
    # authorization protocol specified" -> le terminal sortait en une seconde.
    N="$1"
    # Le numero d'ecran est calcule EN PYTHON et passe en variable (ORCH_DISPLAY est
    # le nom COMPLET, ex. ":140"). Aucune arithmetique bash ici : mesure du 16/10,
    # `${ORCH_DISPLAY_BASE:-:99}$((100+N))` produisait ":99140" -- soit une dizaine
    # de caractere colles au lieu d'une addition, et l'ecran ne repondait jamais.
    D="${ORCH_DISPLAY:?ORCH_DISPLAY requis, ex. :140}"
    NUM="${D#:}"          # 140 : sert a nommer le socket /tmp/.X11-unix/X140
    if pgrep -f "Xvfb $D " >/dev/null 2>&1; then
        echo "ECRAN=DEJA"
    else
        mkdir -p /tmp/.X11-unix
        # on repare un ecran mort laisse par un essai precedent (meme nom, processus
        # absent), sinon le suivant refuse ce numero pour toujours
        rm -f "/tmp/.X11-unix/X$NUM" 2>/dev/null
        setsid nohup Xvfb "$D" -screen 0 1280x1024x24 -ac -nolisten tcp \
            > /dev/null 2>&1 < /dev/null &
        # ON ATTEND LE SOCKET, on ne suppose pas qu'il est pret : un `sleep 2` fixe
        # faisait echouer la verification alors que Xvfb a simplement besoin de plus
        # de temps (mesure : socket pret en 2 a 3 s, 20 s sous charge).
        i=0
        while [ "$i" -lt 30 ]; do
            [ -S "/tmp/.X11-unix/X$NUM" ] && break
            sleep 1
            i=$((i+1))
        done
        echo "ECRAN=OK apres ${i}s"
    fi
    echo "DISPLAY=$D"
    echo "XVFB_PID=$(pgrep -f "Xvfb $D " | head -1)"
    # l'ecran repond-il vraiment ? on le verifie, on ne le suppose pas
    if [ -S "/tmp/.X11-unix/X$NUM" ]; then
        echo "SOCKET=OK"
    else
        echo "SOCKET=ABSENT"
    fi
}

# ---------------------------------------------------------------------------
budget() {
    # ce que la machine peut vraiment prendre : c'est ce chiffre qui plafonne la ferme
    echo "NPROC=$(nproc 2>/dev/null || echo 0)"
    echo "MEM_TOTAL_MO=$(awk "/MemTotal/ {print int(\$2/1024)}" /proc/meminfo 2>/dev/null || echo 0)"
    echo "MEM_DISPO_MO=$(awk "/MemAvailable/ {print int(\$2/1024)}" /proc/meminfo 2>/dev/null || echo 0)"
    df -Pm /workspace 2>/dev/null | awk "NR==2 {print \"DISQUE_LIBRE_MO=\" \$4; print \"DISQUE_TOTAL_MO=\" \$2}"
    echo "TERMINAUX=$(pgrep -cf terminal64.exe || echo 0)"
    echo "TRAVAILLEURS=$(pgrep -cf travailleur_vast.py || echo 0)"
    echo "XVFB=$(pgrep -cf Xvfb || echo 0)"
    echo "BINDS_ERR=$(grep -rlc "MCP.*bind error" /workspace/W*/logs/*.log 2>/dev/null | wc -l)"
    echo "BINDS_OK=$(grep -rlc "MCP.*started" /workspace/W*/logs/*.log 2>/dev/null | wc -l)"
    # taille REELLE d'un creneau : combien de blocs il paie vraiment
    if [ -d /workspace/W00 ]; then
        echo "LIENS_W00=$(find /workspace/W00 -type f -links +1 2>/dev/null | wc -l)"
        echo "FICHIERS_W00=$(find /workspace/W00 -type f 2>/dev/null | wc -l)"
        echo "APPARENT_W00_MO=$(du -sm --apparent-size /workspace/W00 2>/dev/null | cut -f1)"
    fi
    echo "FIN_BUDGET=OK"
}

# ---------------------------------------------------------------------------
capacites() {
    # LE CONTRAT DOIT ETRE VERIFIE, PAS SUPPOSE.
    # Mesure du 16/10 : passer `--slot K --slots N` a un travailleur qui ne connait pas
    # ces options le fait MOURIR a l'instant (argparse : "unrecognized arguments").
    # On interroge donc l'aide du travailleur AVANT de construire la ligne de commande.
    echo "TRV_PRESENT=$([ -f "$TRAV" ] && echo OK || echo ABSENT)"
    if [ -f "$TRAV" ]; then
        AIDE=$(python3 "$TRAV" --help 2>&1)
        echo "AIDE_SLOT=$(printf "%s" "$AIDE" | grep -c -- "--slot" || echo 0)"
        echo "AIDE_SLOTS=$(printf "%s" "$AIDE" | grep -c -- "--slots" || echo 0)"
        echo "AIDE_TRAV=$(printf "%s" "$AIDE" | grep -c -- "--travailleur" || echo 0)"
        VL=$(python3 -c "import re,sys; m=re.search(r'--delai-max[ =]*([0-9]+)', sys.argv[1]); print(m.group(1) if m else '?')" "$AIDE" 2>/dev/null || echo "?")
        echo "DEF_DELAI_MAX=$VL"
    fi
    echo "OPT_SLOT=0"
    echo "FIN_CAPACITES=OK"
}

case "${1:-}" in
    ecran)   demarrer_ecran "${2:?numero de creneau requis}" ;;
    budget)  budget ;;
    creer)   creer_creneau "${2:?}" ;;
    demarrer) demarrer "${2:?}" ;;
    arreter) arreter "${2:?}" ;;
    etat)    etat_creneau "${2:?}" ;;
    *) echo "action inconnue : ${1:-}"; exit 2 ;;
esac
'''


# ---------------------------------------------------------------------------
# acces SSH (meme discipline que le reste du dossier : script + arguments)
# ---------------------------------------------------------------------------
class Hote(object):
    """La machine unique. Toutes les commandes passent par le script depose."""

    def __init__(self, hote, port, user, cle, dossier, dry_run, timeout_ssh=45):
        self.hote = hote
        self.port = int(port)
        self.user = user
        self.cle = chemin_ouvert(cle)
        self.dossier = dossier
        self.dry_run = dry_run
        self.timeout_ssh = timeout_ssh
        self.script = posixpath.join(dossier, "creneau.sh")
        self.depose = False

    def base_ssh(self):
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=" + os.devnull,
                "-i", self.cle, "-p", str(self.port),
                "%s@%s" % (self.user, self.hote)]

    def _run(self, args, entree=None, timeout=None):
        if self.dry_run:
            entete = " ".join(args)
            corps = ""
            if entree:
                corps = "\n".join("  | " + l for l in entree.splitlines()[:60])
            return 0, "# SIMULATION : %s\n%s" % (entete, corps)
        try:
            p = subprocess.run(args, input=entree, capture_output=True, text=True,
                               timeout=timeout or self.timeout_ssh,
                               encoding="utf-8", errors="replace")
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "DELAI DEPASSE (%ss)" % (timeout or self.timeout_ssh)
        except FileNotFoundError:
            return 127, "%s introuvable dans le PATH" % args[0]
        except Exception as e:                                   # noqa: BLE001
            return 1, "erreur %s : %s" % (args[0], e)

    def ssh(self, commande, timeout=None):
        return self._run(self.base_ssh() + [commande], timeout=timeout)

    def ssh_script(self, code, timeout=None):
        """Envoie le script sur l'entree standard : rien a echapper."""
        return self._run(self.base_ssh() + ["bash -s"], entree=code, timeout=timeout)

    def scp_vers(self, source, dest, timeout=300):
        args = ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=" + os.devnull,
                "-i", self.cle, "-P", str(self.port), source,
                "%s@%s:%s" % (self.user, self.hote, dest)]
        if self.dry_run:
            return 0, "# SIMULATION : scp -P %d %s -> %s" % (self.port, source, dest)
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace")
            return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
        except subprocess.TimeoutExpired:
            return 124, "scp DELAI DEPASSE (%ss)" % timeout
        except Exception as e:                                   # noqa: BLE001
            return 1, "erreur scp : %s" % e

    def installer(self):
        """Depose le script UNE fois. Ensuite : bash <script> <action> <args>."""
        if self.depose:
            return True
        if self.dry_run:
            info("SIMULATION -- depot du script distant :")
            info("  ssh -i %s -p %d %s@%s 'mkdir -p %s'"
                 % (montrer(self.cle), self.port, self.user, self.hote, self.dossier))
            info("  scp -> %s  (%d lignes)" % (self.script,
                 len(SCRIPT_CRENEAU.splitlines())))
            info("  + la version locale de flotte.py / travailleur si besoin")
            self.depose = True
            return True
        rc, sortie = self.ssh("mkdir -p %s" % self.dossier)
        if rc != 0:
            info("!! impossible de creer %s : %s" % (self.dossier, tronquer(sortie)))
            return False
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), "creneau.sh")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(SCRIPT_CRENEAU)
        rc, sortie = self.scp_vers(tmp, self.script)
        try:
            os.remove(tmp)
        except OSError:
            pass
        if rc != 0:
            info("!! scp du script a echoue : %s" % tronquer(sortie))
            return False
        self.ssh("chmod +x %s" % self.script)
        self.depose = True
        return True

    def action(self, *args, timeout=None, env=None):
        """bash <script> <action> <args...> -- arguments separes, aucun guillemet.

        `env` pose des variables AVANT la commande (ex. ORCH_DISPLAY par emplacement) :
        la forme `VAR=valeur commande` est comprise par bash et n'introduit aucun
        guillemet a proteger.
        """
        if not self.installer():
            return None
        prefixe = " ".join("%s=%s" % (k, v) for k, v in (env or {}).items())
        morceaux = ["bash", self.script] + [str(a) for a in args]
        if prefixe:
            morceaux = ["env"] + ["%s=%s" % (k, v) for k, v in (env or {}).items()] + morceaux
        commande = " ".join(morceaux)
        rc, sortie = self.ssh(commande, timeout=timeout)
        if self.dry_run:
            return {"_brut": sortie, "_simule": "1"}
        if rc == 124:
            return {"_erreur": "delai depasse", "_brut": sortie}
        return extraire_cles(sortie)


# Mode d'ecran X : ":99" = UN SEUL ecran pour tous les emplacements (mode MESURE le
# 16/10 : 4 emplacements, 4 tests aboutis), None = un ecran par emplacement (:200+n),
# isolant mais jamais mesure a plusieurs. Fixe par main() depuis --ecran-partage.
ECRAN_PARTAGE = ":99"


def ecran_de(slot):
    """Ecran X d'un emplacement.

    DEUX MODES, et il faut MESURER sur la machine plutot que trancher sur une note :
      - mode PARTAGE (defaut) : un seul `Xvfb :99` pour tous les emplacements. C'est le
        mode qui a rendu 4 tests sur 4 le 16/10, apres que tous les essais a serveur X
        par terminal aient plafonne a 1 ou 2 sur 4 (chacun des essais utilisait
        `xvfb-run -a`, qui choisit un numero de display et pose un cookie XAUTHORITY).
      - mode PAR EMPLACEMENT (`--ecran-par-slot`) : un `Xvfb :200+n` par emplacement,
        sans controle d'acces (`-ac`) et avec verification du socket. Isolant : la mort
        d'un ecran ne coute qu'un emplacement. C'est le mode que `demarrer_ecran` a ete
        ecrit pour, mais il n'a JAMAIS ete mesure a plusieurs emplacements.

    Le mode par emplacement est donc disponible, mais le defaut est le mode MESURE.
    """
    if ECRAN_PARTAGE is not None:
        return ECRAN_PARTAGE
    # Base 200 : la plage 100-113 est prise par les `xvfb-run` orphelins de la journee
    # (leurs fichiers /tmp/.X1xx-lock bloquent le numero, et un Xvfb qui ne peut pas
    # prendre son numero ne cree jamais son socket).
    return ":%d" % (200 + slot)


def extraire_cles(texte):
    """Transforme CLE=valeur en dictionnaire (le contrat de creneau.sh)."""
    d = {"_brut": texte or ""}
    for ligne in (texte or "").splitlines():
        if "=" in ligne and not ligne.startswith("#"):
            cle, val = ligne.split("=", 1)
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", cle):
                d[cle] = val.strip()
    return d


# ---------------------------------------------------------------------------
# etats
# ---------------------------------------------------------------------------
TRAVAILLE, LENT, BLOQUE, MORT, VIDE, INCONNU = (
    "TRAVAILLE", "LENT", "BLOQUE", "MORT", "VIDE", "INCONNU")
ORDRE = (TRAVAILLE, LENT, VIDE, BLOQUE, MORT, INCONNU)


class Creneau(object):
    """Un creneau = un dossier de donnees + (au plus) UN travailleur."""

    __slots__ = ("slot", "etat", "raison", "pid", "pid_term", "rss_mo", "etime",
                 "lot", "secondes_lot", "derniere_ligne", "trd", "bind_err", "mcp_ok",
                 "nlink", "agent", "journal_mtime", "demande_a", "tentatives",
                 "dernier_progres", "port_agent", "agent_port_vu", "assistant",
                 "bilans", "test_lance", "test_fini", "_precedent")

    def __init__(self, slot):
        self.slot = slot
        self.etat = INCONNU
        self.raison = ""
        self.pid = 0
        self.pid_term = 0
        self.rss_mo = 0
        self.etime = 0
        self.lot = "?"
        self.secondes_lot = 0
        self.derniere_ligne = ""
        self.trd = ""
        self.bind_err = 0
        self.mcp_ok = 0
        self.nlink = 0
        self.agent = ""
        self.journal_mtime = 0
        self.demande_a = 0.0
        self.tentatives = 0
        self.dernier_progres = 0.0
        self.port_agent = 0
        self.agent_port_vu = 0
        self.assistant = ""
        self.bilans = 0
        self.test_lance = 0
        self.test_fini = 0
        self._precedent = ""

    @property
    def tag(self):
        return "W%02d" % self.slot


def lire_creneau(hote, slot, slots_total, opt):
    """Une lecture d'etat complete pour UN creneau."""
    return hote.action("etat", slot, timeout=opt.timeout_ssh)


def maj_creneau(c, rep, opt, maintenant):
    """Applique la reponse distante au creneau et le CLASSE."""
    if rep is None or rep.get("_erreur") or (rep.get("FIN_ETAT") != "OK"
                                            and not rep.get("_simule")):
        c.etat = MORT
        c.raison = tronquer(rep.get("_erreur", "pas de reponse") if rep else "pas de reponse", 60)
        return
    if rep.get("_simule"):
        c.etat = INCONNU
        c.raison = "simulation"
        return

    c.pid = entier(rep.get("PID"), 0)
    c.pid_term = entier(rep.get("PID_TERM"), 0)
    c.rss_mo = entier(rep.get("RSS_KO"), 0) // 1024
    c.etime = entier(rep.get("ETIME"), 0)
    c.nlink = entier(rep.get("NLINK"), 0)
    c.agent = rep.get("AGENT", "")
    c.trd = rep.get("TRD", "")
    c.bind_err = entier(rep.get("BINDERR"), 0)
    c.mcp_ok = entier(rep.get("MCPOK"), 0)
    c.port_agent = entier(rep.get("PORT_AGENT"), 0)
    c.agent_port_vu = entier(rep.get("AGENT_PORT_VU"), 0)
    c.assistant = rep.get("ASSISTANT", "")
    c.bilans = entier(rep.get("BILANS"), 0)
    c.test_lance = entier(rep.get("TEST_LANCE"), 0)
    c.test_fini = entier(rep.get("TEST_FINI"), 0)

    corps = rep.get("_brut", "")
    if "J_BEGIN" in corps:
        bloc = corps.split("J_BEGIN", 1)[1].split("J_END", 1)[0]
        lignes = [l.strip() for l in bloc.splitlines() if l.strip()]
        if lignes:
            c.derniere_ligne = tronquer(lignes[-1], 70)
            # le lot en cours : "lot 33 : EURUSD 2021 (M30) keltwalk_up.txt"
            m = None
            for l in reversed(lignes):
                m = re.search(r"lot (\d+) : (\S+) (\S+)", l)
                if m:
                    break
            if m:
                c.lot = "%s %s" % (m.group(1), m.group(2))
            dates = re.findall(r"lot \d+ :", bloc)
            c.secondes_lot = 0
            jm = entier(rep.get("J_MTIME"), 0)
            if jm:
                c.journal_mtime = jm
    if not c.lot or c.lot == "?":
        hb = rep.get("HB", "")
        if hb:
            try:
                d = json.loads(hb)
                if d.get("lot_id") is not None:
                    c.lot = "%s %s" % (d.get("lot_id"), d.get("sym", ""))
                c.secondes_lot = entier(d.get("secondes_lot", 0))
            except ValueError:
                pass

    # --- classification --------------------------------------------------
    # 1. la preuve de vie la plus sure : un processus travailleur
    if c.pid > 0:
        c.secondes_lot = max(c.secondes_lot, entier(rep.get("ETIME"), 0))
        if c.pid_term > 0:
            c.etat = TRAVAILLE
            c.raison = ("port deja pris par un autre creneau (bind err %d)"
                        % c.bind_err) if c.bind_err and not c.mcp_ok else ""
            c.dernier_progres = maintenant
        else:
            # le travailleur vit mais aucun terminal : soit il ecrit ses resultats,
            # soit il est entre deux lots. On ne juge pas avant --demarrage-max.
            if maintenant - c.demande_a < opt.demarrage_max:
                c.etat = LENT
                c.raison = "demarrage : aucun terminal encore"
            else:
                c.etat = LENT
                c.raison = "travailleur vivant, terminal absent"
        return

    # 2. plus de travailleur : queue vide pour son creneau, ou mort ?
    if c.nlink == 0 and not rep.get("EXISTE") == "OK":
        c.etat = MORT
        c.raison = "creneau inexistant (a creer avec --creer)"
        return
    if not c.trd and (maintenant - c.demande_a) > opt.demarrage_max:
        c.etat = MORT
        c.raison = "aucun bilan, travailleur disparu apres %.0f s" % (
            maintenant - c.demande_a)
        return
    c.etat = VIDE
    c.raison = "plus de travailleur : creneau epuise" if c.trd else "travailleur non lance"


# ---------------------------------------------------------------------------
# tableau de sante
# ---------------------------------------------------------------------------
def tableau(creneaux, opt, budget, file_locale):
    compte = {e: 0 for e in ORDRE}
    for c in creneaux:
        compte[c.etat] = compte.get(c.etat, 0) + 1
    L = []
    L.append("=" * 132)
    L.append("ORCHESTRATEUR -- %d creneaux sur %s:%d -- %s"
             % (len(creneaux), opt.hote, opt.port, time.strftime("%Y-%m-%d %H:%M:%S")))
    if budget:
        L.append("  machine : %s coeurs | RAM %s Mo dispo / %s Mo | disque %s Mo libres "
                 "| terminaux %s | travailleurs %s | Xvfb %s"
                 % (budget.get("NPROC", "?"), budget.get("MEM_DISPO_MO", "?"),
                    budget.get("MEM_TOTAL_MO", "?"), budget.get("DISQUE_LIBRE_MO", "?"),
                    budget.get("TERMINAUX", "?"), budget.get("TRAVAILLEURS", "?"),
                    budget.get("XVFB", "?")))
        if budget.get("APPARENT_W00_MO"):
            L.append("  disque par creneau : %s Mo apparents, %s fichiers dont %s "
                     "PARTAGES par liens durs (les binaires ne sont payes qu'une fois)"
                     % (budget.get("APPARENT_W00_MO"), budget.get("FICHIERS_W00", "?"),
                        budget.get("LIENS_W00", "?")))
        if budget.get("BINDS_ERR") or budget.get("BINDS_OK"):
            L.append("  port MCP : %s creneau(x) avec 'MCP started', %s avec "
                     "'MCP bind error' (l'erreur n'est pas fatale, mais elle signale "
                     "des demarrages trop rapproches)"
                     % (budget.get("BINDS_OK", 0), budget.get("BINDS_ERR", 0)))
    L.append("  etats : TRAVAILLE %d | LENT %d | VIDE %d | BLOQUE %d | MORT %d | "
             "INCONNU %d" % (compte[TRAVAILLE], compte[LENT], compte[VIDE],
                             compte[BLOQUE], compte[MORT], compte[INCONNU]))
    if file_locale:
        L.append("  file : FAIT %d | ECHEC %d | EN_COURS %d | PRET (restant) %d | total %d"
                 % (file_locale.get("FAIT", 0), file_locale.get("ECHEC", 0),
                    file_locale.get("EN_COURS", 0), file_locale.get("_restant", 0),
                    file_locale.get("_total", 0)))
    else:
        L.append("  file : absente ou illisible (%s)" % opt.db_local)
    L.append("-" * 132)
    L.append("  %-6s %-9s %-7s %-16s %7s %6s %7s %6s  %s"
             % ("creneau", "etat", "pid", "lot", "s/lot", "RSS Mo", "journal",
                "port_ag", "raison"))
    for c in creneaux:
        age = "-"
        if c.journal_mtime:
            age = "%ds" % int(time.time() - c.journal_mtime)
        L.append("  %-6s %-9s %-7s %-16s %7s %6s %7s %6s  %s"
                 % (c.tag, c.etat, c.pid or c.pid_term or "-",
                    tronquer(c.lot, 16), c.secondes_lot or "-", c.rss_mo or "-",
                    age, c.port_agent or "-",
                    tronquer(c.raison or c.derniere_ligne, 46)))
    L.append("-" * 132)
    ennuis = [c for c in creneaux if c.etat in (BLOQUE, MORT)]
    if ennuis:
        L.append("  a traiter (%d) :" % len(ennuis))
        for c in ennuis[:10]:
            L.append("    %s : %s" % (c.tag, tronquer(c.raison, 100)))
        if len(ennuis) > 10:
            L.append("    ... et %d autre(s)" % (len(ennuis) - 10))
    L.append("=" * 132)
    return "\n".join(L)


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------
def creer_tous(hote, creneaux, opt):
    """Cree les arbres de creneaux en parallele (liens durs)."""
    info("=== CREATION de %d creneau(x) (liens durs : 302 Mo payes une seule fois) ==="
         % len(creneaux))
    debut = time.time()
    faits = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, opt.parallele)) as ex:
        futurs = {ex.submit(hote.action, "creer", c.slot, timeout=opt.timeout_phase): c
                  for c in creneaux}
        for fut in concurrent.futures.as_completed(futurs):
            c = futurs[fut]
            try:
                rep = fut.result() or {}
            except Exception as e:                               # noqa: BLE001
                info2(c.tag, "creation : %s" % e)
                continue
            etat = rep.get("CREATION") or rep.get("_simule") or "?"
            if rep.get("EA") == "ABSENT":
                info2(c.tag, "!! EA absent : les lots ne produiront RIEN")
            if rep.get("PREFIXE") == "OK":
                info2(c.tag, "prefixe Wine propre cree")
            if etat in ("OK", "DEJA", "1"):
                faits += 1
            else:
                info2(c.tag, "creation : %s -- %s" % (etat, tronquer(rep.get("_brut"), 80)))
    info("=== CREATION terminee en %.0f s : %d/%d ===" % (time.time() - debut, faits,
                                                          len(creneaux)))
    return faits


def demarrer_un(hote, c, opt, slots_total):
    """Demarre UN creneau et VERIFIE qu'il tourne vraiment (processus + journal)."""
    # l'ecran de CET emplacement, demarre et verifie AVANT le lancement
    rep_ecran = hote.action("ecran", c.slot, timeout=opt.timeout_ssh + 40,
                            env={"ORCH_DISPLAY": ecran_de(c.slot)})
    if rep_ecran is not None and not rep_ecran.get("_simule"):
        if rep_ecran.get("SOCKET") != "OK":
            c.raison = "ecran X indisponible (display %s)" % ecran_de(c.slot)
            info2(c.tag, "!! %s" % c.raison)
            return False
    # L'ENVIRONNEMENT EST CONSTRUIT CONDITIONNELLEMENT.
    # Poser DEMO_LOGIN="" ne vaut PAS "non defini" : une chaine vide ecrase le repli
    # du script distant, et le compte reste absent -- c'est exactement le mode d'echec
    # "tester not started because the account is not specified". On n'exporte donc que
    # ce qu'on a vraiment.
    env = {"ORCH_DISPLAY": ecran_de(c.slot), "SLOTS_TOTAL": slots_total}
    if opt.login:
        env["DEMO_LOGIN"] = opt.login
    if opt.motdepasse:
        env["DEMO_PASSWORD"] = opt.motdepasse
    if opt.serveur:
        env["DEMO_SERVER"] = opt.serveur
    rep = hote.action("demarrer", c.slot, timeout=opt.timeout_ssh, env=env)
    if rep is None:
        c.etat = MORT
        c.raison = "demarrage : pas de reponse"
        return False
    if rep.get("_simule"):
        return True
    etat = rep.get("DEMARRAGE")
    if etat == "OK":
        info2(c.tag, "demarre (pid %s), verification dans %ss"
             % (rep.get("PID", "?"), opt.verif_demarrage))
        c.tentatives += 1
        c.demande_a = time.time()
        return True
    if etat == "DEJA":
        info2(c.tag, "deja en route (pid %s)" % rep.get("PID", "?"))
        c.demande_a = c.demande_a or time.time()
        return True
    c.raison = "demarrage refuse : %s" % etat
    c.etat = MORT
    info2(c.tag, "!! %s -- %s" % (c.raison, tronquer(rep.get("_brut"), 90)))
    return False


def verifier_demarrage(hote, c, opt):
    """Le creneau a-t-il VRAIMENT demarre ? QUATRE controles, dans cet ordre.

    C'est le controle qui manquait le 16/10 : huit travailleurs lances en tempete,
    huit journaux vides, et la location a tourne a vide. On ne se contente jamais de
    " le processus existe " -- mesure du 16/10 : un lancement en tempete laisse des
    processus vivants qui ne produisent AUCUN bilan (4 lances -> 2 bilans, 8 -> 3).

      1. un travailleur vivant pour CE creneau ;
      2. son journal n'est pas vide, et aucune erreur X11 dedans ;
      3. le journal du TERMINAL de ce creneau existe et grossit ;
      4. le port d'agent annonce est celui du creneau (collision = 3000 partage).
    """
    rep = hote.action("etat", c.slot, timeout=opt.timeout_ssh)
    if rep is None or rep.get("_simule"):
        return None
    pid = entier(rep.get("PID"), 0)
    if pid == 0:
        return "le travailleur n'existe plus"

    corps = rep.get("_brut", "")
    j_taille = 0
    if "J_BEGIN" in corps:
        j_taille = len(corps.split("J_BEGIN", 1)[1].split("J_END", 1)[0].strip())
    if j_taille == 0:
        return "le journal du travailleur est vide"
    lignes = [l.strip() for l in
              corps.split("J_BEGIN", 1)[1].split("J_END", 1)[0].splitlines() if l.strip()]
    ligne = lignes[-1] if lignes else ""
    if "Authorization required" in ligne or "XDG_RUNTIME_DIR" in ligne:
        return ("erreur X11 : %s (le travailleur ne peut pas ouvrir de terminal)"
                % tronquer(ligne, 60))
    if "TRV_ABSENT" in ligne or "DB_ABSENT" in ligne or "PAS_DE_CRENEAU" in ligne:
        return "demarrage refuse : %s" % tronquer(ligne, 60)

    # 3. le terminal de ce creneau a-t-il ouvert SON journal ?
    c.agent = rep.get("AGENT", "")
    c.bilans = entier(rep.get("BILANS"), 0)
    c.test_lance = entier(rep.get("TEST_LANCE"), 0)
    c.test_fini = entier(rep.get("TEST_FINI"), 0)
    c.port_agent = entier(rep.get("PORT_AGENT"), 0)
    c.agent_port_vu = entier(rep.get("AGENT_PORT_VU"), 0)
    if c.test_fini or c.bilans:
        # le creneau a meme deja rendu un bilan : au-dessus de tout soupcon
        c.pid = pid
        c.derniere_ligne = tronquer(ligne, 70)
        return None

    t_taille = entier(rep.get("MT5LOG_TAILLE"), 0)
    if t_taille == 0:
        return "aucun journal de terminal : le terminal n'a pas demarre"
    if c.test_lance == 0:
        # journal present mais aucun test lance : il telecharge peut-etre l'historique
        # (mesure : 5,7 s pour un historique M1), on laisse --demarrage-max trancher
        return None

    # 4. le port d'agent : une collision ici est exactement le mode d'echec du 16/10
    if c.port_agent and c.agent_port_vu and c.agent_port_vu != c.port_agent:
        return ("collision de port d'agent : attribue %d, agent demarre sur %d"
                % (c.port_agent, c.agent_port_vu))
    c.pid = pid
    c.derniere_ligne = tronquer(ligne, 70)
    return None


def demarrer_sequentiel(hote, creneaux, opt):
    """DEMARRAGE UN A LA FOIS, avec verification complete avant le suivant.

    Mesure du 16/10 : un lancement en TEMPETE est ce qu'il faut eviter (2 lances ->
    1 bilan ; 4 -> 2 ; 8 -> 3). On demarre donc un emplacement, on attend que les
    QUATRE controles passent, on laisse `--etablissement` secondes a l'agent pour
    s'installer sur son port, et seulement alors on passe au suivant. Un emplacement
    qui echoue est REESSAYE (--tentatives-max) au lieu d'etre perdu en silence.
    """
    info("=== DEMARRAGE UN A LA FOIS : %d emplacement(s) ===" % len(creneaux))
    info("    verification complete (processus + journal + terminal + port d'agent) "
         "avant chaque suivant")
    vivants, echecs = [], []
    for c in creneaux:
        ok = False
        for essai in range(1, opt.tentatives_max + 1):
            if not demarrer_un(hote, c, opt, opt.creneaux):
                info2(c.tag, "tentative %d/%d : demarrage refuse" % (essai, opt.tentatives_max))
                if not opt.dry_run:
                    time.sleep(opt.etablissement)
                continue
            motif = attendre_etablissement(hote, c, opt)
            if motif is None:
                info2(c.tag, "vivant et verifie : pid %s, agent sur le port %s"
                     % (c.pid or "?", c.agent_port_vu or c.port_agent or "?"))
                vivants.append(c)
                ok = True
                break
            info2(c.tag, "tentative %d/%d : %s" % (essai, opt.tentatives_max, motif))
            # on arrete proprement ce qui a echoue AVANT de reessayer, sinon on
            # empile les morts et on brouille les ports pour le suivant
            hote.action("arreter", c.slot, timeout=opt.timeout_ssh)
            if not opt.dry_run:
                time.sleep(opt.etablissement)
        if not ok:
            c.etat = MORT
            echecs.append(c)
        # LAISSER L'AGENT S'INSTALLER : c'est ce delai qui evite que le suivant lui
        # prenne son port. Chaque lancement stable coute ~65-90 s (mesure : 88 s et
        # 108 s pour un test Model=4 sur un an).
        if not opt.dry_run:
            time.sleep(opt.etablissement)
    info("=== %d/%d emplacement(s) demarres et verifies, %d en echec ==="
         % (len(vivants), len(creneaux), len(echecs)))
    return vivants, echecs


def attendre_etablissement(hote, c, opt):
    """Attend que le creneau soit vraiment etabli, ou rend le motif d'echec.

    Deux phases, parce que les mesures ont montre qu'un terminal met 5,7 s a
    telecharger un historique et 26 a 90 s a rendre un bilan : on ne juge pas trop tot.
    """
    if opt.dry_run:
        return None
    debut = time.time()
    dernier = ""
    while time.time() - debut < opt.demarrage_max:
        motif = verifier_demarrage(hote, c, opt)
        if motif is None and (c.test_lance or c.test_fini or c.bilans):
            return None
        if motif is not None:
            # un echec franc (processus mort, journal vide) se juge tout de suite
            if "n'existe plus" in motif or "vide" in motif or "X11" in motif \
                    or "refuse" in motif:
                return motif
            dernier = motif
        time.sleep(opt.intervalle_verif)
    return dernier or "aucun signe de vie apres %.0f s" % opt.demarrage_max


def rampe(hote, creneaux, opt):
    """--rampe N : montee par PAQUETS, un emplacement a la fois, verification stricte.

    On ne decouvre pas le plafond en lancant tout : on monte par paquets de N, chaque
    emplacement etant verifie (processus + journal + terminal + port d'agent) avant le
    suivant, et on s'arrete des que plus de `--seuil-echec` % d'un paquet meurt. Si un
    paquet casse, on ANNONCE le nombre d'emplacements reellement productifs et le
    budget de la machine : c'est le vrai plafond, mesure et non suppose.
    """
    taille = opt.rampe
    vivants = []
    info("=== RAMPE : paquets de %d, un emplacement a la fois, arret si plus de %.0f%% "
         "d'un paquet meurt ===" % (taille, opt.seuil_echec))
    for debut in range(0, len(creneaux), taille):
        paquet = creneaux[debut:debut + taille]
        info("--- paquet %d : emplacements %s a %s ---"
             % (debut // taille + 1, paquet[0].tag, paquet[-1].tag))
        bons, mauvais = demarrer_sequentiel(hote, paquet, opt)
        taux = 100.0 * len(mauvais) / max(1, len(paquet))
        info("  paquet %d : %d/%d verifies vivants, %.0f%% d'echec"
             % (debut // taille + 1, len(bons), len(paquet), taux))
        vivants.extend(bons)
        if taux > opt.seuil_echec:
            info("!! %.0f%% du paquet a echoue (> %.0f%%) : ARRET DE LA RAMPE apres %s."
                 % (taux, opt.seuil_echec, paquet[-1].tag))
            b = budget(hote, opt)
            if b:
                info("   budget : %s coeurs | RAM %s Mo dispo | disque %s Mo libres"
                     % (b.get("NPROC"), b.get("MEM_DISPO_MO"), b.get("DISQUE_LIBRE_MO")))
            info("   ==> PLAFOND MESURE sur cette machine : %d emplacement(s) productif(s) "
                 "sur %d demandes." % (len(vivants), debut + len(paquet)))
            return len(vivants), False
    b = budget(hote, opt)
    if b:
        info("=== RAMPE complete : %d emplacement(s) vivants sur %d demandes ==="
             % (len(vivants), len(creneaux)))
        info("   budget restant : RAM %s Mo dispo, disque %s Mo libres (un test actif "
             "consomme 600 Mo a 1 Go mesure)" % (b.get("MEM_DISPO_MO"),
                                                 b.get("DISQUE_LIBRE_MO")))
    return len(vivants), True


def budget(hote, opt):
    rep = hote.action("budget", timeout=opt.timeout_ssh)
    if rep is None or rep.get("_simule"):
        return {}
    return rep


def verifier_contrat(hote, opt):
    """Le travailleur connait-il --slot/--slots ? On DEMANDE, on ne suppose pas.

    Mesure du 16/10 : lui passer ces options alors qu'il ne les connait pas le fait
    mourir a l'instant (`unrecognized arguments`), et l'emplacement est perdu en
    silence. Ce controle transforme une dependance invisible en message explicite.
    """
    if hote.dry_run:
        return None
    trv = "/workspace/scripts/burst/travailleur_vast.py"
    rc, sortie = hote.ssh("python3 %s --help 2>&1 | grep -c -- '\\-\\-slot'" % trv)
    if rc == 124:
        info("contrat : impossible d'interroger %s (delai depasse)" % trv)
        return None
    n_slot = entier((sortie or "0").strip().splitlines()[-1] if sortie.strip() else "0", 0)
    if n_slot == 0:
        info("=" * 78)
        info("!! DEPENDANCE NON SATISFAITE : travailleur_vast.py ne connait PAS "
             "--slot/--slots")
        info("   Verifie sur la machine : python3 %s --help | grep slot" % trv)
        info("   Consequence : chaque emplacement prendra les lots de TOUTE la ferme.")
        info("   Avec 1 emplacement c'est correct ; au-dela, le meme lot serait teste")
        info("   N fois. L'orchestrateur NE PASSE PAS ces options (le travailleur")
        info("   mourrait sur `unrecognized arguments`).")
        info("=" * 78)
        return False
    info("contrat verifie : le travailleur accepte --slot/--slots (partition active)")
    return True


def diag_debit(hote, opt):
    """Mesure le VRAI plafond : N creneaux demarres etales, puis on regarde qui survit.

    C'est ce mode qui repond a "combien de MT5 cette machine prend-elle". On mesure
    trois choses que rien d'autre ne donne : combien de terminaux sortent vraiment un
    bilan (TRD_TOTAL), combien echouent sur le port, et le cout RAM par creneau.
    """
    info("=== DIAGNOSTIC DE DEBIT : %d creneau(x) etales de %.0f s ==="
         % (opt.creneaux, opt.etagement))
    liste = [Creneau(i) for i in range(opt.creneau_min, opt.creneau_min + opt.creneaux)]
    crees = creer_tous(hote, liste, opt)
    if crees == 0:
        info("!! aucun creneau cree : arret du diagnostic.")
        return
    for i, c in enumerate(liste):
        demarrer_un(hote, c, opt, opt.creneaux)
        if i + 1 < len(liste):
            time.sleep(opt.etagement)
    info("--- attente de %ss (un test coute 26-45 s, on laisse de la marge) ---"
         % opt.diag_attente)
    debut = time.time()
    while time.time() - debut < opt.diag_attente:
        time.sleep(min(30, opt.diag_attente))
        b = budget(hote, opt)
        if b:
            info("  t=%4.0fs terminaux=%s travailleurs=%s | RAM dispo %s Mo | "
                 "MCP started %s / bind err %s"
                 % (time.time() - debut, b.get("TERMINAUX"), b.get("TRAVAILLEURS"),
                    b.get("MEM_DISPO_MO"), b.get("BINDS_OK"), b.get("BINDS_ERR")))
    lire_tous(hote, liste, opt)
    print(tableau(liste, opt, budget(hote, opt), compter_file(opt.db_local)), flush=True)
    bilans = [c for c in liste if c.trd]
    port_ko = [c for c in liste if c.bind_err and not c.mcp_ok]
    rss = [c.rss_mo for c in liste if c.rss_mo]
    ports = set(c.port_agent for c in liste if c.port_agent)
    vus = [c for c in liste if c.agent_port_vu]
    melanges = [c for c in liste if c.agent_port_vu and c.port_agent
                and c.agent_port_vu != c.port_agent]
    info("=== RESULTAT DU DIAGNOSTIC ===")
    info("  creneaux avec un bilan TRD_TOTAL : %d/%d" % (len(bilans), len(liste)))
    info("  creneaux avec erreur de port MCP : %d/%d" % (len(port_ko), len(liste)))
    info("  ports d'agent attribues : %d distincts sur %d creneaux%s"
         % (len(ports), len(liste),
            "" if len(ports) == len(liste) else "  <== COLLISION : a corriger"))
    if vus:
        info("  agents reellement en ecoute : %d/%d creneaux ont un journal d'agent"
             % (len(vus), len(liste)))
    if melanges:
        info("  !! %d creneau(x) dont l'agent a demarre sur un AUTRE port que le sien : "
             "le travailleur ecrit son .ini sans `Port=` (dependance 2 non satisfaite)"
             % len(melanges))
        for c in melanges[:5]:
            info("     %s : attribue %d, agent vu sur %d"
                 % (c.tag, c.port_agent, c.agent_port_vu))
    if rss:
        info("  RAM par creneau : min %d Mo, max %d Mo, moyenne %d Mo"
             % (min(rss), max(rss), sum(rss) // len(rss)))
    b = budget(hote, opt)
    if b:
        n = entier(b.get("NPROC"), 1) or 1
        dispo = entier(b.get("MEM_DISPO_MO"), 0)
        if rss and dispo:
            par_ram = max(1, dispo // max(1, max(rss)))
            info("  plafond RAM   : ~%d creneaux (%d Mo dispo / %d Mo par creneau)"
                 % (par_ram, dispo, max(rss)))
        info("  plafond COEURS: ~%d creneaux (un test occupe ~1 coeur, la machine en a %d)"
             % (n, n))
        info("  plafond DISQUE: %s Mo libres, un creneau consomme 150-400 Mo de donnees"
             % b.get("DISQUE_LIBRE_MO", "?"))
        info("  ==> le plafond reel est le PLUS PETIT des trois.")


def demarrer_ecrans(hote, creneaux, opt):
    """Un ecran X par emplacement, demarre et VERIFIE avant tout lancement.

    On ne suppose jamais qu'un ecran existe : on verifie le socket. C'est ce controle
    qui manque quand " tout marchait " puis que plus rien ne marche.
    """
    ok = 0
    for c in creneaux:
        rep = hote.action("ecran", c.slot, timeout=opt.timeout_ssh + 40,
                          env={"ORCH_DISPLAY": ecran_de(c.slot)})
        if rep is None:
            info2(c.tag, "!! impossible de demarrer l'ecran %s" % ecran_de(c.slot))
            continue
        if rep.get("_simule"):
            ok += 1
            continue
        if rep.get("SOCKET") == "OK":
            ok += 1
        else:
            info2(c.tag, "!! ecran %s sans socket : le terminal ne s'ouvrira pas"
                 % ecran_de(c.slot))
    info("ecrans X : %d/%d verifies (un par emplacement, sans controle d'acces)"
         % (ok, len(creneaux)))
    return ok


def lire_tous(hote, creneaux, opt):
    """Lecture de TOUS les creneaux en parallele."""
    maintenant = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, opt.parallele)) as ex:
        futurs = {ex.submit(lire_creneau, hote, c.slot, opt.creneaux, opt): c
                  for c in creneaux}
        for fut in concurrent.futures.as_completed(futurs):
            c = futurs[fut]
            try:
                rep = fut.result()
            except Exception as e:                               # noqa: BLE001
                rep = {"_erreur": str(e)}
            maj_creneau(c, rep, opt, maintenant)
    return maintenant


def compter_file(chemin):
    if not chemin or not os.path.isfile(chemin):
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % chemin.replace("?", "%3f"), uri=True,
                               timeout=5)
    except Exception:                                            # noqa: BLE001
        return None
    try:
        d = {str(s): int(n) for s, n in
             conn.execute("SELECT statut, COUNT(*) FROM jobs GROUP BY statut")}
        r = conn.execute("SELECT COUNT(*) FROM jobs WHERE statut='PRET'").fetchone()
        d["_restant"] = int(r[0]) if r else 0
        d["_total"] = sum(v for k, v in d.items() if not k.startswith("_"))
        return d
    except Exception:                                            # noqa: BLE001
        return None
    finally:
        try:
            conn.close()
        except Exception:                                        # noqa: BLE001
            pass


def surveiller(hote, creneaux, opt):
    debut = time.time()
    tour = 0
    while True:
        tour += 1
        file_locale = compter_file(opt.db_local)
        b = budget(hote, opt) if tour % opt.budget_tous_les == 1 else None
        lire_tous(hote, creneaux, opt)
        print(tableau(creneaux, opt, b, file_locale), flush=True)
        if opt.once or (opt.tours and tour >= opt.tours):
            break
        if opt.duree and time.time() - debut >= opt.duree:
            info("--duree %ss atteinte." % opt.duree)
            break
        # relance des creneaux morts, si demande
        if opt.relancer:
            arretes = [c for c in creneaux
                       if c.etat in (MORT, BLOQUE) and c.tentatives < opt.tentatives_max]
            if arretes:
                info("relance de %d creneau(x) (tentative < %d)"
                     % (len(arretes), opt.tentatives_max))
                for c in arretes:
                    demarrer_un(hote, c, opt, opt.creneaux)
                    time.sleep(opt.etagement)
        try:
            time.sleep(opt.intervalle)
        except KeyboardInterrupt:
            info("interrompu (Ctrl-C).")
            break
    return tour


def arreter_tous(hote, creneaux, opt):
    info("=== ARRET de %d creneau(x) (uniquement leurs processus) ===" % len(creneaux))
    for c in creneaux:
        rep = hote.action("arreter", c.slot, timeout=opt.timeout_ssh)
        if rep and not rep.get("_simule"):
            info2(c.tag, "arret : %s" % rep.get("ARRET", "?"))


# ---------------------------------------------------------------------------
# ligne de commande
# ---------------------------------------------------------------------------
def construire_arguments(argv=None):
    p = argparse.ArgumentParser(
        prog="orchestrateur_local.py",
        description="Chef d'orchestre de N creneaux MT5 sur UNE machine puissante "
                    "(un seul Xvfb, un seul terminal par creneau, demarrages etales).",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    g = p.add_argument_group("machine")
    g.add_argument("--hote", default=HOTE_DEFAUT, help="hote SSH (defaut : %s)" % HOTE_DEFAUT)
    g.add_argument("--port", type=int, default=PORT_DEFAUT,
                   help="port SSH (defaut : %d)" % PORT_DEFAUT)
    g.add_argument("--user", default="root", help="utilisateur SSH (defaut : root)")
    g.add_argument("--cle", default=CLE_DEFAUT, help="cle SSH (defaut : %s)" % CLE_DEFAUT)
    g.add_argument("--distant", default=DOSSIER_DISTANT,
                   help="dossier du script distant (defaut : %s)" % DOSSIER_DISTANT)

    g = p.add_argument_group("creneaux")
    g.add_argument("--creneaux", type=int, default=8,
                   help="nombre de creneaux (defaut : 8)")
    g.add_argument("--creneau-min", type=int, default=0,
                   help="premier numero de creneau (defaut : 0) -- utile pour ajouter "
                        "des creneaux a une ferme deja en route")
    g.add_argument("--parallele", type=int, default=16,
                   help="fils pour les lectures d'etat et la creation (defaut : 16)")
    g.add_argument("--etagement", type=float, default=8.0,
                   help="secondes entre deux demarrages (defaut : 8) : MT5 prend un port "
                        "LOCAL fixe, les demarrages en tempete sont ce qu'il faut eviter")
    g.add_argument("--etablissement", type=float, default=30.0,
                   help="secondes de stabilisation apres qu'un emplacement est verifie "
                        "vivant, AVANT de lancer le suivant (defaut : 30). C'est le "
                        "reglage qui evite que le suivant prenne le port de l'agent")
    g.add_argument("--intervalle-verif", type=float, default=10.0,
                   help="intervalle d'interrogation pendant la verification d'un "
                        "demarrage (defaut : 10)")
    g.add_argument("--verif-demarrage", type=float, default=30.0,
                   help="secondes avant de verifier qu'un creneau tourne vraiment "
                        "(defaut : 30)")
    g.add_argument("--demarrage-max", type=float, default=180.0,
                   help="on ne declare JAMAIS un creneau mort avant ce delai (defaut : "
                        "180) : un test coute 26 s a chaud, 45 s a froid")
    g.add_argument("--ecran", default=":99", help="ecran X dedie (defaut : :99)")
    g.add_argument("--ecran-partage", default=":99", metavar=":N",
                   help="UN SEUL ecran X pour tous les emplacements (defaut : :99). C'est le "
                        "mode MESURE le 16/10 : 4 emplacements, 4 tests aboutis.")
    g.add_argument("--ecran-par-slot", action="store_true",
                   help="repasser en mode un ecran X par emplacement (:200+n) : isolant "
                        "(la mort d'un ecran ne coute qu'un emplacement) mais JAMAIS mesure "
                        "a plusieurs emplacements. A tester en comparaison du mode partage.")

    g = p.add_argument_group("actions")
    g.add_argument("--creer", action="store_true",
                   help="cree les arbres de creneaux par liens durs")
    g.add_argument("--demarrer", action="store_true", help="demarre les travailleurs")
    g.add_argument("--sante", action="store_true", help="un tour de tableau de sante")
    g.add_argument("--surveiller", action="store_true", help="boucle de surveillance")
    g.add_argument("--rampe", type=int, default=0, metavar="N",
                   help="montee progressive par paquets de N, avec arret si plus de "
                        "--seuil-echec %% d'un paquet meurt")
    g.add_argument("--seuil-echec", type=float, default=20.0,
                   help="pourcentage d'echec qui arrete la rampe (defaut : 20)")
    g.add_argument("--relancer", action="store_true",
                   help="relance les creneaux MORT/BLOQUE pendant la surveillance")
    g.add_argument("--tentatives-max", type=int, default=3,
                   help="tentatives de relance par creneau (defaut : 3)")
    g.add_argument("--arreter", action="store_true",
                   help="arrete les travailleurs des creneaux (eux seuls)")
    g.add_argument("--budget", action="store_true",
                   help="affiche le budget de la machine et le cout reel d'un creneau")
    g.add_argument("--diag", action="store_true",
                   help="DIAGNOSTIC DE DEBIT : cree, demarre N creneaux etales et "
                        "mesure combien sortent un bilan, combien echouent sur le port, "
                        "et le cout RAM par creneau (le plafond reel de la machine)")
    g.add_argument("--diag-attente", type=int, default=240,
                   help="duree du diagnostic en secondes (defaut : 240)")

    g = p.add_argument_group("compte de demonstration (sinon le testeur ne demarre pas)")
    g.add_argument("--login", default=None,
                   help="DEMO_LOGIN pose AVANT de lancer le travailleur. Sans lui MT5 "
                        "ecrit 'tester not started because the account is not specified' "
                        "et AUCUN bilan n'est jamais rendu. Par defaut : lu dans "
                        "/root/onstart.sh sur la machine")
    g.add_argument("--motdepasse", default=None,
                   help="DEMO_PASSWORD (meme raison). Par defaut : lu sur la machine")
    g.add_argument("--serveur", default=None,
                   help="DEMO_SERVER (defaut : la valeur du template sur la machine)")

    g = p.add_argument_group("delais et cadence")
    g.add_argument("--timeout-ssh", type=int, default=45,
                   help="delai maximal d'une commande ssh (defaut : 45)")
    g.add_argument("--timeout-phase", type=int, default=600,
                   help="delai maximal d'une phase longue (creation) (defaut : 600)")
    g.add_argument("--intervalle", type=int, default=60,
                   help="secondes entre deux tours de surveillance (defaut : 60)")
    g.add_argument("--stall-timeout", type=int, default=600,
                   help="au-dela : BLOQUE (defaut : 600)")
    g.add_argument("--duree", type=int, default=0,
                   help="duree maximale de la surveillance (0 = sans limite)")
    g.add_argument("--tours", type=int, default=0, help="nombre de tours (0 = sans limite)")
    g.add_argument("--once", action="store_true", help="un seul tour")
    g.add_argument("--budget-tous-les", type=int, default=5,
                   help="lire le budget machine tous les N tours (defaut : 5)")
    g.add_argument("--db-local", default=None,
                   help="base LOCALE de la file pour FAIT/ECHEC (defaut : "
                        "research\\burst\\paquet\\file\\usine_indus.db)")
    g.add_argument("--dry-run", action="store_true",
                   help="montre exactement ce qui serait fait et ne touche a RIEN")
    return p


def main(argv=None):
    global ECRAN_PARTAGE
    opt = construire_arguments(argv).parse_args(argv)
    # Mode d'ecran : partage (MESURE le 16/10) par defaut, un par emplacement si demande.
    if getattr(opt, "ecran_par_slot", False):
        ECRAN_PARTAGE = None
    else:
        ECRAN_PARTAGE = getattr(opt, "ecran_partage", ":99") or ":99"
    if opt.db_local is None:
        for c in (r"research\burst\paquet\file\usine_indus.db",
                  r"research\vast\file\usine_indus.db",
                  r"research\flotte_resultats\file\usine_indus.db"):
            if os.path.isfile(c):
                opt.db_local = c
                break
        else:
            opt.db_local = ""

    if not opt.dry_run:
        for b in ("ssh", "scp"):
            if shutil.which(b) is None:
                raise SystemExit("%s introuvable dans le PATH : rien ne peut etre fait." % b)
        if not os.path.isfile(chemin_ouvert(opt.cle)):
            info("ATTENTION : cle SSH introuvable : %s" % chemin_ouvert(opt.cle))

    creneaux = [Creneau(i) for i in range(opt.creneau_min,
                                          opt.creneau_min + opt.creneaux)]
    hote = Hote(opt.hote, opt.port, opt.user, opt.cle, opt.distant, opt.dry_run,
                opt.timeout_ssh)

    info("machine %s@%s:%d | %d creneau(x) %s..%s | %d fil(s) | etagement %.0fs"
         % (opt.user, opt.hote, opt.port, len(creneaux), creneaux[0].tag,
            creneaux[-1].tag, opt.parallele, opt.etagement))
    if opt.dry_run:
        info("MODE SIMULATION (--dry-run) : aucune commande distante, rien n'est cree, "
             "demarre ni arrete.")

    if not any((opt.creer, opt.demarrer, opt.sante, opt.surveiller, opt.rampe,
                opt.arreter, opt.budget, opt.diag)):
        info("rien a faire : ajouter --creer, --demarrer, --sante, --rampe N, "
             "--diag, --surveiller ou --budget (voir --help).")
        opt.sante = True

    if not hote.installer():
        return 1

    if opt.dry_run:
        info("--- commandes qui SERAIENT executees ---")

    # 1. les ecrans X : un par emplacement, D'ABORD et verifies.
    #    C'est le piege qui a coute la journee du 16/10. Note du soir : avec UN SEUL ecran
    #    partage on obtient 4 tests sur 4 (mesure) ; le mode un-ecran-par-emplacement reste
    #    disponible (--ecran-par-slot) mais n'a jamais ete mesure a plusieurs emplacements.
    demarrer_ecrans(hote, creneaux, opt)
    if opt.dry_run:
        if ECRAN_PARTAGE:
            info("  bash %s ecran <creneau>   (UN SEUL ecran partage pour tous : %s -- mode "
                 "MESURE, 4 emplacements = 4 tests)" % (hote.script, ECRAN_PARTAGE))
        else:
            info("  bash %s ecran <creneau>   (un ecran X par emplacement : :200, :201, ...)"
                 % hote.script)

    # 1b. le contrat du travailleur : verifie AVANT de lancer quoi que ce soit
    verifier_contrat(hote, opt)

    # 2. budget de la machine : on sait a quoi on a affaire avant de charger
    b0 = budget(hote, opt)
    if b0 and not b0.get("_simule"):
        info("budget : %s coeurs | RAM %s Mo dispo | disque %s Mo libres | "
             "terminaux deja en vie %s" % (b0.get("NPROC", "?"),
                                           b0.get("MEM_DISPO_MO", "?"),
                                           b0.get("DISQUE_LIBRE_MO", "?"),
                                           b0.get("TERMINAUX", "?")))
        dispo = entier(b0.get("MEM_DISPO_MO"), 0)
        disque = entier(b0.get("DISQUE_LIBRE_MO"), 0)
        # 275 Mo mesures par test complet : c'est le chiffre a retenir pour la RAM
        if dispo and opt.creneaux * 275 > dispo * 0.8:
            info("  AVERTISSEMENT RAM : %d creneaux x 275 Mo (mesure) = %d Mo, la "
                 "machine n'a que %d Mo disponibles."
                 % (opt.creneaux, opt.creneaux * 275, dispo))
        if disque and disque < opt.creneaux * 400:
            info("  AVERTISSEMENT DISQUE : %d Mo libres pour %d creneaux (un creneau "
                 "consomme ~150-400 Mo de donnees variables ; les binaires sont "
                 "partages, eux)." % (disque, opt.creneaux))

    if opt.creer:
        creer_tous(hote, creneaux, opt)

    if opt.diag:
        diag_debit(hote, opt)
        if opt.dry_run:
            info("SIMULATION terminee : rien n'a ete cree, demarre ni arrete.")
        return 0

    if opt.rampe:
        vivants, complet = rampe(hote, creneaux, opt)
        if not complet:
            info("rampe interrompue : %d creneau(x) vivant(s)." % vivants)
            return 0
        if opt.demarrer:
            info("(les creneaux ont deja ete demarres par la rampe)")
    elif opt.demarrer:
        vivants, mauvais = demarrer_sequentiel(hote, creneaux, opt)
        if mauvais:
            info("emplacements en echec : %s"
                 % ", ".join(c.tag for c in mauvais))
        info("=== %d/%d emplacement(s) verifies vivants ===" % (len(vivants),
                                                                len(creneaux)))

    if opt.surveiller or opt.sante or opt.once:
        lire_tous(hote, creneaux, opt)
        print(tableau(creneaux, opt, budget(hote, opt), compter_file(opt.db_local)),
              flush=True)
        if opt.surveiller and not opt.sante:
            surveiller(hote, creneaux, opt)

    if opt.arreter:
        arreter_tous(hote, creneaux, opt)

    if opt.dry_run:
        info("SIMULATION terminee : rien n'a ete cree, demarre ni arrete.")
        return 0

    compte = {}
    for c in creneaux:
        compte[c.etat] = compte.get(c.etat, 0) + 1
    info("RESUME : " + " | ".join("%s %d" % (e, compte.get(e, 0)) for e in ORDRE))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        info("interrompu.")
        sys.exit(130)
