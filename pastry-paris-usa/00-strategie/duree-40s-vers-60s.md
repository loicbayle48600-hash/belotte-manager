# Passer une vidéo de 40 s à plus d'1 min

## Réponse courte

Oui, et c'est même ce qu'il faut faire. Mais **ne ralentis pas toute la vidéo** : 40 s étirées
en 60 s, ça donne 0,66x sur toute la durée, l'image devient molle, la voix devient bizarre et
la rétention s'effondre. La bonne méthode est d'**ajouter du temps aux bons endroits**.

Cible réelle : **1 min 05 à 1 min 15**, jamais 1:00 pile. Les programmes de rémunération
(TikTok Rewards, Shorts) exigent une durée *strictement supérieure* à 60 s, et un rush qui
tombe à 59,8 s après export t'élimine.

## Le budget des 25–35 s à ajouter

| Bloc à ajouter | Durée | Où | Effet |
|---|---|---|---|
| Hook visuel + texte | 3–5 s | Tout au début | Le plan le plus beau de la vidéo, remonté en tête |
| Contexte / setup | 5–8 s | Après le hook | « This is X, he trains at Y in Paris » |
| Ralenti 0,4x sur 2–3 money shots | +8–12 s | Au milieu | Coulée de chocolat, feuilletage, découpe, glaçage |
| Freeze + zoom (Ken Burns) | 1–1,5 s ×4 | Sur les gestes clés | Fait respirer et casse la monotonie |
| Carte recette / ingrédients | 4–6 s | Avant la fin | Excellent pour les saves et les commentaires |
| Plan final tenu + CTA | 4–6 s | Fin | Fait monter le temps de visionnage moyen |

40 s de rush + ces blocs = 65 à 75 s sans jamais donner l'impression d'être étiré.

## Les 3 ralentis qui marchent

1. **Le money shot en 0,4x** : le chocolat qui tombe, la crème qui sort de la poche, le
   couteau qui traverse le feuilleté. Ralenti = plus premium, pas plus lent.
2. **Le speed ramp** : vitesse normale → 0,3x pile sur l'impact → retour normal. 1,5 s de rush
   devient 4 s à l'écran.
3. **La boucle inversée** : le même plan joué à l'endroit puis à l'envers (2× la durée), ça
   marche très bien sur les coulées et les poudrages de sucre glace.

À l'inverse : **accélère** les gestes ennuyeux (pétrissage, attente, nettoyage) à 4x. Tu perds
2 s et tu gagnes en rythme, ce qui te laisse plus de budget pour les ralentis.

## Ce qu'il ne faut pas faire

- Ralentir la voix du pâtissier (elle devient inaudible). Si tu ralentis un plan parlé, tu
  coupes le son d'origine et tu passes en VO anglaise ou en musique.
- Ajouter 20 s d'écran noir / de générique. Compté comme durée, mais tue la rétention et donc
  la distribution.
- Boucler la vidéo entière deux fois. Détecté comme contenu répété.

## Recettes ffmpeg

Voir `03-production/ffmpeg-recettes.md` pour les commandes exactes (ralenti, speed ramp,
freeze frame, concat, export 1080×1920).
