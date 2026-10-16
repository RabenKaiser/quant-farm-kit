# DOCKERFILE -- image MT5-sous-Wine pour Vast.ai (le travail n'a AUCUN besoin de GPU).
#
# Base : ubuntu 24.04 — exactement l'OS sur lequel le couple Wine 11 + MT5 6230 est PROUVE.
# Ce qui est cuit dans l'image : Wine 11, le programme MT5 (sans donnees), les scripts,
# le stub no_broker.so. Ce qui n'y est PAS : donnees de marche, comptes, queue — tout
# cela vit dans /workspace (stockage Vast) ou se retire depuis la source avec l'IP fraiche.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    WINEDEBUG=-all \
    WINEPREFIX=/opt/wpx \
    WINEDLLOVERRIDES="mscoree,mshtml=" \
    PATH="/opt/wine-stable/bin:${PATH}"

RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates wget gnupg2 gcc libc6-dev python3 python3-numpy \
      xz-utils xvfb winbind sudo tini \
 && mkdir -pm755 /usr/local/share/keyrings \
 && wget -qO- https://dl.winehq.org/wine-builds/ubuntu/dists/noble/winehq.key \
      | gpg --dearmor -o /usr/local/share/keyrings/winehq-archive.key \
 && wget -qO /etc/apt/sources.list.d/winehq.sources \
      https://dl.winehq.org/wine-builds/ubuntu/dists/noble/winehq-noble.sources \
 && apt-get update \
 && apt-get install -y --install-recommends winehq-stable \
 && rm -rf /var/lib/apt/lists/*

# Quarantaine respectee par construction : tout connect()/getaddrinfo() echoue.
COPY no_broker.c /tmp/no_broker.c
RUN gcc -shared -fPIC -o /usr/local/lib/no_broker.so /tmp/no_broker.c \
 && rm /tmp/no_broker.c

# Le programme MT5 PROUVE (build 6230, headless, sans #property script_show_inputs).
COPY mt5/ /opt/mt5/
COPY scripts/ /opt/scripts/

# Prefixe Wine pret a l'emploi (xvfb-run remplace l'ecran absent).
RUN xvfb-run wineboot -i \
 && rm -rf /opt/wpx/drive_c/users/root/Temp/*

# Les pieges payes, encodes ici :
#  - MaxBars=5000000 sinon erreur 4401 (dans config/*.ini livres par les scripts)
#  - UN terminal par dossier de donnees (le lanceur le verifie)
#  - arret PROPRE obligatoire (CloseMainWindow/taskkill sans /F)
WORKDIR /opt/mt5
CMD ["/bin/bash"]
