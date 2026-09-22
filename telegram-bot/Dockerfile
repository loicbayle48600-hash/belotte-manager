# Image minimale : aucune dépendance npm à installer.
FROM node:22-alpine

WORKDIR /app
COPY package.json ./
COPY index.js ./
COPY src ./src

ENV NODE_ENV=production
USER node
CMD ["node", "index.js"]
