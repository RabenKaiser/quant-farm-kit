# DEPOT GITHUB — quant-farm-kit (voie retenue le 16/10)

Le depot existe : `git@github.com:RabenKaiser/quant-farm-kit.git`.
La release n'est plus necessaire : les URL brutes du depot suffisent.

## 1. La seule action humaine : la cle de depot (deploy key)

Le depot peut rester PRIVE — l'instance n'y accede pas par URL brute alors ? SI :
`raw.githubusercontent.com` sur un depot PRIVE exige un jeton. Deux options :
- depot PUBLIC (contenu verifie : aucun compte, aucun mot de passe, aucune donnee) ;
- ou jeton fin-grain lecture seule en variable `KIT_TOKEN` du template.
Par defaut : depot PUBLIC.

## 2. Contenu du depot (pousse par la meule locale)

```
README.md                 <- ce fichier
onstart_template.sh       <- le template lui-meme
kit/mt5prog.tgz.part-aa   <- le programme MT5, DECOUPE (limite GitHub : 100 Mo)
kit/mt5prog.tgz.part-ab
kit/scripts_pack.tgz      <- scripts + EA a jalons (sans mot de passe)
image/Dockerfile          <- pour la variante image cuite (VM, plus tard)
image/no_broker.c
```

## 3. Le template Vast

- Image : `ubuntu:24.04` (ou toute base CPU recente)
- `PROVISIONING_SCRIPT=https://raw.githubusercontent.com/RabenKaiser/quant-farm-kit/main/onstart_template.sh`
- Variables d'environnement :
  - `KIT_URL=https://raw.githubusercontent.com/RabenKaiser/quant-farm-kit/main/kit`
  - `DEMO_LOGIN=603100618` et `DEMO_PASSWORD=...` (le paquet ne contient AUCUN mot de passe depuis le 16/10 — redaction confirmee)

## 4. Ce que le kit ne contient PAS

AUCUN compte, AUCUN mot de passe (le travailleur lit DEMO_LOGIN/DEMO_PASSWORD de
l'environnement), AUCUNE donnee de marche : l'instance tire les siennes avec son IP
fraiche — PREEUVE le 16/10 : autorisation reussie sur l'IP de l'instance,
417 symboles synchronises.
