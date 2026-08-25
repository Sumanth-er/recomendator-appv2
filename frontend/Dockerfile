FROM nginx:1.27-alpine

# nginx's own entrypoint runs envsubst over /etc/nginx/templates at start-up,
# so PORT and BACKEND_URL are baked in when the container boots rather than at
# build time.
COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY index.html styles.css app.js /usr/share/nginx/html/

ENV PORT=8080 BACKEND_URL=http://backend:8080
EXPOSE 8080
