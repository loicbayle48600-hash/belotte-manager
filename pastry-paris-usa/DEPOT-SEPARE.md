# Sortir ce projet dans un dépôt à part

Le projet est volontairement autonome : rien ici ne dépend de `belotte-manager`. Il est posé
dans ce dépôt uniquement parce que la création d'un dépôt GitHub n'est pas autorisée depuis
cette session (l'API répond `403 Resource not accessible by integration`).

## Option 1 — tu crées le dépôt vide, je pousse

1. Sur GitHub : **New repository** → nom `pastry-paris-usa` → privé → **sans** README ni .gitignore.
2. Dis-moi le nom exact : je l'attache à la session et j'y pousse le contenu tel quel.

## Option 2 — tu le fais toi-même en 5 commandes

```bash
# depuis ta machine, à la racine d'un clone de belotte-manager
git checkout claude/pastry-video-script-paris-ih9cba
cp -r pastry-paris-usa /chemin/vers/pastry-paris-usa
cd /chemin/vers/pastry-paris-usa
git init -b main && git add . && git commit -m "Kit scripts patisserie Paris -> USA"
git remote add origin https://github.com/<toi>/pastry-paris-usa.git
git push -u origin main
```

Une fois le dépôt à part en place, le dossier `pastry-paris-usa/` peut être supprimé d'ici.
