# quant-farm-kit

Kit d'amorcage d'une instance Vast pour la meule de tests MT5.
Aucun identifiant, aucune donnee de marche : l'instance tire ses donnees elle-meme
avec son IP fraiche.

## Contenu

| fichier | role |
|---|---|
| `onstart_template.sh` | le script de demarrage (a mettre dans le template Vast) |
| `verifier_pret.sh` | auto-test de premier demarrage -> `/workspace/resultats/etat_pret.txt` |
| `kit/mt5prog.tgz.part-aa` + `part-ab` | le programme MT5 6230 prouve, decoupe (limite GitHub : 100 Mo) |
| `kit/scripts_pack.tgz` | scripts python + EA a jalons, SANS identifiants |
| `image/Dockerfile` + `image/no_broker.c` | variante image cuite (VM uniquement, plus tard) |

## Template Vast

- Image : `ubuntu:24.04`
- `PROVISIONING_SCRIPT = https://raw.githubusercontent.com/RabenKaiser/quant-farm-kit/main/onstart_template.sh`
- Variables : `KIT_URL`, `DEMO_LOGIN`, `DEMO_PASSWORD` (les identifiants ne vivent
  QUE la, jamais dans ce depot)

## Verifier apres le boot

`cat /workspace/resultats/etat_pret.txt` -> doit finir par `VERDICT : PRET`.
