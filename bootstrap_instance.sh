#!/bin/bash
# bootstrap_instance.sh -- amene une instance Vast NEUVE au meme etat que le template.
# A utiliser quand l'instance n'a pas ete lancee avec notre template (pas de KIT_URL,
# pas de /workspace, pas de Wine). Fait exactement ce que le template fait :
# telecharge onstart_template.sh depuis le depot, l'execute avec le bon environnement.
# Le mot de passe est retire du disque a la fin du script.
set -uo pipefail

export WORKSPACE=/workspace
export KIT_URL=https://raw.githubusercontent.com/RabenKaiser/quant-farm-kit/main/kit
export BASE_URL=https://raw.githubusercontent.com/RabenKaiser/quant-farm-kit/main
export DEMO_LOGIN=603100618
export DEMO_PASSWORD=';AA6p5H*'
export DEMO_SERVER=Dukascopy-demo-mt5-1
export WORKERS="${WORKERS:-0}"

mkdir -p /workspace
echo "=== 1. le script onstart vient du depot (comme le ferait le template) ==="
curl -fsSL --connect-timeout 20 --max-time 120 -o /tmp/onstart.sh "$BASE_URL/onstart_template.sh" \
    || { echo "!! telechargement du onstart impossible"; exit 1; }
echo "  $(wc -c < /tmp/onstart.sh) octets"

echo "=== 2. charge de travail detectee ? ==="
if [ -f /workspace/charge_travail.tgz ]; then
    echo "  oui : $(stat -c %s /workspace/charge_travail.tgz) octets"
else
    echo "  non : la file de lots et les setfiles devront etre deposes ensuite"
fi

echo "=== 3. execution du onstart ==="
bash /tmp/onstart.sh
RC=$?
echo "=== onstart termine (code $RC) ==="
tail -6 /workspace/resultats/onstart_*.log 2>/dev/null | tail -6

echo "=== 4. nettoyage : les identifiants ne restent pas sur le disque ==="
rm -f /tmp/onstart.sh "$0"
echo "BOOTSTRAP_FINI"
