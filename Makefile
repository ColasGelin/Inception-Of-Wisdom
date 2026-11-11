# Inception-of-Wisdom entry points. Every target takes PART=p1|p2|p3|bonus (default p3).
PART     ?= p3
BUG      ?= syntax
ALL      ?=
IOW_PORT ?= 8042
IOW_TARGET_PORT ?= 8081
API      := http://127.0.0.1:$(IOW_PORT)
COMPOSE  := docker compose --project-directory $(PART) -f $(PART)/docker-compose.yml
CURL     := curl -sS --fail-with-body -w '\n'

.DEFAULT_GOAL := help
.PHONY: help env up down logs ps reset bug heal-on heal-off test clean export-run gitea

help:
	@echo "make up [PART=p1|p2|p3|bonus]   build and start (default PART=p3), dashboard on $(API)"
	@echo "make down                      stop and remove the containers (volumes kept)"
	@echo "make logs                      follow all logs"
	@echo "make bug BUG=syntax|import|env|route|none   plant a bug on main and restart the target"
	@echo "make heal-off / make heal-on   flip the global kill switch (p3, bonus)"
	@echo "make reset                     recreate the target workspace from the pristine source"
	@echo "make test                      run the unit tests inside the agent image (no Ollama needed)"
	@echo "make export-run                write the latest healed and rolled-back runs to exports/"
	@echo "make gitea PART=bonus          print the local Gitea URL and login (bonus)"
	@echo "make clean [ALL=1]             remove runtime state (ALL=1 also removes model + Chroma volumes)"

# UID/GID of the host user owning the workspace, and the group owning the
# Docker socket as containers see it (differs between Linux and Docker Desktop).
env:
	@mkdir -p $(PART)/.runtime/target exports
	@printf 'IOW_UID=%s\nIOW_GID=%s\nDOCKER_GID=%s\nIOW_PORT=%s\nIOW_TARGET_PORT=%s\n' "$$(id -u)" "$$(id -g)" \
	  "$$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock python:3.12-slim stat -c %g /var/run/docker.sock)" \
	  "$(IOW_PORT)" "$(IOW_TARGET_PORT)" > $(PART)/.env
	@echo "wrote $(PART)/.env:" && sed 's/^/  /' $(PART)/.env

up: env
	$(COMPOSE) up -d --build
	@echo "dashboard: $(API)   (first start of p2/p3 downloads the models, follow with: make logs PART=$(PART))"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps

# Wipes the workspace (including agent commits) and rebuilds it from pristine
# source. ChromaDB is not touched: the watcher re-embeds only what differs.
reset:
	-$(COMPOSE) stop agent target
	$(COMPOSE) run --rm init python -m iow.workspace init --force
	$(COMPOSE) up -d

bug:
	@$(CURL) -X POST $(API)/demo/bug/$(BUG)

heal-off:
	@$(CURL) -X POST -H 'Content-Type: application/json' -d '{"enabled": false}' $(API)/loop/kill-switch

heal-on:
	@$(CURL) -X POST -H 'Content-Type: application/json' -d '{"enabled": true}' $(API)/loop/kill-switch

test:
	$(COMPOSE) build init
	$(COMPOSE) run --rm --no-deps -u "$$(id -u):$$(id -g)" init python -m pytest -q -p no:cacheprovider tests

gitea:
	@$(COMPOSE) exec agent cat /bootstrap/credentials

export-run:
	@mkdir -p exports
	$(COMPOSE) exec agent python -m iow.export /exports

clean: down
	rm -rf $(PART)/.runtime $(PART)/.env
ifeq ($(ALL),1)
	$(COMPOSE) down -v
endif
