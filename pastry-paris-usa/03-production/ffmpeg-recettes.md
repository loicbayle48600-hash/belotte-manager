# Recettes ffmpeg — passer un rush de 40 s à 1:05+

Toutes les commandes supposent un rush vertical. Si le rush est horizontal, recadre d'abord
(dernière section).

## 1. Ralentir un plan (money shot)

`setpts=N*PTS` : N = 1/vitesse. 0,4x → `setpts=2.5*PTS`.

```bash
# extraire le plan à ralentir (ici 3 s à partir de 00:12)
ffmpeg -ss 00:00:12 -i rush.mp4 -t 3 -c copy plan.mp4

# ralentir à 0,4x → 7,5 s (vidéo seule, son coupé)
ffmpeg -i plan.mp4 -filter:v "setpts=2.5*PTS" -an plan_slow.mp4
```

Avec le son conservé (`atempo` ne descend pas sous 0.5, donc on l'enchaîne) :

```bash
ffmpeg -i plan.mp4 -filter_complex \
  "[0:v]setpts=2.5*PTS[v];[0:a]atempo=0.5,atempo=0.8[a]" \
  -map "[v]" -map "[a]" plan_slow.mp4
```

## 2. Interpolation (ralenti fluide sur un rush 30 fps)

À utiliser sur les coulées et les poudrages, ça évite le saccadé :

```bash
ffmpeg -i plan.mp4 -filter:v "minterpolate=fps=120:mi_mode=mci,setpts=2.5*PTS" -an plan_slow.mp4
```

Lent à calculer, mais c'est ce qui fait la différence entre « ralenti amateur » et « ralenti pub ».

## 3. Accélérer un passage ennuyeux

```bash
ffmpeg -i plan.mp4 -filter:v "setpts=0.25*PTS" -an plan_fast.mp4   # 4x
```

## 4. Freeze frame (1,2 s) sur un geste clé

```bash
# extraire l'image
ffmpeg -ss 00:00:18.4 -i rush.mp4 -frames:v 1 freeze.png

# la transformer en clip de 1,2 s avec un zoom lent (Ken Burns)
ffmpeg -loop 1 -i freeze.png -t 1.2 -r 30 \
  -vf "scale=2160:-1,zoompan=z='min(zoom+0.0015,1.12)':d=36:s=1080x1920,format=yuv420p" \
  freeze_clip.mp4
```

## 5. Boucle inversée (le plan à l'endroit puis à l'envers)

```bash
ffmpeg -i plan.mp4 -filter_complex "[0:v]reverse[r];[0:v][r]concat=n=2:v=1:a=0" -an boucle.mp4
```

## 6. Assembler le tout

Tous les clips doivent avoir **le même codec, la même résolution et le même fps** avant concat.
Normalise d'abord :

```bash
for f in *.mp4; do
  ffmpeg -i "$f" -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30" \
    -c:v libx264 -crf 18 -preset slow -pix_fmt yuv420p -an "norm_$f"
done

# liste ordonnée
printf "file '%s'\n" norm_hook.mp4 norm_contexte.mp4 norm_plan_slow.mp4 ... > liste.txt
ffmpeg -f concat -safe 0 -i liste.txt -c copy montage.mp4
```

## 7. Coller la voix off + l'ambiance

```bash
ffmpeg -i montage.mp4 -i vo_en.wav -i ambiance.wav -filter_complex \
  "[1:a]volume=1.0[vo];[2:a]volume=0.12[amb];[vo][amb]amix=inputs=2:duration=first[a]" \
  -map 0:v -map "[a]" -c:v copy -c:a aac -b:a 192k final.mp4
```

## 8. Vérifier la durée avant publication (impératif)

```bash
ffprobe -v error -show_entries format=duration -of csv=p=0 final.mp4
```

**Doit renvoyer > 61.0.** Entre 60,0 et 61,0, un ré-encodage côté plateforme peut te faire
passer sous la minute et te sortir des programmes de rémunération.

## 9. Recadrer un rush horizontal en 9:16

```bash
ffmpeg -i rush_h.mp4 -vf "crop=ih*9/16:ih,scale=1080:1920" -c:a copy rush_v.mp4
```

Décale le cadre si le sujet n'est pas centré : `crop=ih*9/16:ih:(iw-ih*9/16)/2+200:0`.

## 10. Export final

```bash
ffmpeg -i final.mp4 -c:v libx264 -crf 18 -preset slow -profile:v high -pix_fmt yuv420p \
  -movflags +faststart -c:a aac -b:a 192k -ar 48000 upload.mp4
```

Vise un fichier **sous 280 Mo** — au-delà, la compression de la plateforme écrase les détails
(et sur de la pâtisserie, le détail est tout le produit).
