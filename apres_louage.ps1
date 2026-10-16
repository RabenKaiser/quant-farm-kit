param(
    [Parameter(Mandatory=$true)][string]$Hote,      # ex: ssh7.vast.ai
    [Parameter(Mandatory=$true)][int]$Port,          # ex: 26618
    [string]$Cle = "$env:USERPROFILE\.ssh\vast_quant",
    [int]$Travailleurs = 0,                          # 0 = ne pas demarrer les travailleurs
    [switch]$Amorcer                                 # force le bootstrap si le template n'a pas tourne
)
# APRES_LOUAGE.ps1 -- une seule commande apres avoir loue une instance Vast.
# Fait, dans l'ordre : diagnostic du template, depot de la charge de travail,
# attente du verdict etat_pret.txt, demarrage des travailleurs si demande.
# ASCII uniquement (PowerShell 5.1 mange l'UTF-8 sans BOM).
$ErrorActionPreference = 'Continue'
$sshBase = @('-i', $Cle, '-p', "$Port", '-o', 'ConnectTimeout=15', '-o', 'BatchMode=yes', "root@$Hote")
$repo = 'C:\EAFarm\HISTORY - Copy\research\vast'

function Faire($cmd) { return (ssh @sshBase $cmd 2>&1 | Out-String) }

"=== 1. l'instance repond-elle ? ==="
$r = Faire 'hostname; nproc; free -g | awk ''NR==2{print "RAM " $2 " Go"}''; df -h /workspace 2>/dev/null | tail -1'
if ($r -notmatch '\S') { "!! aucune reponse. Verifier le port et que la cle est autorisee."; exit 1 }
$r.Trim() -split "`n" | ForEach-Object { "  " + $_.Trim() }

"=== 2. le template a-t-il ete applique ? ==="
$stub = Faire 'head -2 /root/onstart.sh 2>/dev/null'
$ws   = Faire 'ls /workspace 2>/dev/null | tr "\n" " "'
if ($stub -match 'This file is run on instance start') {
    "  !! TALON DE VAST : le template N'A PAS ete applique (onstart par defaut)."
    "     Inutile d'attendre : rien ne s'installera tout seul."
    if (-not $Amorcer) { "     Relancer avec -Amorcer pour amorcer a la main, ou corriger le template."; exit 2 }
} else {
    "  onstart personnalise present"
}
if ($ws -match '\S') { "  /workspace : " + $ws.Trim() } else { "  /workspace vide" }

"=== 3. depot de la charge de travail (file de lots + setfiles) ==="
$charge = Join-Path $repo 'charge_travail.tgz'
if (Test-Path $charge) {
    scp -i $Cle -P $Port (Join-Path $repo 'charge_travail.tgz') "root@${Hote}:/workspace/" 2>$null
    $n = Faire 'stat -c %s /workspace/charge_travail.tgz 2>/dev/null'
    if ($n -match '\d') { "  charge deposee : $($n.Trim()) octets" } else { "  !! depot echoue" }
} else { "  !! $charge introuvable" }

if ($Amorcer -and $stub -match 'This file is run on instance start') {
    "=== 3b. amorcage manuel (le template n'a pas tourne) ==="
    "  ATTENTION : les identifiants doivent etre fournis. Utiliser plutot le script"
    "  ON_START_A_COLLER.sh dans le template : il les contient deja."
    scp -i $Cle -P $Port (Join-Path $repo 'bootstrap_instance.sh') "root@${Hote}:/root/" 2>$null
    Faire 'setsid nohup bash /root/bootstrap_instance.sh > /root/bootstrap.log 2>&1 < /dev/null & echo lance' | Out-Null
    "  bootstrap lance : suivre avec  ssh ... ''tail -f /root/bootstrap.log''"
}

"=== 4. attente du verdict (jusqu'a 8 minutes) ==="
$verdict = ''
for ($i = 0; $i -lt 32; $i++) {
    Start-Sleep -Seconds 15
    $v = Faire 'cat /workspace/resultats/etat_pret.txt 2>/dev/null | tail -20'
    if ($v -match 'VERDICT') { $verdict = $v; break }
    if ($i % 4 -eq 3) { "  ... en cours ($([int](($i+1)*15/60)) min) : " + (Faire 'tail -1 /root/bootstrap.log 2>/dev/null; tail -1 /workspace/resultats/onstart_*.log 2>/dev/null' | Out-String).Trim() }
}
if ($verdict) {
    ""
    "=== VERDICT DE L'INSTANCE ==="
    $verdict.Trim() -split "`n" | ForEach-Object { "  " + $_.Trim() }
} else {
    "  !! pas encore de verdict. Regarder : tail -50 /var/log/onstart.log"
}

"=== 5. travailleurs ==="
if ($Travailleurs -gt 0) {
    $cmd = "cd /workspace && WORKERS=$Travailleurs QUEUE_DB=/workspace/file/usine_indus.db bash /workspace/onstart_template.sh 2>&1 | tail -12"
    "  demarrage de $Travailleurs travailleur(s) :"
    (Faire $cmd).Trim() -split "`n" | ForEach-Object { "    " + $_.Trim() }
} else {
    "  aucun travailleur demande (-Travailleurs N pour les lancer)"
}
"=== fin ==="
